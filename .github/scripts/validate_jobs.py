#!/usr/bin/env python3
"""Validate job board changes in a pull request and explain any problems.

Only the job ads that this PR actually touches are reported on, so a
pre-existing problem elsewhere in the file never blocks an unrelated
submission. Findings are posted as a single, self-updating PR comment written
for someone who may be editing Markdown for the first time.

The PR's content is only ever read as *data* (`git show` / `git diff`); nothing
from the pull request is executed. See the security note in
.github/workflows/validate-jobs.yml.

Environment:
  REPO           owner/name of the repository
  PR_NUMBER      pull request number
  BASE_SHA       commit the PR is based on
  HEAD_SHA       tip of the PR (or a ref such as refs/remotes/pr/head)
  GH_TOKEN       token for the `gh` CLI
  DRY_RUN=1      print the comment instead of posting it (for local testing)
  MAX_BODY_CHARS, DATE_SKEW_DAYS, MAX_AGE_DAYS   override .github/jobs-board.json
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jobs_board as jb

MARKER = "<!-- devstaff-jobs-validator -->"
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def run(cmd: list[str], **kwargs) -> str:
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True, **kwargs
    ).stdout


def file_at(ref: str, path: str) -> str | None:
    """Contents of `path` at `ref`, or None if it does not exist there."""
    try:
        return run(["git", "show", f"{ref}:{path}"])
    except subprocess.CalledProcessError:
        return None


def parse_diff(diff: str) -> tuple[set[int], list[int]]:
    """Split a unified=0 diff into added lines and pure-deletion points.

    Returns the head-side line numbers this PR added or modified, plus the
    anchors of hunks that only removed lines. An anchor `n` means the deleted
    lines used to sit between head lines `n` and `n + 1`.
    """
    added: set[int] = set()
    deletions: list[int] = []
    for line in diff.splitlines():
        match = HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        if count == 0:
            deletions.append(start)
        else:
            added.update(range(start, start + count))
    return added, deletions


def changed_lines(base: str, head: str, path: str) -> tuple[set[int], list[int]]:
    """As parse_diff, for one file between two commits.

    Three-dot, so commits that landed on the base branch after the PR was
    opened are not mistaken for the contributor's own changes.
    """
    return parse_diff(run(["git", "diff", "--unified=0", f"{base}...{head}", "--", path]))


def is_touched(ad: jb.Ad, added: set[int], deletions: list[int]) -> bool:
    """Whether this PR changed this particular ad.

    Deletions need care. Removing a row from an ad breaks the ad that remains,
    so that has to count as touching it - but removing a whole ad leaves the
    neighbouring ads untouched, and blaming a neighbour for a pre-existing
    problem would block someone who is only tidying up the board. So a deletion
    counts only when it happened *inside* an ad: when the lines either side of
    the deletion point both still belong to the same ad.
    """
    if any(n in added for n in range(ad.start_line, ad.end_line + 1)):
        return True
    return any(ad.start_line <= n and n + 1 <= ad.end_line for n in deletions)


def is_new(ad: jb.Ad, added: set[int]) -> bool:
    """True if every meaningful line of this ad was added by the PR."""
    return all(
        n in added
        for n, raw in zip(range(ad.start_line, ad.end_line + 1), ad.raw_lines)
        if raw.strip()
    )


def blob_link(repo: str, sha: str, path: str, line: int) -> str:
    return f"https://github.com/{repo}/blob/{sha}/{path}#L{line}"


def build_comment(config, doc, reported, warnings, repo, sha, path, ambient) -> str:
    messages = config["messages"]
    boilerplate = {k: jb.fill(messages[k], ambient) for k in
                   ("comment_title", "intro", "warnings_heading",
                    "warnings_note", "format_example", "outro")}
    out = [MARKER, boilerplate["comment_title"], "", boilerplate["intro"], ""]

    for ad, issues in reported:
        if ad is None:
            label = f"[`{path}` line {issues[0].line}]({blob_link(repo, sha, path, issues[0].line)})"
        else:
            label = f"[`{ad.heading or '(untitled job ad)'}`]({blob_link(repo, sha, path, ad.start_line)})"
        out.append(f"### {label}")
        out.append("")
        for issue in issues:
            out.append(f"* {jb.render(config, issue, ambient)}")
        out.append("")

    if warnings:
        out.append(boilerplate["warnings_heading"])
        out.append("")
        out.append(boilerplate["warnings_note"])
        out.append("")
        for ad, issue in warnings:
            where = f"`{ad.heading}` — " if ad is not None and ad.heading else ""
            out.append(f"* {where}{jb.render(config, issue, ambient)}")
        out.append("")

    out.append(boilerplate["format_example"])
    out.append("")
    out.append(boilerplate["outro"])
    return "\n".join(out)


def find_comment(repo: str, pr: str) -> str | None:
    ids = run(
        [
            "gh", "api", f"repos/{repo}/issues/{pr}/comments", "--paginate",
            "--jq", f'.[] | select(.body | startswith("{MARKER}")) | .id',
        ]
    ).split()
    return ids[0] if ids else None


def post_or_update(repo: str, pr: str, body: str) -> None:
    payload = json.dumps({"body": body})
    existing = find_comment(repo, pr)
    if existing:
        run(
            ["gh", "api", f"repos/{repo}/issues/comments/{existing}",
             "--method", "PATCH", "--input", "-"],
            input=payload,
        )
        print(f"Updated existing comment {existing}.")
    else:
        run(
            ["gh", "api", f"repos/{repo}/issues/{pr}/comments",
             "--method", "POST", "--input", "-"],
            input=payload,
        )
        print("Posted new comment.")


def clear_comment(repo: str, pr: str, success: str) -> None:
    """Replace a previous failure comment with the success text.

    Nothing is posted if the PR was clean all along, so a well-formed
    submission never gets a bot comment at all.
    """
    existing = find_comment(repo, pr)
    if not existing:
        print("Nothing to report and no previous comment. Staying quiet.")
        return
    run(
        ["gh", "api", f"repos/{repo}/issues/comments/{existing}",
         "--method", "PATCH", "--input", "-"],
        input=json.dumps({"body": MARKER + "\n" + success}),
    )
    print(f"Cleared previous comment {existing}.")


def main() -> int:
    config = jb.load_config()
    limits = config["limits"]
    path = config["job_file"]

    repo = os.environ.get("REPO", "")
    pr = os.environ.get("PR_NUMBER", "")
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "HEAD")
    dry_run = os.environ.get("DRY_RUN", "") not in ("", "0", "false")

    text = file_at(head, path)
    if text is None:
        print(f"{path} does not exist in this PR. Nothing to validate.")
        return 0

    today = dt.date.today()
    ambient = {
        "today": today.isoformat(),
        "max_age_days": limits["max_age_days"],
        "max_body_chars": limits["max_body_chars"],
        "date_skew_days": limits["date_skew_days"],
    }

    doc = jb.parse(text, limits)
    added, deletions = changed_lines(base, head, path) if base else (set(), [])

    # File-level problems are always reported: if the file will not parse there
    # are no ads left to attach anything to.
    reported: list[tuple[jb.Ad | None, list[jb.Issue]]] = []
    if doc.file_issues:
        reported.append((None, doc.file_issues))

    warnings: list[tuple[jb.Ad, jb.Issue]] = []
    seen_existing_ad = False
    for ad in doc.ads:
        touched = is_touched(ad, added, deletions)
        new = is_new(ad, added)

        issues = list(ad.issues) if touched else []

        if new and seen_existing_ad:
            issues.append(jb.Issue("not_at_top", ad.start_line))
        if not new:
            seen_existing_ad = True

        if new and ad.date is not None:
            skew = abs((ad.date - today).days)
            if skew > limits["date_skew_days"]:
                warnings.append(
                    (ad, jb.Issue("date_skew", ad.date_line or ad.start_line,
                                  {"value": ad.date.isoformat(), "days": skew}))
                )

        if issues:
            reported.append((ad, issues))

    errors = sum(len(issues) for _, issues in reported)

    if not errors and not warnings:
        if dry_run:
            print("No problems found.")
        else:
            clear_comment(repo, pr, config["messages"]["success"])
        return 0

    body = build_comment(config, doc, reported, warnings, repo, head, path, ambient)
    if dry_run:
        print(body)
    else:
        post_or_update(repo, pr, body)

    print(f"\n{errors} error(s), {len(warnings)} warning(s).", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
