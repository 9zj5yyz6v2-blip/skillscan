#!/usr/bin/env python3
"""Issue-driven scan bot for SkillScan.

Reads a public GitHub repo URL from the issue body, shallow-clones it,
runs the STATIC scanner (the scanned code is never executed), and posts
a markdown report as an issue comment via the `gh` CLI.

Environment:
    GH_TOKEN       -- provided by GitHub Actions
    ISSUE_NUM      -- issue number
    ISSUE_BODY     -- raw issue body (must contain a github.com repo URL)
    ISSUE_AUTHOR   -- issue author login
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skillscan import load_rules, scan_path  # noqa: E402

URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
MAX_FINDINGS = 12
MAX_REPOS_PER_RUN = 1  # one scan per issue


def gh(*args: str) -> None:
    subprocess.run(["gh", *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def post_comment(num: str, body: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     encoding="utf-8") as f:
        f.write(body)
        path = f.name
    try:
        gh("issue", "comment", num, "--body-file", path)
    finally:
        os.unlink(path)


def extract_repo(body: str):
    m = URL_RE.search(body or "")
    if not m:
        return None
    full = m.group(1).rstrip(".")
    if full.lower().endswith(".git"):
        full = full[:-4]
    return full


def render(full: str, res: dict) -> str:
    score, grade = res.get("score", "?"), res.get("grade", "?")
    counts = res.get("counts", {})
    crit, high = counts.get("critical", 0), counts.get("high", 0)
    med = counts.get("medium", 0)

    lines = [
        f"## 🛡️ SkillScan report for [`{full}`]({res.get('repo_url', 'https://github.com/' + full)})",
        "",
        f"**Grade {grade} · {score}/100** — critical **{crit}** · high **{high}** · medium **{med}** · ruleset {res.get('ruleset_version', '?')}",
        "",
    ]
    if crit or high:
        lines += [
            "### Top findings",
            "",
            "| Severity | Rule | Where | What matched |",
            "|---|---|---|---|",
        ]
        for h in [x for x in res.get("findings", [])
                  if x.get("severity") in ("critical", "high")][:MAX_FINDINGS]:
            msg = (h.get("message") or "").replace("|", "\\|")
            lines.append(
                f"| {h['severity']} | {h.get('rule')} | `{h.get('file')}:{h.get('line')}` | {msg} |")
        lines.append("")

    lines += [
        "---",
        "### How to read this",
        "",
        "- This is a **static first-pass screen**, not an accusation. In our 330-repo study, "
        "**62.5% of repos with critical hits turned out to be false positives** — CI scripts, "
        "test fixtures, docs, and plugin manifests trigger the same keywords as real attacks.",
        "- Findings in docs / CI / tests are severity-capped (ruleset v0.3.1+), but only a human "
        "can judge intent. A clean scan is not a security guarantee either.",
        "- Every rule maps to a public standard (CWE / OWASP LLM Top 10 / MITRE ATLAS); "
        "our full false-positive review process is public: "
        "[REGRESSION.md](https://github.com/9zj5yyz6v2-blip/skillscan/blob/main/skillscan/REGRESSION.md).",
        "",
        "Want to scan locally instead? `python skillscan/skillscan.py path/to/skill` — "
        "zero dependencies, Python 3.8+.",
        "",
        f"/cc @{os.environ.get('ISSUE_AUTHOR', '')} — thanks for trying SkillScan! "
        "Feedback (false positives included) makes the ruleset sharper: reply here any time.",
    ]
    return "\n".join(lines)


def main() -> int:
    num = os.environ.get("ISSUE_NUM", "")
    body = os.environ.get("ISSUE_BODY", "")
    full = extract_repo(body)

    if not full:
        post_comment(num, (
            "⚠️ I couldn't find a GitHub repo URL in this issue. "
            "Please edit the issue to include a link like `https://github.com/owner/repo` "
            "and the scan will run on the next edit-triggered run, or just open a new one."))
        return 0

    with tempfile.TemporaryDirectory(prefix="skillscan-target-") as tmp:
        dest = str(Path(tmp) / "target")
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--single-branch",
                 f"https://github.com/{full}.git", dest],
                check=True, timeout=300,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            post_comment(num, (
                f"⚠️ Failed to clone `https://github.com/{full}` "
                "(private, deleted, renamed, or too large). "
                "Double-check the URL and open a new scan request."))
            return 0

        try:
            rules = load_rules()
            res = scan_path(dest, rules)
        except Exception as e:  # noqa: BLE001
            post_comment(num, (
                f"⚠️ Scan failed with an internal error: `{type(e).__name__}`. "
                "This is our bug, not yours — please open a new scan request later, "
                "or run the scanner locally: "
                "`python skillscan/skillscan.py path/to/skill`."))
            return 0

    post_comment(num, render(full, res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
