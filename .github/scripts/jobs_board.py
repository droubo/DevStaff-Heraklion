"""Parser for the DevStaff job board file (``jobs/README.md``).

The board is a plain Markdown file that is edited by hand, usually through the
GitHub web UI, so it is parsed literally line by line rather than with a real
Markdown library. The format is:

    <introduction text>

    ---

    ## <Job Title> @ <Company>

    | Link | [Apply](https://... or mailto:...) |
    |------|-----|
    | Date | YYYY-MM-DD |

    <short description, no `#` or `##` headings>

    ## <next job>
    ...

This module only *finds* problems; it does not word them. Every problem is
returned as an ``Issue`` carrying a code plus the values needed to fill in the
matching message template from ``.github/jobs-board.json``, so that the
validator and the pruner share one vocabulary and one set of texts.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / ".github" / "jobs-board.json"

# A job ad heading: exactly two hashes. `##Title` (no space) is deliberately
# matched too, so it is reported as a malformed heading instead of silently
# becoming part of the previous ad's description.
HEADING_RE = re.compile(r"^##(?!#)\s*(.*?)\s*$")
# Headings that are not allowed inside a description. `##` is unreachable here
# because it starts a new ad, but it is matched anyway to stay honest.
BIG_HEADING_RE = re.compile(r"^#{1,2}(?!#)")
TABLE_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")
LINK_RE = re.compile(r"^\[([^\]]*)\]\(\s*(\S+?)\s*\)$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ALLOWED_LINK_SCHEMES = ("https://", "mailto:")
REQUIRED_LINK_LABEL = "Apply"


@dataclass
class Issue:
    """One problem found in the file.

    ``code`` selects a message template, ``line`` is 1-indexed and points at the
    most useful line to look at, and ``fields`` fills the template's
    placeholders.
    """

    code: str
    line: int
    fields: dict = field(default_factory=dict)


@dataclass
class Ad:
    """A single job opening.

    ``start_line`` and ``end_line`` are 1-indexed and inclusive, and span the
    heading down to the last non-blank line of the description. Trailing blank
    lines are excluded so that the blank line between two ads is not counted as
    belonging to both.
    """

    start_line: int
    end_line: int
    raw_lines: list[str]
    heading: str = ""
    role: str = ""
    company: str = ""
    link_url: str | None = None
    date: dt.date | None = None
    date_line: int | None = None
    body: str = ""
    issues: list[Issue] = field(default_factory=list)

    def age_days(self, today: dt.date) -> int | None:
        """How many days ago this ad was posted, or None if the date is unusable."""
        if self.date is None:
            return None
        return (today - self.date).days


@dataclass
class Document:
    lines: list[str]
    separator_line: int | None = None
    ads: list[Ad] = field(default_factory=list)
    file_issues: list[Issue] = field(default_factory=list)

    def all_issues(self) -> list[Issue]:
        return self.file_issues + [i for ad in self.ads for i in ad.issues]


def load_config(path: str | os.PathLike | None = None) -> dict:
    """Load jobs-board.json, letting environment variables override the limits.

    Each key under ``limits`` may be overridden by an environment variable of
    the same name in upper case, which is how the workflows expose the knobs
    without anyone having to edit Python.
    """
    config_path = Path(path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH)
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)

    for key, default in config.get("limits", {}).items():
        raw = os.environ.get(key.upper(), "").strip()
        if not raw:
            continue
        try:
            config["limits"][key] = int(raw)
        except ValueError:
            raise SystemExit(
                f"Environment variable {key.upper()}={raw!r} is not a whole number "
                f"(the default is {default})."
            )
    return config


def fill(template: str, values: dict) -> str:
    """Substitute placeholders, falling back to the raw text on a bad template.

    A mis-edited message should never take the whole check down: showing the
    unsubstituted text is far more useful to a contributor than a stack trace.
    """
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template


def render(config: dict, issue: Issue, ambient: dict | None = None) -> str:
    """Turn an Issue into the prose a contributor reads."""
    messages = config.get("messages", {})
    template = (
        messages.get("errors", {}).get(issue.code)
        or messages.get("warnings", {}).get(issue.code)
    )
    if template is None:
        return f"`{issue.code}` (no message configured for this problem)"

    values = dict(ambient or {})
    values.update(issue.fields)
    return fill(template, values)


def _split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    # A trailing newline produces a final empty element that is not a real line.
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _find_separator(lines: list[str]) -> int | None:
    """Index of the `---` line that divides the introduction from the ads.

    Requires a blank line above, and a blank line below unless it is the last
    line of the file - a board with every ad pruned ends right after it. Index
    0 is refused so that YAML front matter could never be mistaken for it.
    """
    for i in range(1, len(lines)):
        if lines[i].strip() != "---" or lines[i - 1].strip() != "":
            continue
        if i + 1 >= len(lines) or lines[i + 1].strip() == "":
            return i
    return None


def _parse_table(block: list[str], block_start: int) -> tuple[dict, int]:
    """Read the details table at the top of an ad block.

    Returns a mapping of lower-cased row label -> (value, 1-indexed line), plus
    the block-relative index of the first line after the table.
    """
    i = 0
    while i < len(block) and block[i].strip() == "":
        i += 1

    rows: dict[str, tuple[str, int]] = {}
    while i < len(block) and block[i].lstrip().startswith("|"):
        cells = [c.strip() for c in block[i].strip().strip("|").split("|")]
        is_separator_row = bool(cells) and all(
            TABLE_SEP_CELL_RE.match(c) for c in cells
        )
        if not is_separator_row and len(cells) >= 2:
            key = cells[0].casefold()
            rows.setdefault(key, (cells[1], block_start + i))
        i += 1
    return rows, i


def _check_link(ad: Ad, cell: str, line: int) -> None:
    match = LINK_RE.match(cell)
    if not match:
        ad.issues.append(Issue("bad_link_format", line, {"cell": cell}))
        return

    label, url = match.group(1), match.group(2)
    if label.strip() != REQUIRED_LINK_LABEL:
        ad.issues.append(Issue("bad_link_label", line, {"label": label}))
    if not url.startswith(ALLOWED_LINK_SCHEMES):
        ad.issues.append(Issue("bad_link_scheme", line, {"url": url}))
    else:
        ad.link_url = url


def _check_date(ad: Ad, cell: str, line: int) -> None:
    ad.date_line = line
    if not ISO_DATE_RE.match(cell):
        ad.issues.append(Issue("bad_date_format", line, {"value": cell}))
        return
    try:
        ad.date = dt.date.fromisoformat(cell)
    except ValueError:
        ad.issues.append(Issue("impossible_date", line, {"value": cell}))


def _check_body(ad: Ad, body_lines: list[str], body_start: int, max_body_chars: int) -> None:
    ad.body = "\n".join(body_lines).strip()

    if not ad.body:
        ad.issues.append(Issue("body_empty", ad.start_line))
        return

    if len(ad.body) > max_body_chars:
        ad.issues.append(
            Issue(
                "body_too_long",
                ad.start_line,
                {"count": len(ad.body), "max": max_body_chars},
            )
        )

    for offset, line in enumerate(body_lines):
        if BIG_HEADING_RE.match(line):
            ad.issues.append(
                Issue(
                    "body_has_top_heading",
                    body_start + offset,
                    {"line": body_start + offset, "text": line.strip()},
                )
            )


def _parse_ad(block: list[str], start_line: int, max_body_chars: int) -> Ad:
    # Trailing blank lines belong to the gap between ads, not to this ad.
    trimmed = list(block)
    while trimmed and trimmed[-1].strip() == "":
        trimmed.pop()

    ad = Ad(
        start_line=start_line,
        end_line=start_line + len(trimmed) - 1,
        raw_lines=trimmed,
    )

    heading_match = HEADING_RE.match(trimmed[0])
    ad.heading = heading_match.group(1) if heading_match else trimmed[0].strip()
    parts = [p.strip() for p in ad.heading.split(" @ ")]
    if len(parts) == 2 and all(parts):
        ad.role, ad.company = parts
    else:
        ad.issues.append(Issue("bad_heading_format", start_line, {"heading": ad.heading}))

    rest = trimmed[1:]
    rows, body_offset = _parse_table(rest, start_line + 1)
    if not rows:
        ad.issues.append(Issue("missing_table", start_line))
    else:
        if "link" in rows:
            _check_link(ad, *rows["link"])
        else:
            ad.issues.append(Issue("missing_link_row", start_line))

        if "date" in rows:
            _check_date(ad, *rows["date"])
        else:
            ad.issues.append(Issue("missing_date_row", start_line))

    _check_body(ad, rest[body_offset:], start_line + 1 + body_offset, max_body_chars)
    return ad


def parse(text: str, limits: dict | None = None) -> Document:
    """Parse the whole job board file."""
    limits = limits or {}
    max_body_chars = int(limits.get("max_body_chars", 1024))

    lines = _split_lines(text)
    doc = Document(lines=lines)

    separator = _find_separator(lines)
    if separator is None:
        doc.file_issues.append(Issue("missing_separator", 1))
        return doc
    doc.separator_line = separator + 1

    # Everything below the separator must belong to an ad.
    body_region = lines[separator + 1 :]
    first_heading = next(
        (i for i, line in enumerate(body_region) if HEADING_RE.match(line)), None
    )
    stray_end = len(body_region) if first_heading is None else first_heading
    for i in range(stray_end):
        if body_region[i].strip():
            line_no = separator + 2 + i
            doc.file_issues.append(
                Issue("stray_content_before_first_ad", line_no, {"line": line_no})
            )
            break

    if first_heading is None:
        return doc

    # Split the region into one block per `##` heading.
    starts = [
        i
        for i in range(first_heading, len(body_region))
        if HEADING_RE.match(body_region[i])
    ]
    bounds = starts + [len(body_region)]
    for n, start in enumerate(starts):
        block = body_region[start : bounds[n + 1]]
        doc.ads.append(_parse_ad(block, separator + 2 + start, max_body_chars))

    seen: dict[str, Ad] = {}
    for ad in doc.ads:
        key = " ".join(ad.heading.split()).casefold()
        if key in seen:
            ad.issues.append(Issue("duplicate_title", ad.start_line, {"title": ad.heading}))
        else:
            seen[key] = ad

    return doc


def rebuild(doc: Document, ads: list[Ad]) -> str:
    """Re-emit the file with only ``ads`` kept, preserving everything verbatim.

    Used by the pruner. Passing every ad back in reproduces the original file
    byte for byte, as long as it was already normalised (one blank line between
    ads, single trailing newline).
    """
    if doc.separator_line is None:
        raise ValueError("cannot rebuild a file with no `---` separator")

    out = list(doc.lines[: doc.separator_line])
    for ad in ads:
        out.append("")
        out.extend(ad.raw_lines)
    return "\n".join(out) + "\n"
