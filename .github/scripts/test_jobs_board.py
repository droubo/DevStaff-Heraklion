#!/usr/bin/env python3
"""Tests for the job board parser.

Run with:  python3 -m unittest discover -s .github/scripts -p 'test_*.py'
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jobs_board as jb

PREAMBLE = "# DevStaff Community Job Openings\n\nSome introduction text.\n\n---\n"
LIMITS = {"max_body_chars": 1024}

GOOD_AD = """
## Senior Widget Engineer @ Acme

| Link | [Apply](https://acme.example/careers/1) |
|------|-----|
| Date | 2026-09-03 |

Acme builds widgets. We would like you to build some too.
"""


def board(*ads: str) -> str:
    return PREAMBLE + "".join(ads)


def codes(doc: jb.Document) -> list[str]:
    return [issue.code for issue in doc.all_issues()]


class ParseGoodBoard(unittest.TestCase):
    def test_clean_board_has_no_issues(self):
        doc = jb.parse(board(GOOD_AD), LIMITS)
        self.assertEqual(codes(doc), [])
        self.assertEqual(len(doc.ads), 1)

    def test_fields_are_extracted(self):
        ad = jb.parse(board(GOOD_AD), LIMITS).ads[0]
        self.assertEqual(ad.role, "Senior Widget Engineer")
        self.assertEqual(ad.company, "Acme")
        self.assertEqual(ad.link_url, "https://acme.example/careers/1")
        self.assertEqual(ad.date, dt.date(2026, 9, 3))

    def test_mailto_links_are_accepted(self):
        doc = jb.parse(board(GOOD_AD.replace("https://acme.example/careers/1",
                                             "mailto:jobs@acme.example")), LIMITS)
        self.assertEqual(codes(doc), [])

    def test_several_ads_are_separated(self):
        second = GOOD_AD.replace("Senior Widget Engineer", "Junior Widget Engineer")
        doc = jb.parse(board(GOOD_AD, second), LIMITS)
        self.assertEqual(len(doc.ads), 2)
        self.assertEqual(codes(doc), [])

    def test_line_ranges_do_not_overlap(self):
        second = GOOD_AD.replace("Senior Widget Engineer", "Junior Widget Engineer")
        first, last = jb.parse(board(GOOD_AD, second), LIMITS).ads
        self.assertLess(first.end_line, last.start_line)

    def test_age_is_measured_from_the_date(self):
        ad = jb.parse(board(GOOD_AD), LIMITS).ads[0]
        self.assertEqual(ad.age_days(dt.date(2026, 12, 2)), 90)

    def test_rebuild_is_lossless(self):
        text = board(GOOD_AD)
        doc = jb.parse(text, LIMITS)
        self.assertEqual(jb.rebuild(doc, doc.ads), text)

    def test_rebuild_drops_only_the_removed_ad(self):
        second = GOOD_AD.replace("Senior Widget Engineer", "Junior Widget Engineer")
        doc = jb.parse(board(GOOD_AD, second), LIMITS)
        rebuilt = jb.rebuild(doc, [doc.ads[1]])
        self.assertNotIn("Senior Widget Engineer", rebuilt)
        self.assertIn("Junior Widget Engineer", rebuilt)
        self.assertEqual(codes(jb.parse(rebuilt, LIMITS)), [])


class FileStructure(unittest.TestCase):
    def test_missing_separator(self):
        doc = jb.parse("# Title\n\nNo separator here.\n" + GOOD_AD, LIMITS)
        self.assertEqual(codes(doc), ["missing_separator"])

    def test_separator_needs_blank_lines_around_it(self):
        doc = jb.parse("# Title\nSome text\n---\n\n" + GOOD_AD, LIMITS)
        self.assertEqual(codes(doc), ["missing_separator"])

    def test_front_matter_is_not_mistaken_for_the_separator(self):
        doc = jb.parse("---\n\ntitle: x\n\n---\n" + GOOD_AD, LIMITS)
        self.assertIsNotNone(doc.separator_line)
        self.assertEqual(doc.separator_line, 5)

    def test_stray_content_before_the_first_ad(self):
        doc = jb.parse(board("\nA loose sentence.\n", GOOD_AD), LIMITS)
        self.assertIn("stray_content_before_first_ad", codes(doc))

    def test_empty_board_is_valid(self):
        doc = jb.parse(PREAMBLE, LIMITS)
        self.assertEqual(codes(doc), [])
        self.assertEqual(doc.ads, [])


class Headings(unittest.TestCase):
    def test_missing_at_sign(self):
        doc = jb.parse(board(GOOD_AD.replace(" @ Acme", " at Acme")), LIMITS)
        self.assertEqual(codes(doc), ["bad_heading_format"])

    def test_missing_company(self):
        doc = jb.parse(board(GOOD_AD.replace(" @ Acme", " @ ")), LIMITS)
        self.assertEqual(codes(doc), ["bad_heading_format"])

    def test_missing_space_after_hashes_still_parses_as_an_ad(self):
        doc = jb.parse(board(GOOD_AD.replace("## Senior", "##Senior")), LIMITS)
        self.assertEqual(len(doc.ads), 1)
        self.assertEqual(codes(doc), [])

    def test_duplicate_titles(self):
        doc = jb.parse(board(GOOD_AD, GOOD_AD), LIMITS)
        self.assertEqual(codes(doc), ["duplicate_title"])

    def test_duplicate_detection_ignores_case_and_spacing(self):
        twin = GOOD_AD.replace("## Senior Widget Engineer @ Acme",
                               "## senior  widget engineer @ ACME")
        doc = jb.parse(board(GOOD_AD, twin), LIMITS)
        self.assertEqual(codes(doc), ["duplicate_title"])


class Table(unittest.TestCase):
    def test_no_table_at_all(self):
        doc = jb.parse(board("\n## Role @ Acme\n\nJust a description.\n"), LIMITS)
        self.assertEqual(codes(doc), ["missing_table"])

    def test_missing_link_row(self):
        doc = jb.parse(board("""
## Role @ Acme

| Date | 2026-09-03 |

A description.
"""), LIMITS)
        self.assertEqual(codes(doc), ["missing_link_row"])

    def test_missing_date_row(self):
        doc = jb.parse(board("""
## Role @ Acme

| Link | [Apply](https://acme.example/1) |

A description.
"""), LIMITS)
        self.assertEqual(codes(doc), ["missing_date_row"])

    def test_table_without_the_separator_row_is_accepted(self):
        doc = jb.parse(board(GOOD_AD.replace("|------|-----|\n", "")), LIMITS)
        self.assertEqual(codes(doc), [])

    def test_extra_whitespace_in_cells_is_tolerated(self):
        doc = jb.parse(board(GOOD_AD.replace("| Date | 2026-09-03 |",
                                             "|   Date   |   2026-09-03   |")), LIMITS)
        self.assertEqual(codes(doc), [])

    def test_lowercase_row_labels_are_tolerated(self):
        doc = jb.parse(board(GOOD_AD.replace("| Link |", "| link |")), LIMITS)
        self.assertEqual(codes(doc), [])


class Link(unittest.TestCase):
    def test_plain_url_is_not_a_link(self):
        doc = jb.parse(board(GOOD_AD.replace("[Apply](https://acme.example/careers/1)",
                                             "https://acme.example/careers/1")), LIMITS)
        self.assertEqual(codes(doc), ["bad_link_format"])

    def test_space_between_brackets(self):
        doc = jb.parse(board(GOOD_AD.replace("[Apply](https", "[Apply] (https")), LIMITS)
        self.assertEqual(codes(doc), ["bad_link_format"])

    def test_wrong_label(self):
        doc = jb.parse(board(GOOD_AD.replace("[Apply]", "[Apply here]")), LIMITS)
        self.assertEqual(codes(doc), ["bad_link_label"])

    def test_insecure_scheme(self):
        doc = jb.parse(board(GOOD_AD.replace("https://", "http://")), LIMITS)
        self.assertEqual(codes(doc), ["bad_link_scheme"])

    def test_other_scheme(self):
        doc = jb.parse(board(GOOD_AD.replace("https://acme.example/careers/1",
                                             "ftp://acme.example/jobs")), LIMITS)
        self.assertEqual(codes(doc), ["bad_link_scheme"])

    def test_label_and_scheme_are_both_reported(self):
        doc = jb.parse(board(GOOD_AD.replace("[Apply](https://", "[Click](http://")), LIMITS)
        self.assertEqual(codes(doc), ["bad_link_label", "bad_link_scheme"])


class Date(unittest.TestCase):
    def test_missing_leading_zeroes(self):
        doc = jb.parse(board(GOOD_AD.replace("2026-09-03", "2026-9-3")), LIMITS)
        self.assertEqual(codes(doc), ["bad_date_format"])

    def test_wrong_order(self):
        doc = jb.parse(board(GOOD_AD.replace("2026-09-03", "03/09/2026")), LIMITS)
        self.assertEqual(codes(doc), ["bad_date_format"])

    def test_impossible_day(self):
        doc = jb.parse(board(GOOD_AD.replace("2026-09-03", "2026-02-30")), LIMITS)
        self.assertEqual(codes(doc), ["impossible_date"])

    def test_impossible_month(self):
        doc = jb.parse(board(GOOD_AD.replace("2026-09-03", "2026-13-01")), LIMITS)
        self.assertEqual(codes(doc), ["impossible_date"])


class Body(unittest.TestCase):
    def test_empty_body(self):
        doc = jb.parse(board("""
## Role @ Acme

| Link | [Apply](https://acme.example/1) |
|------|-----|
| Date | 2026-09-03 |
"""), LIMITS)
        self.assertEqual(codes(doc), ["body_empty"])

    def test_body_too_long(self):
        doc = jb.parse(board(GOOD_AD.replace("Acme builds widgets.", "x" * 1100)), LIMITS)
        self.assertEqual(codes(doc), ["body_too_long"])

    def test_limit_is_configurable(self):
        doc = jb.parse(board(GOOD_AD), {"max_body_chars": 10})
        self.assertEqual(codes(doc), ["body_too_long"])
        self.assertEqual(doc.ads[0].issues[0].fields["max"], 10)

    def test_h1_in_body_is_rejected(self):
        doc = jb.parse(board(GOOD_AD + "\n# What you'll do\n\nBuild things.\n"), LIMITS)
        self.assertEqual(codes(doc), ["body_has_top_heading"])

    def test_h3_in_body_is_allowed(self):
        doc = jb.parse(board(GOOD_AD + "\n### What you'll do\n\n* Build things.\n"), LIMITS)
        self.assertEqual(codes(doc), [])

    def test_h2_in_body_starts_a_second_ad(self):
        # `##` cannot be "inside" a body: by definition it opens a new ad, which
        # is exactly why contributors must not use it.
        doc = jb.parse(board(GOOD_AD + "\n## What you'll do\n\nBuild things.\n"), LIMITS)
        self.assertEqual(len(doc.ads), 2)
        self.assertIn("bad_heading_format", codes(doc))


class Config(unittest.TestCase):
    def setUp(self):
        self.config = jb.load_config(jb.DEFAULT_CONFIG_PATH)

    def test_every_issue_code_has_a_message(self):
        errors = self.config["messages"]["errors"]
        warnings = self.config["messages"]["warnings"]
        expected = {
            "missing_separator", "stray_content_before_first_ad", "bad_heading_format",
            "duplicate_title", "not_at_top", "missing_table", "missing_link_row",
            "missing_date_row", "bad_link_format", "bad_link_label", "bad_link_scheme",
            "bad_date_format", "impossible_date", "body_empty", "body_too_long",
            "body_has_top_heading", "date_skew",
        }
        self.assertEqual(expected - set(errors) - set(warnings), set())

    def test_the_documented_example_is_itself_valid(self):
        example = jb.fill(
            self.config["messages"]["format_example"], {"today": "2026-09-03"}
        )
        ad = example.split("````markdown\n")[1].split("````")[0]
        doc = jb.parse(PREAMBLE + "\n" + ad, LIMITS)
        self.assertEqual(codes(doc), [])

    def test_the_documented_example_does_not_hard_code_a_date(self):
        # A fixed date in the example would eventually drift past
        # date_skew_days and warn everyone who copied it faithfully.
        self.assertIn("| Date | {today} |", self.config["messages"]["format_example"])

    def test_every_message_renders(self):
        ambient = {
            "today": "2026-09-03", "max_age_days": 90,
            "max_body_chars": 1024, "date_skew_days": 7,
        }
        fields = {
            "line": 12, "heading": "Role @ Acme", "title": "Role @ Acme",
            "cell": "x", "label": "Click", "url": "http://x", "value": "2026-9-3",
            "count": 1100, "max": 1024, "text": "# Heading", "days": 30,
        }
        catalogue = {
            **self.config["messages"]["errors"],
            **self.config["messages"]["warnings"],
        }
        for code, template in catalogue.items():
            rendered = jb.render(self.config, jb.Issue(code, 1, fields), ambient)
            self.assertNotIn("{", rendered, f"{code} has an unfilled placeholder")

    def test_environment_variables_override_limits(self):
        os.environ["MAX_AGE_DAYS"] = "45"
        try:
            self.assertEqual(jb.load_config()["limits"]["max_age_days"], 45)
        finally:
            del os.environ["MAX_AGE_DAYS"]

    def test_a_nonsense_override_is_rejected(self):
        os.environ["MAX_AGE_DAYS"] = "ninety"
        try:
            with self.assertRaises(SystemExit):
                jb.load_config()
        finally:
            del os.environ["MAX_AGE_DAYS"]


class RealBoard(unittest.TestCase):
    def test_the_committed_board_parses(self):
        config = jb.load_config(jb.DEFAULT_CONFIG_PATH)
        path = jb.REPO_ROOT / config["job_file"]
        doc = jb.parse(path.read_text(encoding="utf-8"), config["limits"])
        self.assertEqual(doc.file_issues, [])
        self.assertGreaterEqual(len(doc.ads), 1)
        for ad in doc.ads:
            self.assertTrue(ad.role and ad.company, f"unparsed heading: {ad.heading!r}")


if __name__ == "__main__":
    unittest.main()
