"""
Azure DevOps Classic Pipeline — AI Code Review Script
Covers: Architecture, Security, EF Core/SQL, Performance,
        Code Quality, Error Handling, Testability, API Design
Outputs: HTML dashboard artifact + PR comments + pipeline annotations
Fails build on Critical (and optionally High) severity findings.
"""

import os, sys, json, subprocess, datetime, re, html
import requests
from pathlib import Path

# Force UTF-8 on Windows agents (default console is cp1252 which cannot encode emoji)
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ─── Configuration ──────────────────────────────────────────────────────────

ENDPOINT    = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
API_KEY     = os.environ.get("AZURE_OPENAI_KEY", "")
DEPLOYMENT  = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-01")

ORG         = os.environ.get("SYSTEM_TEAMFOUNDATIONCOLLECTIONURI", "")
PROJECT     = os.environ.get("SYSTEM_TEAMPROJECT", "")
REPO_ID     = os.environ.get("BUILD_REPOSITORY_ID", "")
PR_ID       = os.environ.get("SYSTEM_PULLREQUEST_PULLREQUESTID", "")
ADO_PAT     = os.environ.get("AZURE_DEVOPS_PAT", "")
BUILD_ID    = os.environ.get("BUILD_BUILDID", "0")
BUILD_NUM   = os.environ.get("BUILD_BUILDNUMBER", "unknown")
BRANCH      = os.environ.get("BUILD_SOURCEBRANCH", "unknown")
REPO_NAME   = os.environ.get("BUILD_REPOSITORY_NAME", "unknown")

# Fail build on these severities (comma-separated env override)
FAIL_ON     = set(os.environ.get("REVIEW_FAIL_ON", "critical,high").lower().split(","))
MAX_DIFF_CHARS = int(os.environ.get("REVIEW_MAX_DIFF_CHARS", "15000"))
OUTPUT_DIR  = os.environ.get("BUILD_ARTIFACTSTAGINGDIRECTORY", "/tmp/review-output")

Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

# ─── Load domain prompts from markdown file ──────────────────────────────────────

def load_domains(md_path: str) -> dict:
    """
    Parse review_prompts.md into a DOMAINS dict.

    Each domain block starts with a level-2 heading:
        ## domain_key | Title | icon | #hexcolor

    Everything between that heading and the next ## (or EOF) is the prompt.
    Lines beginning with # (comments) and --- dividers are stripped.
    The script appends "CODE DIFF:" automatically, so it must NOT be in the file.
    """
    path = Path(md_path)
    if not path.exists():
        print("[ERROR] review_prompts.md not found at: " + md_path)
        print("        Expected location: scripts/review_prompts.md beside ai_review.py")
        sys.exit(1)

    raw = path.read_text(encoding="utf-8")
    domains = {}
    sections = re.split(r'\n(?=## )', raw)

    for section in sections:
        lines = section.splitlines()
        heading = next((l for l in lines if l.startswith("## ")), None)
        if not heading:
            continue

        # Parse: ## key | Title | icon | #color
        parts = [p.strip() for p in heading[3:].split("|")]
        if len(parts) < 4:
            print("[WARN] Skipping malformed heading (need 4 pipe-separated parts): " + heading)
            continue

        key, title, icon, color = parts[0], parts[1], parts[2], parts[3]
        if not key or not title:
            continue

        # Everything after the heading line = prompt body
        heading_idx = next(i for i, l in enumerate(lines) if l.startswith("## "))
        body_lines = lines[heading_idx + 1:]

        # Strip comment lines, horizontal rules, leading/trailing blanks
        cleaned = []
        for line in body_lines:
            stripped = line.rstrip()
            if stripped.startswith("#"):
                continue
            if re.match(r"^-{3,}$", stripped):
                continue
            cleaned.append(stripped)

        while cleaned and not cleaned[0]:
            cleaned.pop(0)
        while cleaned and not cleaned[-1]:
            cleaned.pop()

        prompt = "\n".join(cleaned)
        if not prompt:
            print("[WARN] Domain '" + key + "' has an empty prompt body - skipping.")
            continue

        domains[key] = {
            "title":  title,
            "icon":   icon,
            "color":  color,
            "prompt": prompt,
        }

    if not domains:
        print("[ERROR] No domains loaded from " + md_path)
        print("        Check headings follow the format:  ## key | Title | icon | #color")
        sys.exit(1)

    return domains


# Resolve prompts file relative to this script so the path works on any agent
_SCRIPT_DIR   = Path(__file__).parent
_PROMPTS_FILE = os.environ.get(
    "REVIEW_PROMPTS_FILE",
    str(_SCRIPT_DIR / "review_prompts.md")
)

DOMAINS = load_domains(_PROMPTS_FILE)
print("[INFO] Loaded " + str(len(DOMAINS)) + " review domain(s) from: " + _PROMPTS_FILE)
# ─── Helpers ────────────────────────────────────────────────────────────────

def log(msg: str):
    print(msg, flush=True)

def vso(cmd: str, msg: str):
    """Emit an Azure DevOps logging command."""
    print(f"##vso[{cmd}]{msg}", flush=True)

def get_diff() -> str:
    """Extract git diff of changed .cs and .sql files."""
    try:
        # Try PR merge base first
        base = subprocess.run(
            ["git", "merge-base", "HEAD", "origin/master"],
            capture_output=True, text=True
        ).stdout.strip()

        if not base:
            base = "HEAD~1"

        result = subprocess.run(
            ["git", "diff", base, "HEAD",
             "--unified=8",
             "--diff-filter=ACMR",
             "--", "*.cs", "*.sql", "*.csproj"],
            capture_output=True, text=True, timeout=60
        )
        diff = result.stdout
        if len(diff) > MAX_DIFF_CHARS:
            log(f"[WARN] Diff truncated from {len(diff)} to {MAX_DIFF_CHARS} chars to stay within token budget.")
            diff = diff[:MAX_DIFF_CHARS]
        return diff
    except Exception as e:
        log(f"[WARN] git diff failed: {e}")
        return ""

def call_openai(prompt: str, diff: str) -> list:
    """Call Azure OpenAI and parse the JSON array response."""
    url = f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}/chat/completions?api-version={API_VERSION}"
    payload = {
        "messages": [
            {
                "role": "system",
                "content": "You are an expert code reviewer. Always respond with ONLY valid JSON arrays. No markdown, no prose, no code fences."
            },
            {
                "role": "user",
                "content": prompt + diff
            }
        ],
        "temperature": 0.1,
        "max_tokens": 2500
    }
    resp = requests.post(
        url,
        headers={"api-key": API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=120
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    # Strip any accidental markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw).strip()
    raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        issues = json.loads(raw)
        return issues if isinstance(issues, list) else []
    except json.JSONDecodeError as e:
        log(f"  [WARN] JSON parse error: {e}. Raw: {raw[:300]}")
        return []

def post_pr_comment(body: str):
    """Post a comment to the active PR via Azure DevOps REST API."""
    if not PR_ID or not ADO_PAT:
        return
    url = (f"{ORG.rstrip('/')}/{PROJECT}/_apis/git/repositories/{REPO_ID}"
           f"/pullRequests/{PR_ID}/threads?api-version=7.1")
    resp = requests.post(
        url,
        auth=("", ADO_PAT),
        json={"comments": [{"content": body, "commentType": 1}], "status": 1},
        timeout=30
    )
    if not resp.ok:
        log(f"  [WARN] PR comment failed: {resp.status_code} {resp.text[:200]}")

# ─── HTML Dashboard generator ────────────────────────────────────────────────

def build_dashboard(all_issues: list, meta: dict) -> str:
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    all_issues_sorted = sorted(all_issues, key=lambda i: severity_order.get(i.get("severity","info"), 4))

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for i in all_issues:
        counts[i.get("severity", "info")] = counts.get(i.get("severity","info"), 0) + 1

    domain_counts = {}
    for i in all_issues:
        d = i.get("domain", "unknown")
        domain_counts[d] = domain_counts.get(d, 0) + 1

    total = len(all_issues)
    passed = total == 0 or (counts["critical"] == 0 and counts["high"] == 0)
    status_text = "PASSED" if passed else "FAILED"
    status_color = "#10b981" if passed else "#ef4444"

    def badge(sev):
        colors = {
            "critical": ("#7f1d1d","#fca5a5"),
            "high":     ("#78350f","#fcd34d"),
            "medium":   ("#1e3a5f","#93c5fd"),
            "low":      ("#064e3b","#6ee7b7"),
            "info":     ("#1f2937","#d1d5db"),
        }
        bg, fg = colors.get(sev, ("#1f2937","#d1d5db"))
        return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;text-transform:uppercase">{html.escape(sev)}</span>'

    def render_issues(issues):
        if not issues:
            return '<p style="color:#6b7280;padding:16px;text-align:center">No issues found &#x2713;</p>'
        rows = []
        for issue in issues:
            sev = issue.get("severity", "info")
            left_colors = {
                "critical": "#ef4444", "high": "#f59e0b",
                "medium": "#3b82f6", "low": "#10b981", "info": "#6b7280"
            }
            lc            = left_colors.get(sev, "#6b7280")
            domain_key    = issue.get("domain", "")
            domain_info   = DOMAINS.get(domain_key, {})
            domain_title  = html.escape(domain_info.get("title", domain_key))
            icon          = domain_info.get("icon", "&#x1F50D;")
            file_str      = html.escape(str(issue.get("file", "unknown")))
            line_str      = str(issue.get("line", ""))
            rule_str      = html.escape(str(issue.get("rule", "")))
            msg_str       = html.escape(str(issue.get("message", "")))
            sug_str       = html.escape(str(issue.get("suggestion", "")))
            loc           = (file_str + ":" + line_str) if (line_str and line_str != "0") else file_str
            sug_html      = ('<p style="color:#6ee7b7;margin:0;font-size:12px;line-height:1.5">&#x1F4A1; '
                             + sug_str + '</p>') if sug_str else ''
            card = (
                '\n<div style="border-left:4px solid ' + lc + ';background:#111827;'
                'border-radius:0 8px 8px 0;padding:14px 16px;margin-bottom:8px" '
                'data-sev="' + sev + '" data-domain="' + html.escape(domain_key) + '"">'
                '\n  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">'
                '\n    ' + badge(sev) +
                '\n    <span style="background:#1f2937;color:#9ca3af;padding:2px 8px;border-radius:4px;font-size:11px">' +
                icon + ' ' + domain_title + '</span>'
                '\n    <code style="color:#818cf8;font-size:12px">' + rule_str + '</code>'
                '\n    <span style="color:#4b5563;font-size:11px;margin-left:auto">' + html.escape(loc) + '</span>'
                '\n  </div>'
                '\n  <p style="color:#f3f4f6;margin:0 0 6px;font-size:14px;line-height:1.5">' + msg_str + '</p>'
                '\n  ' + sug_html +
                '\n</div>'
            )
            rows.append(card)
        return "\n".join(rows)

    domain_tabs = []
    for key, info in DOMAINS.items():
        cnt = domain_counts.get(key, 0)
        badge_color = "#ef4444" if cnt > 0 else "#374151"
        domain_tabs.append(
            f'<button onclick="filterDomain(\'{key}\')" '
            f'style="background:#1f2937;color:#d1d5db;border:1px solid #374151;'
            f'padding:6px 12px;border-radius:6px;cursor:pointer;font-size:13px;'
            f'display:inline-flex;align-items:center;gap:6px">'
            f'{info["icon"]} {info["title"]} '
            f'<span style="background:{badge_color};color:#fff;border-radius:10px;'
            f'padding:1px 7px;font-size:11px">{cnt}</span></button>'
        )

    issues_json = json.dumps(all_issues_sorted)

    # Pre-compute everything that would require complex expressions inside f-strings.
    # Python < 3.12 does not allow brackets, quotes or ternaries inside f-string braces
    # when the f-string itself uses the same quote style.

    escaped_build   = html.escape(BUILD_NUM)
    escaped_branch  = html.escape(BRANCH)
    escaped_repo    = html.escape(REPO_NAME)
    escaped_ts      = html.escape(meta.get("timestamp", ""))
    escaped_dur     = html.escape(str(meta.get("duration_s", "")))

    status_bg       = "#052e16" if passed else "#450a0a"
    status_icon     = "✅" if passed else "❌"
    status_border   = status_color + "40"
    if passed:
        status_detail = " — No critical or high issues"
    else:
        status_detail = " — {0} critical, {1} high issues found".format(
            counts["critical"], counts["high"]
        )

    cnt_critical = counts["critical"]
    cnt_high     = counts["high"]
    cnt_medium   = counts["medium"]
    cnt_low      = counts["low"]
    cnt_info     = counts["info"]

    # Build domain summary cards as a plain string (no nested f-string)
    domain_cards_parts = []
    for key, info in DOMAINS.items():
        dc = domain_counts.get(key, 0)
        plural = "s" if dc != 1 else ""
        domain_cards_parts.append(
            '<div class="summary-card">'
            '<span class="icon">' + info["icon"] + '</span>'
            '<div class="info">'
            '<div class="title">' + info["title"] + '</div>'
            '<div class="count">' + str(dc) + ' issue' + plural + '</div>'
            '</div>'
            '</div>'
        )
    domain_cards_html = "\n    ".join(domain_cards_parts)

    domain_tabs_html  = "\n".join(domain_tabs)
    issues_html       = render_issues(all_issues_sorted)

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Code Review &mdash; Build """ + escaped_build + """</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #030712; color: #f9fafb; font-family: 'Segoe UI', system-ui, sans-serif; min-height: 100vh; }
  .header { background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%); border-bottom: 1px solid #1f2937; padding: 24px 32px; }
  .header h1 { font-size: 22px; font-weight: 700; color: #f9fafb; margin-bottom: 4px; }
  .header .meta { color: #6b7280; font-size: 13px; display: flex; gap: 16px; flex-wrap: wrap; margin-top: 8px; }
  .header .meta span { display: flex; align-items: center; gap: 4px; }
  .status-pill { display: inline-flex; align-items: center; gap: 8px; padding: 6px 16px; border-radius: 20px; font-weight: 700; font-size: 14px; margin-top: 12px; }
  .main { padding: 24px 32px; max-width: 1200px; margin: 0 auto; }
  .scorecard { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 24px; }
  .score-card { background: #111827; border: 1px solid #1f2937; border-radius: 10px; padding: 16px; text-align: center; }
  .score-card .num { font-size: 32px; font-weight: 800; margin-bottom: 4px; }
  .score-card .label { font-size: 12px; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.05em; }
  .filters { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; align-items: center; }
  .filters h3 { color: #9ca3af; font-size: 13px; font-weight: 500; margin-right: 4px; }
  .sev-btn { background: #1f2937; color: #d1d5db; border: 1px solid #374151; padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .sev-btn.active { border-color: #6366f1; color: #a5b4fc; }
  .section-title { font-size: 16px; font-weight: 600; color: #e5e7eb; margin: 20px 0 12px; display: flex; align-items: center; gap: 8px; }
  .domain-tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; }
  .summary-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 24px; }
  .summary-card { background: #111827; border: 1px solid #1f2937; border-radius: 8px; padding: 12px 16px; display: flex; align-items: center; gap: 10px; }
  .summary-card .icon { font-size: 20px; }
  .summary-card .info .title { font-size: 13px; font-weight: 600; color: #e5e7eb; }
  .summary-card .info .count { font-size: 12px; color: #6b7280; }
  input[type=text] { background: #111827; border: 1px solid #374151; color: #f9fafb; padding: 8px 14px; border-radius: 8px; font-size: 14px; width: 280px; outline: none; }
  input[type=text]::placeholder { color: #4b5563; }
  #no-results { color: #6b7280; text-align: center; padding: 40px; display: none; }
</style>
</head>
<body>
<div class="header">
  <h1>&#x1F916; AI Code Review Dashboard</h1>
  <div class="meta">
    <span>&#x1F4E6; Build <strong style="color:#c7d2fe">""" + escaped_build + """</strong></span>
    <span>&#x1F33F; Branch <strong style="color:#c7d2fe">""" + escaped_branch + """</strong></span>
    <span>&#x1F5C2; Repo <strong style="color:#c7d2fe">""" + escaped_repo + """</strong></span>
    <span>&#x1F550; """ + escaped_ts + """</span>
    <span>&#x23F1; """ + escaped_dur + """s analysis time</span>
  </div>
  <div class="status-pill" style="background:""" + status_bg + """;color:""" + status_color + """;border:1px solid """ + status_border + """">
    """ + status_icon + """ Review """ + status_text + status_detail + """
  </div>
</div>

<div class="main">
  <!-- Score cards -->
  <div class="scorecard">
    <div class="score-card">
      <div class="num" style="color:#ef4444">""" + str(cnt_critical) + """</div>
      <div class="label">Critical</div>
    </div>
    <div class="score-card">
      <div class="num" style="color:#f59e0b">""" + str(cnt_high) + """</div>
      <div class="label">High</div>
    </div>
    <div class="score-card">
      <div class="num" style="color:#3b82f6">""" + str(cnt_medium) + """</div>
      <div class="label">Medium</div>
    </div>
    <div class="score-card">
      <div class="num" style="color:#10b981">""" + str(cnt_low) + """</div>
      <div class="label">Low</div>
    </div>
    <div class="score-card">
      <div class="num" style="color:#6b7280">""" + str(cnt_info) + """</div>
      <div class="label">Info</div>
    </div>
  </div>

  <!-- Domain summary -->
  <div class="section-title">&#x1F4CA; Domain Summary</div>
  <div class="summary-grid">
    """ + domain_cards_html + """
  </div>

  <!-- Filters -->
  <div class="section-title">&#x1F50D; Issues (""" + str(total) + """ total)</div>
  <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <input type="text" id="search" placeholder="Search issues, files, rules..." oninput="applyFilters()">
    <div class="filters">
      <h3>Severity:</h3>
      <button class="sev-btn active" onclick="filterSev('all',this)">All</button>
      <button class="sev-btn" onclick="filterSev('critical',this)" style="color:#fca5a5">Critical</button>
      <button class="sev-btn" onclick="filterSev('high',this)" style="color:#fcd34d">High</button>
      <button class="sev-btn" onclick="filterSev('medium',this)" style="color:#93c5fd">Medium</button>
      <button class="sev-btn" onclick="filterSev('low',this)" style="color:#6ee7b7">Low</button>
      <button class="sev-btn" onclick="filterSev('info',this)" style="color:#9ca3af">Info</button>
    </div>
  </div>
  <div class="domain-tabs">""" + domain_tabs_html + """</div>

  <!-- Issues list -->
  <div id="issues-container">
    """ + issues_html + """
  </div>
  <div id="no-results">No issues match your filter.</div>
</div>

<script>
const ALL_ISSUES = """ + issues_json + """;
let currentSev = 'all';
let currentDomain = 'all';

function filterSev(sev, btn) {
  currentSev = sev;
  document.querySelectorAll('.sev-btn').forEach(b => b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  applyFilters();
}

function filterDomain(domain) {
  currentDomain = currentDomain === domain ? 'all' : domain;
  applyFilters();
}

function applyFilters() {
  const search = document.getElementById('search').value.toLowerCase();
  const container = document.getElementById('issues-container');
  const cards = container.querySelectorAll('[data-sev]');
  let visible = 0;
  cards.forEach(card => {
    const sev = card.getAttribute('data-sev');
    const dom = card.getAttribute('data-domain');
    const text = card.textContent.toLowerCase();
    const sevMatch = currentSev === 'all' || sev === currentSev;
    const domMatch = currentDomain === 'all' || dom === currentDomain;
    const searchMatch = !search || text.includes(search);
    if(sevMatch && domMatch && searchMatch) {
      card.style.display = '';
      visible++;
    } else {
      card.style.display = 'none';
    }
  });
  document.getElementById('no-results').style.display = visible === 0 ? '' : 'none';
}
</script>
</body>
</html>"""

# ─── PR comment builder ───────────────────────────────────────────────────────

def build_pr_comment(all_issues: list) -> str:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for i in all_issues:
        sev = i.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1

    passed = counts["critical"] == 0 and counts["high"] == 0
    lines = [
        f"## 🤖 AI Code Review — {'✅ Passed' if passed else '❌ Failed'}",
        "",
        f"| Severity | Count |",
        f"|----------|-------|",
        f"| 🔴 Critical | {counts['critical']} |",
        f"| 🟡 High | {counts['high']} |",
        f"| 🔵 Medium | {counts['medium']} |",
        f"| 🟢 Low | {counts['low']} |",
        f"| ⚪ Info | {counts['info']} |",
        "",
    ]

    critical_high = [i for i in all_issues if i.get("severity") in ("critical","high")]
    if critical_high:
        lines.append("### ⚠️ Critical & High Issues\n")
        for issue in critical_high[:20]:  # cap at 20 for PR comment length
            sev_icon = "🔴" if issue.get("severity") == "critical" else "🟡"
            domain_info = DOMAINS.get(issue.get("domain",""), {})
            loc = issue.get("file","")
            if issue.get("line") and str(issue.get("line")) != "0":
                loc += f":{issue.get('line')}"
            lines.append(f"**{sev_icon} [{issue.get('rule','')}]** `{loc}`  ")
            lines.append(f"{issue.get('message','')}  ")
            if issue.get("suggestion"):
                lines.append(f"💡 _{issue.get('suggestion')}_")
            lines.append("")

    lines.append("_Full report available as a pipeline artifact: `ai-review-report`_")
    return "\n".join(lines)

# ─── ADO inline display helpers ──────────────────────────────────────────────

def build_markdown_summary(all_issues: list) -> str:
    """
    Markdown that renders in the pipeline Build Summary tab via
    ##vso[task.addattachment type=Distributedtask.Core.Summary].
    ADO renders this markdown natively — no download needed.
    """
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for i in all_issues:
        sev = i.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1

    domain_counts = {}
    for i in all_issues:
        d = i.get("domain", "unknown")
        domain_counts[d] = domain_counts.get(d, 0) + 1

    passed  = counts["critical"] == 0 and counts["high"] == 0
    total   = len(all_issues)
    status  = "PASSED" if passed else "FAILED"
    icon    = "✅" if passed else "❌"

    lines = [
        "# " + icon + " AI Code Review — " + status,
        "",
        "| | Critical | High | Medium | Low | Info | Total |",
        "|---|---|---|---|---|---|---|",
        "| **Count** | "
            + str(counts["critical"]) + " | "
            + str(counts["high"])     + " | "
            + str(counts["medium"])   + " | "
            + str(counts["low"])      + " | "
            + str(counts["info"])     + " | "
            + str(total)              + " |",
        "",
        "## Domain Breakdown",
        "",
    ]

    for key, info in DOMAINS.items():
        cnt = domain_counts.get(key, 0)
        bar = ("🔴" if cnt >= 5 else "🟡" if cnt >= 2 else "🟢") if cnt > 0 else "✅"
        lines.append("| " + info["icon"] + " **" + info["title"] + "** | " + bar + " " + str(cnt) + " issue(s) |")

    lines += ["", "---", "", "## Critical & High Issues", ""]

    critical_high = [i for i in all_issues if i.get("severity") in ("critical", "high")]
    if not critical_high:
        lines.append("_No critical or high issues found._")
    else:
        for issue in critical_high[:30]:
            sev      = issue.get("severity", "high")
            sev_icon = "🔴" if sev == "critical" else "🟡"
            domain   = DOMAINS.get(issue.get("domain", ""), {})
            loc      = issue.get("file", "")
            if issue.get("line") and str(issue.get("line")) != "0":
                loc += ":" + str(issue.get("line"))
            rule = issue.get("rule", "")
            msg  = issue.get("message", "")
            sug  = issue.get("suggestion", "")
            lines.append(sev_icon + " **[" + rule + "]** `" + loc + "`")
            lines.append("")
            lines.append("> " + msg)
            if sug:
                lines.append("> ")
                lines.append("> 💡 _" + sug + "_")
            lines.append("")

    if len(critical_high) > 30:
        lines.append("_... and " + str(len(critical_high) - 30) + " more. See the full HTML report artifact._")

    lines += [
        "---",
        "",
        "## Medium Issues",
        "",
    ]
    medium = [i for i in all_issues if i.get("severity") == "medium"]
    if not medium:
        lines.append("_No medium issues._")
    else:
        for issue in medium[:20]:
            domain = DOMAINS.get(issue.get("domain", ""), {})
            loc    = issue.get("file", "")
            if issue.get("line") and str(issue.get("line")) != "0":
                loc += ":" + str(issue.get("line"))
            lines.append("- 🔵 **[" + issue.get("rule","") + "]** `" + loc + "` — " + issue.get("message",""))

    lines += [
        "",
        "---",
        "_Full interactive report: download the `ai-review-report` artifact from this pipeline run._",
    ]
    return "\n".join(lines)


def publish_to_ado(report_path: Path, summary_path: Path):
    """
    Emit the two VSO commands that surface reports directly inside the ADO pipeline UI.

    1. ##vso[task.uploadsummary]  — renders the HTML in the pipeline run's Extensions tab.
       Opens inline in the browser without downloading.

    2. ##vso[task.addattachment]  — attaches the markdown so it appears in the
       Build Summary tab under 'AI Code Review'.
    """
    # Extensions tab: renders the full interactive HTML dashboard inline
    vso("task.uploadsummary", str(report_path))

    # Build Summary tab: renders the markdown summary card
    vso(
        "task.addattachment type=Distributedtask.Core.Summary;name=AI Code Review",
        str(summary_path)
    )


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if not ENDPOINT or not API_KEY:
        vso("task.logissue type=error", "AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_KEY not set.")
        sys.exit(1)

    log("-" * 60)
    log(f"[AI-REVIEW] Build {BUILD_NUM}")
    log("-" * 60)

    start = datetime.datetime.utcnow()
    diff  = get_diff()
    if not diff.strip():
        log("[INFO] No .cs/.sql changes detected in this build. Skipping AI review.")
        vso("task.setvariable variable=REVIEW_SKIPPED", "true")
        return

    log(f"[INFO] Diff size: {len(diff)} chars\n")

    all_issues = []
    for key, domain in DOMAINS.items():
        log(f"[>>] Reviewing: {domain['title']} ...")
        try:
            issues = call_openai(domain["prompt"], diff)
            for i in issues:
                i["domain"] = key
            all_issues.extend(issues)
            log(f"    [OK] {len(issues)} issue(s) found")
        except Exception as e:
            log(f"    [WARN] Domain {key} failed: {e}")

    duration = int((datetime.datetime.utcnow() - start).total_seconds())
    log(f"\n[SUMMARY] Total: {len(all_issues)} issues in {duration}s\n")

    # ── Pipeline log annotations (red/yellow markers in the Logs tab) ─────────
    sev_cmd = {
        "critical": "task.logissue type=error",
        "high":     "task.logissue type=error",
        "medium":   "task.logissue type=warning",
        "low":      "task.logissue type=warning",
    }
    for issue in all_issues:
        sev = issue.get("severity", "info")
        if sev in sev_cmd:
            loc = issue.get("file", "")
            if issue.get("line") and str(issue.get("line")) != "0":
                loc += " line " + str(issue.get("line"))
            vso(sev_cmd[sev],
                "[" + issue.get("domain","") + ":" + issue.get("rule","") + "] "
                + loc + " : " + issue.get("message",""))

    # ── Write output files ────────────────────────────────────────────────────
    meta = {
        "timestamp":  datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "duration_s": duration,
    }

    # 1. Full HTML dashboard (artifact download + Extensions tab inline view)
    dashboard_html = build_dashboard(all_issues, meta)
    report_path    = Path(OUTPUT_DIR) / "code-review-report.html"
    report_path.write_text(dashboard_html, encoding="utf-8")
    log(f"[INFO] HTML dashboard saved: {report_path}")

    # 2. Markdown summary (Build Summary tab card)
    summary_md   = build_markdown_summary(all_issues)
    summary_path = Path(OUTPUT_DIR) / "code-review-summary.md"
    summary_path.write_text(summary_md, encoding="utf-8")
    log(f"[INFO] Markdown summary saved: {summary_path}")

    # 3. JSON (for downstream tooling / custom queries)
    json_path = Path(OUTPUT_DIR) / "code-review-results.json"
    json_path.write_text(json.dumps(all_issues, indent=2), encoding="utf-8")

    # ── Publish directly into the ADO pipeline UI ─────────────────────────────
    # Extensions tab  →  full HTML dashboard rendered inline
    # Build Summary tab  →  markdown summary card
    publish_to_ado(report_path, summary_path)
    log("[INFO] Report published to ADO pipeline UI (Extensions tab + Build Summary tab)")

    # ── PR comment ────────────────────────────────────────────────────────────
    if PR_ID:
        log("[INFO] Posting PR comment...")
        post_pr_comment(build_pr_comment(all_issues))
        log("[OK] PR comment posted")

    # ── Quality gate ──────────────────────────────────────────────────────────
    has_fail     = any(i.get("severity") in FAIL_ON for i in all_issues)
    counts_fail  = {s: sum(1 for i in all_issues if i.get("severity") == s) for s in FAIL_ON}
    fail_summary = ", ".join(str(v) + " " + k for k, v in counts_fail.items() if v > 0)

    if has_fail:
        log(f"\n[FAILED] Quality gate FAILED: {fail_summary} issue(s) found in FAIL_ON set: {FAIL_ON}")
        vso("task.complete result=Failed",
            "AI Code Review FAILED: " + fail_summary + " issues require attention.")
        sys.exit(1)
    else:
        log(f"\n[PASSED] Quality gate PASSED")
        vso("task.complete result=Succeeded",
            "AI Code Review PASSED: " + str(len(all_issues)) + " total issues (none critical/high).")


if __name__ == "__main__":
    main()