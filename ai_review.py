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
    """
    Generates a fully inline-styled HTML report for the ADO Extensions tab.
    Rules:
      - Zero <style> blocks or class= attributes (ADO CSP strips them)
      - All colours work on BOTH light and dark ADO themes
        -> dark backgrounds with light text throughout, no reliance on
           the host page background colour
    """
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    all_issues_sorted = sorted(
        all_issues,
        key=lambda i: severity_order.get(i.get("severity", "info"), 4)
    )

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for i in all_issues:
        sev = i.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1

    domain_counts = {}
    for i in all_issues:
        d = i.get("domain", "unknown")
        domain_counts[d] = domain_counts.get(d, 0) + 1

    total  = len(all_issues)
    passed = total == 0 or (counts["critical"] == 0 and counts["high"] == 0)

    # ── Colour tokens — all dark-theme safe ──────────────────────────────────
    # Page chrome
    BG_PAGE   = "#1e2430"   # overall page background
    BG_HEADER = "#151b27"   # top header bar
    BG_CARD   = "#252d3d"   # card / panel backgrounds
    BG_ROW    = "#1e2430"   # table row background
    BG_ROWALT = "#252d3d"   # alternate table row
    BORDER    = "#2e3a50"   # subtle border
    TXT_PRI   = "#e2e8f0"   # primary text
    TXT_SEC   = "#94a3b8"   # secondary / muted text
    TXT_MONO  = "#7dd3fc"   # monospace (file paths, rules)

    # Severity — badge bg / badge text / left-bar colour
    SEV = {
        "critical": ("#7f1d1d", "#fca5a5", "#ef4444"),
        "high":     ("#78350f", "#fcd34d", "#f59e0b"),
        "medium":   ("#1e3a5f", "#93c5fd", "#3b82f6"),
        "low":      ("#14532d", "#86efac", "#22c55e"),
        "info":     ("#1e293b", "#cbd5e1", "#64748b"),
    }

    # Domain badge colours (bg, text)
    DOMAIN_BADGE = {
        "has":  ("#7f1d1d", "#fca5a5"),  # has issues
        "none": ("#14532d", "#86efac"),  # no issues
    }

    def sev_badge(sev):
        bg, fg, _ = SEV.get(sev, SEV["info"])
        return (
            "<span style=\"display:inline-block;background:" + bg + ";color:" + fg + ";"
            "border:1px solid " + fg + "40;padding:2px 10px;border-radius:3px;"
            "font-size:11px;font-weight:700;font-family:sans-serif;"
            "letter-spacing:.04em\">" + sev.upper() + "</span>"
        )

    # ── Issue rows ───────────────────────────────────────────────────────────
    rows_html = []
    for idx, issue in enumerate(all_issues_sorted):
        sev        = issue.get("severity", "info")
        _, _, bar  = SEV.get(sev, SEV["info"])
        domain_key = issue.get("domain", "")
        domain_ttl = html.escape(DOMAINS.get(domain_key, {}).get("title", domain_key))
        file_str   = html.escape(str(issue.get("file", "")))
        line_str   = str(issue.get("line", ""))
        rule_str   = html.escape(str(issue.get("rule", "")))
        msg_str    = html.escape(str(issue.get("message", "")))
        sug_str    = html.escape(str(issue.get("suggestion", "")))
        loc        = (file_str + ":" + line_str) if (line_str and line_str != "0") else file_str
        row_bg     = BG_ROW if idx % 2 == 0 else BG_ROWALT

        sug_block = ""
        if sug_str:
            sug_block = (
                "<div style=\"margin-top:7px;padding:7px 10px;"
                "background:#0f2818;border-left:3px solid #22c55e;"
                "font-size:12px;color:#86efac;"
                "font-family:sans-serif;line-height:1.5\">"
                "<strong style=\"color:#4ade80\">Suggestion:</strong> " + sug_str + "</div>"
            )

        rows_html.append(
            "<tr style=\"background:" + row_bg + "\">"
            "<td style=\"width:4px;background:" + bar + ";padding:0;border:none\"></td>"
            "<td style=\"padding:10px 12px;vertical-align:top;"
            "border-bottom:1px solid " + BORDER + "\">"
            + sev_badge(sev) +
            "</td>"
            "<td style=\"padding:10px 12px;vertical-align:top;"
            "border-bottom:1px solid " + BORDER + ";"
            "font-size:12px;color:" + TXT_SEC + ";"
            "font-family:sans-serif\">"
            + domain_ttl +
            "</td>"
            "<td style=\"padding:10px 12px;vertical-align:top;"
            "border-bottom:1px solid " + BORDER + ";min-width:160px\">"
            "<div style=\"font-size:12px;font-weight:600;color:" + TXT_MONO + ";"
            "font-family:monospace;margin-bottom:3px\">"
            + rule_str +
            "</div>"
            "<div style=\"font-size:11px;color:" + TXT_SEC + ";"
            "font-family:monospace;word-break:break-all\">"
            + html.escape(loc) +
            "</div>"
            "</td>"
            "<td style=\"padding:10px 12px;vertical-align:top;"
            "border-bottom:1px solid " + BORDER + "\">"
            "<div style=\"font-size:13px;color:" + TXT_PRI + ";"
            "font-family:sans-serif;line-height:1.6\">"
            + msg_str +
            "</div>"
            + sug_block +
            "</td>"
            "</tr>"
        )

    issues_table = (
        "<table style=\"width:100%;border-collapse:collapse\">"
        "<thead>"
        "<tr style=\"background:" + BG_HEADER + "\">"
        "<th style=\"width:4px;padding:0;border:none\"></th>"
        "<th style=\"padding:9px 12px;text-align:left;font-size:11px;color:" + TXT_SEC + ";"
        "font-family:sans-serif;font-weight:600;letter-spacing:.05em;"
        "text-transform:uppercase;border-bottom:2px solid " + BORDER + "\">Severity</th>"
        "<th style=\"padding:9px 12px;text-align:left;font-size:11px;color:" + TXT_SEC + ";"
        "font-family:sans-serif;font-weight:600;letter-spacing:.05em;"
        "text-transform:uppercase;border-bottom:2px solid " + BORDER + "\">Domain</th>"
        "<th style=\"padding:9px 12px;text-align:left;font-size:11px;color:" + TXT_SEC + ";"
        "font-family:sans-serif;font-weight:600;letter-spacing:.05em;"
        "text-transform:uppercase;border-bottom:2px solid " + BORDER + "\">Rule / File</th>"
        "<th style=\"padding:9px 12px;text-align:left;font-size:11px;color:" + TXT_SEC + ";"
        "font-family:sans-serif;font-weight:600;letter-spacing:.05em;"
        "text-transform:uppercase;border-bottom:2px solid " + BORDER + "\">Finding &amp; Suggestion</th>"
        "</tr>"
        "</thead>"
        "<tbody>"
        + "".join(rows_html) +
        "</tbody></table>"
    ) if rows_html else (
        "<p style=\"color:#4ade80;padding:20px;"
        "font-family:sans-serif\">No issues found.</p>"
    )

    # ── Score cards ──────────────────────────────────────────────────────────
    score_cards = ""
    for sev_key, label, bar_c in [
        ("critical", "Critical", "#ef4444"),
        ("high",     "High",     "#f59e0b"),
        ("medium",   "Medium",   "#3b82f6"),
        ("low",      "Low",      "#22c55e"),
        ("info",     "Info",     "#64748b"),
    ]:
        score_cards += (
            "<td style=\"padding:0 5px\">"
            "<div style=\"background:" + BG_CARD + ";border:1px solid " + BORDER + ";"
            "border-top:3px solid " + bar_c + ";border-radius:6px;"
            "padding:14px 12px;text-align:center;min-width:88px\">"
            "<div style=\"font-size:30px;font-weight:800;color:" + bar_c + ";"
            "font-family:sans-serif;line-height:1\">"
            + str(counts[sev_key]) +
            "</div>"
            "<div style=\"font-size:10px;color:" + TXT_SEC + ";"
            "font-family:sans-serif;margin-top:5px;"
            "text-transform:uppercase;letter-spacing:.07em\">"
            + label +
            "</div>"
            "</div>"
            "</td>"
        )

    # ── Domain breakdown ─────────────────────────────────────────────────────
    domain_rows = ""
    for key, info in DOMAINS.items():
        cnt    = domain_counts.get(key, 0)
        plural = "s" if cnt != 1 else ""
        if cnt == 0:
            badge_bg, badge_fg = DOMAIN_BADGE["none"]
        else:
            badge_bg, badge_fg = DOMAIN_BADGE["has"]
        domain_rows += (
            "<tr>"
            "<td style=\"padding:7px 12px;font-size:12px;"
            "font-family:sans-serif;color:" + TXT_PRI + ";"
            "border-bottom:1px solid " + BORDER + "\">"
            + html.escape(info.get("title", key)) +
            "</td>"
            "<td style=\"padding:7px 12px;border-bottom:1px solid " + BORDER + ";text-align:right\">"
            "<span style=\"display:inline-block;background:" + badge_bg + ";color:" + badge_fg + ";"
            "font-size:10px;font-weight:700;font-family:sans-serif;"
            "padding:2px 8px;border-radius:10px\">"
            + str(cnt) + " issue" + plural +
            "</span>"
            "</td>"
            "</tr>"
        )

    # ── Header meta ──────────────────────────────────────────────────────────
    status_text  = "PASSED" if passed else "FAILED"
    status_bg    = "#0f2818" if passed else "#2d0f0f"
    status_color = "#4ade80" if passed else "#f87171"
    status_bdr   = "#22c55e" if passed else "#ef4444"
    status_icon  = "[PASSED]" if passed else "[FAILED]"
    if passed:
        status_detail = "No critical or high severity issues found."
    else:
        status_detail = (
            str(counts["critical"]) + " critical, " +
            str(counts["high"]) + " high issue(s) require attention."
        )

    escaped_build  = html.escape(BUILD_NUM)
    escaped_branch = html.escape(BRANCH)
    escaped_repo   = html.escape(REPO_NAME)
    escaped_ts     = html.escape(meta.get("timestamp", ""))
    escaped_dur    = html.escape(str(meta.get("duration_s", "")))

    # ── Assemble ─────────────────────────────────────────────────────────────
    return (
        "<!DOCTYPE html>"
        "<html lang=\'en\'><head>"
        "<meta charset=\'UTF-8\'>"
        "<meta name=\'viewport\' content=\'width=device-width,initial-scale=1\'>"
        "<title>AI Code Review</title>"
        "</head>"
        "<body style=\'margin:0;padding:0;background:" + BG_PAGE + ";font-family:sans-serif\'>"

        # ── Header ──────────────────────────────────────────────────────────
        "<div style=\'background:" + BG_HEADER + ";"
        "padding:18px 24px;border-bottom:1px solid " + BORDER + "\'>"
        "<div style=\'font-size:17px;font-weight:700;color:" + TXT_PRI + ";"
        "margin-bottom:8px\'>AI Code Review Dashboard</div>"
        "<div style=\'font-size:12px;color:" + TXT_SEC + ";margin-bottom:12px\'>"
        "Build: <strong style=\'color:" + TXT_PRI + "\'>" + escaped_build + "</strong>"
        " &nbsp;|&nbsp; "
        "Branch: <strong style=\'color:" + TXT_PRI + "\'>" + escaped_branch + "</strong>"
        " &nbsp;|&nbsp; "
        "Repo: <strong style=\'color:" + TXT_PRI + "\'>" + escaped_repo + "</strong>"
        " &nbsp;|&nbsp; " + escaped_ts +
        " &nbsp;|&nbsp; " + escaped_dur + "s analysis"
        "</div>"
        "<div style=\'display:inline-block;background:" + status_bg + ";"
        "color:" + status_color + ";border:1px solid " + status_bdr + ";"
        "padding:6px 14px;border-radius:4px;font-size:13px;font-weight:700;"
        "font-family:sans-serif\'>"
        + status_icon + " Review " + status_text + " &mdash; " + status_detail +
        "</div>"
        "</div>"

        # ── Body ─────────────────────────────────────────────────────────────
        "<div style=\'padding:20px 24px;background:" + BG_PAGE + "\'>"

        # Score cards
        "<div style=\'margin-bottom:22px\'>"
        "<div style=\'font-size:11px;font-weight:600;color:" + TXT_SEC + ";"
        "text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px\'>Issue Summary</div>"
        "<table style=\'border-collapse:collapse\'><tr>" + score_cards + "</tr></table>"
        "</div>"

        # Two-column: domain breakdown + issues table
        "<table style=\'width:100%;border-collapse:collapse;vertical-align:top\'><tr>"

        # Left: domain breakdown
        "<td style=\'width:210px;vertical-align:top;padding-right:18px\'>"
        "<div style=\'font-size:11px;font-weight:600;color:" + TXT_SEC + ";"
        "text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px\'>Domain Breakdown</div>"
        "<table style=\'width:100%;border-collapse:collapse;"
        "background:" + BG_CARD + ";border:1px solid " + BORDER + ";border-radius:6px\'>"
        "<tbody>" + domain_rows + "</tbody></table>"
        "</td>"

        # Right: issues
        "<td style=\'vertical-align:top\'>"
        "<div style=\'font-size:11px;font-weight:600;color:" + TXT_SEC + ";"
        "text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px\'>"
        "All Issues (" + str(total) + " total)</div>"
        "<div style=\'border:1px solid " + BORDER + ";border-radius:6px;overflow:hidden\'>"
        + issues_table +
        "</div>"
        "</td>"
        "</tr></table>"

        "</div>"   # /body padding
        "</body></html>"
    )

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
