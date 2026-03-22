# CodeReviewWithAI
AI-Powered Code Review: Automating Quality, Security &amp; Excellence across Every Pull Request

Code review is one of the most valuable practices in software engineering. It catches bugs before they ship, spreads knowledge across the team, and enforces quality standards. But it also has a dirty secret: it is slow, inconsistent, and heavily dependent on who is doing the reviewing on any given day.
I have been working on a solution to this problem — integrating a fully automated AI code review system directly into our CI/CD pipeline on Azure DevOps. Every time a developer opens a pull request, eight specialised AI reviewers go to work on the diff simultaneously, checking everything from SOLID architecture principles to SQL injection vulnerabilities to N+1 query patterns. The results surface directly inside Azure DevOps — no downloads, no external tools, no changes to the developer workflow.

<img width="950" height="505" alt="image" src="https://github.com/user-attachments/assets/5ccaa223-440c-46c7-ba5a-c086b38b9e9e" />

# How It Works: Technical Architecture
## The Pipeline Flow
The solution is a Python script (ai_review.py) that lives in a scripts/ folder at the root of the repository, alongside a Markdown file (review_prompts.md) that contains all the domain prompts. The pipeline calls the script as a Command Line task after the build step.
Here is the sequence of events for every PR build: <br/>
•	Git diff is extracted — only .cs and .sql files, with 8 lines of context per hunk <br/>
•	The diff is truncated to a configurable character limit (default 15,000 chars) to stay within token budget <br/>
•	The script loops through all eight domains, calling Azure OpenAI once per domain <br/>
•	Each response is parsed from JSON and normalised — the domain key is stamped onto each finding <br/>
•	All findings are merged, sorted by severity, and written to an HTML dashboard file and a JSON file <br/>
•	The HTML dashboard path is emitted as ##vso[task.uploadsummary] — this renders it inline in the Extensions tab of the pipeline run <br/>
•	A Markdown summary is emitted as ##vso[task.addattachment] — this renders as a card in the Build Summary tab <br/>
•	If a PR is active, a formatted comment is posted to it via the Azure DevOps REST API <br/>
•	If any finding has a severity in the REVIEW_FAIL_ON set (default: critical, high), the script calls sys.exit(1) — failing the build <br/>


# Seeing Results Inside Azure DevOps
One of the most important product decisions was keeping results native to Azure DevOps. Nobody wants to download a report, open a separate dashboard, or switch to another tool. Results should appear where the developer already is.
<img width="924" height="510" alt="image" src="https://github.com/user-attachments/assets/0a63590f-8013-465f-a4c0-fba259aaa703" />

# How to Get Started
If you want to adopt this in your own team, the setup is simpler than it sounds. The entire solution is two files — ai_review.py and review_prompts.md — that drop into any repository under a scripts/ folder. No new infrastructure, no marketplace extensions, no change to your existing pipeline tasks.
The three things you need to provision: <br/>
•	An Azure OpenAI resource with a GPT-4o deployment <br/>
•	A Variable Group in Azure DevOps Library with five secrets (endpoint, key, deployment name, DevOps PAT, and the fail-on setting) <br/>
•	One new Command Line task in your Classic pipeline (or a step in YAML) that calls python scripts/ai_review.py <br/> <br/>
Total setup time for an experienced Azure DevOps user is around 30 to 45 minutes for the first repository. Subsequent repositories take about 10 minutes each — copy the two files, link the variable group, add the task. <br/><br/>
<i>Suggestion: Start with REVIEW_FAIL_ON=info for the first week. Observe the findings. Tune the prompts for your team's conventions. Then escalate to critical,high when you are confident in the signal. </i>
