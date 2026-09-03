#!/usr/bin/env python3
"""Remove expired job ads from the board and open a pull request with the result.

The job board promises that openings older than three months are taken down.
This runs weekly and does that, as a pull request rather than a direct push, so
the repository's four-eyes rule still applies.

Ads whose `Date` is missing or unreadable are never removed; they are listed in
the PR description for a human to deal with.

Environment:
  REPO           owner/name of the repository
  GH_TOKEN       token for the `gh` CLI
  BASE_BRANCH    branch to open the PR against (default: the checked out one)
  BRANCH         branch to push to (default: chore/prune-job-board)
  DRY_RUN=1      report what would happen and change nothing
  MAX_AGE_DAYS   override .github/jobs-board.json
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jobs_board as jb

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def run(cmd: list[str], **kwargs) -> str:
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True, **kwargs
    ).stdout.strip()


def describe(ad: jb.Ad, today: dt.date) -> str:
    age = ad.age_days(today)
    when = ad.date.isoformat() if ad.date else "no readable date"
    suffix = f", {age} days old" if age is not None else ""
    return f"* `{ad.heading}` (posted {when}{suffix})"


def open_or_update_pr(repo: str, branch: str, base: str, title: str, body: str) -> None:
    existing = run(
        ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open",
         "--json", "number", "--jq", ".[0].number"]
    )
    if existing:
        run(["gh", "pr", "edit", existing, "--repo", repo, "--title", title, "--body", body])
        print(f"Updated existing pull request #{existing}.")
    else:
        run(["gh", "pr", "create", "--repo", repo, "--base", base, "--head", branch,
             "--title", title, "--body", body])
        print("Opened a new pull request.")


def main() -> int:
    config = jb.load_config()
    limits = config["limits"]
    messages = config["messages"]
    path = config["job_file"]
    max_age = limits["max_age_days"]

    dry_run = os.environ.get("DRY_RUN", "") not in ("", "0", "false")
    repo = os.environ.get("REPO", "")
    branch = os.environ.get("BRANCH", "chore/prune-job-board")

    today = dt.date.today()
    with open(path, encoding="utf-8") as handle:
        original = handle.read()

    doc = jb.parse(original, limits)
    if doc.separator_line is None:
        print(
            f"{path} has no `---` separator, so no ads could be read. "
            "Refusing to touch the file.",
            file=sys.stderr,
        )
        return 1

    expired, kept, unreadable = [], [], []
    for ad in doc.ads:
        age = ad.age_days(today)
        if age is None:
            unreadable.append(ad)
            kept.append(ad)
        elif age > max_age:
            expired.append(ad)
        else:
            kept.append(ad)

    if not expired:
        print(messages["prune_nothing_to_do"].format(max_age_days=max_age))
        return 0

    print(f"Expired ({len(expired)}):")
    for ad in expired:
        print(" ", describe(ad, today))

    updated = jb.rebuild(doc, kept)
    if updated == original:
        print("Rebuilt file is unchanged. Nothing to do.")
        return 0

    kept_note = ""
    if unreadable:
        kept_note = messages["prune_kept_note"].format(
            kept_list="\n".join(describe(ad, today) for ad in unreadable)
        )
    body = messages["prune_pr_body"].format(
        max_age_days=max_age,
        removed_list="\n".join(describe(ad, today) for ad in expired),
        kept_note=kept_note,
    )
    title = messages["prune_pr_title"]

    if dry_run:
        print("\n--- DRY RUN, nothing written ---\n")
        print(f"{title}\n\n{body}")
        return 0

    base = os.environ.get("BASE_BRANCH") or run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(updated)

    run(["git", "config", "user.name", BOT_NAME])
    run(["git", "config", "user.email", BOT_EMAIL])
    run(["git", "checkout", "-B", branch])
    run(["git", "add", path])
    run(["git", "commit", "-m", f"{title}\n\nOpened automatically by the prune-jobs workflow."])
    # The branch is a scratch branch owned by this workflow, recreated from the
    # default branch on every run, so overwriting it is intended.
    run(["git", "push", "--force", "origin", f"HEAD:{branch}"])

    open_or_update_pr(repo, branch, base, title, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
