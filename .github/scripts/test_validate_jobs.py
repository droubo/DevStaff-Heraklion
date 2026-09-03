#!/usr/bin/env python3
"""Tests for the pull request scoping logic in validate_jobs.py.

The parser is covered by test_jobs_board.py; this covers the part that decides
*which* job ads a pull request is responsible for.

Run with:  python3 -m unittest discover -s .github/scripts -p 'test_*.py'
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jobs_board as jb
import validate_jobs as vj

LIMITS = {"max_body_chars": 1024}


def ad(start: int, end: int) -> jb.Ad:
    lines = ["x"] * (end - start + 1)
    return jb.Ad(start_line=start, end_line=end, raw_lines=lines)


class ParseDiff(unittest.TestCase):
    def test_added_block(self):
        added, deletions = vj.parse_diff("@@ -11,0 +12,8 @@ context\n")
        self.assertEqual(added, set(range(12, 20)))
        self.assertEqual(deletions, [])

    def test_single_line_change_without_a_count(self):
        added, deletions = vj.parse_diff("@@ -16 +16 @@\n")
        self.assertEqual(added, {16})
        self.assertEqual(deletions, [])

    def test_pure_deletion_is_kept_apart_from_additions(self):
        added, deletions = vj.parse_diff("@@ -30,14 +29,0 @@ context\n")
        self.assertEqual(added, set())
        self.assertEqual(deletions, [29])

    def test_several_hunks(self):
        added, deletions = vj.parse_diff(
            "@@ -11,0 +12,3 @@\n+a\n@@ -40,2 +42,0 @@\n-b\n"
        )
        self.assertEqual(added, {12, 13, 14})
        self.assertEqual(deletions, [42])

    def test_noise_is_ignored(self):
        added, deletions = vj.parse_diff(
            "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
        )
        self.assertEqual(added, {1})
        self.assertEqual(deletions, [])


class Touched(unittest.TestCase):
    def test_an_untouched_ad_is_not_reported_on(self):
        self.assertFalse(is_t := vj.is_touched(ad(12, 29), {40, 41}, []))

    def test_an_edited_line_touches_its_ad(self):
        self.assertTrue(vj.is_touched(ad(12, 29), {16}, []))

    def test_removing_a_row_from_an_ad_touches_it(self):
        # The Date row deleted from inside an ad spanning 12-28.
        self.assertTrue(vj.is_touched(ad(12, 28), set(), [15]))

    def test_removing_a_whole_ad_does_not_touch_the_one_above(self):
        # An ad ending at line 29 was followed by a deleted ad.
        self.assertFalse(vj.is_touched(ad(12, 29), set(), [29]))

    def test_removing_a_whole_ad_does_not_touch_the_one_below(self):
        # An ad now starting at line 12 was preceded by a deleted ad.
        self.assertFalse(vj.is_touched(ad(12, 29), set(), [11]))

    def test_a_deletion_at_the_very_start_of_an_ad_is_not_interior(self):
        self.assertFalse(vj.is_touched(ad(12, 29), set(), [12 - 1]))


class IsNew(unittest.TestCase):
    def test_a_wholly_added_ad_is_new(self):
        self.assertTrue(vj.is_new(ad(12, 15), {12, 13, 14, 15}))

    def test_a_partly_edited_ad_is_not_new(self):
        self.assertFalse(vj.is_new(ad(12, 15), {13}))

    def test_blank_lines_do_not_have_to_be_in_the_diff(self):
        blanks = jb.Ad(start_line=12, end_line=15, raw_lines=["a", "", "", "b"])
        self.assertTrue(vj.is_new(blanks, {12, 15}))


class MultipleAdsInOnePr(unittest.TestCase):
    """Adding several ads at once is supported; ordering is judged per ad."""

    def setUp(self):
        self.first, self.second, self.existing = ad(12, 20), ad(22, 30), ad(32, 44)

    def _order_errors(self, ads, added):
        """Mirror of the ordering pass in validate_jobs.main()."""
        flagged, seen_existing = [], False
        for a in ads:
            new = vj.is_new(a, added)
            if new and seen_existing:
                flagged.append(a)
            if not new:
                seen_existing = True
        return flagged

    def test_three_new_ads_at_the_top_are_all_accepted(self):
        added = set(range(12, 31))
        self.assertEqual(
            self._order_errors([self.first, self.second, self.existing], added), []
        )

    def test_a_new_ad_below_an_existing_one_is_flagged(self):
        added = set(range(32, 45))
        moved = ad(32, 44)
        self.assertEqual(
            self._order_errors([self.first, self.second, moved], added), [moved]
        )

    def test_one_at_the_top_and_one_at_the_bottom_flags_only_the_bottom(self):
        bottom = ad(46, 54)
        added = set(range(12, 21)) | set(range(46, 55))
        flagged = self._order_errors(
            [self.first, self.second, self.existing, bottom], added
        )
        self.assertEqual(flagged, [bottom])


class RemovingAnAdFromTheRealBoard(unittest.TestCase):
    """Deleting an ad must not surface problems in the ads left behind."""

    def setUp(self):
        self.config = jb.load_config(jb.DEFAULT_CONFIG_PATH)
        path = jb.REPO_ROOT / self.config["job_file"]
        self.original = path.read_text(encoding="utf-8")
        self.doc = jb.parse(self.original, self.config["limits"])
        if len(self.doc.ads) < 2:
            self.skipTest("needs at least two ads on the board")

    def test_neighbours_are_left_alone(self):
        import difflib

        for index, victim in enumerate(self.doc.ads):
            kept = [a for a in self.doc.ads if a is not victim]
            pruned = jb.rebuild(self.doc, kept)

            # Build the same unified=0 diff git would produce.
            diff = "".join(
                difflib.unified_diff(
                    self.original.splitlines(keepends=True),
                    pruned.splitlines(keepends=True),
                    n=0,
                )
            )
            added, deletions = vj.parse_diff(diff)
            self.assertEqual(added, set(), f"removing ad {index} looked like an addition")

            for survivor in jb.parse(pruned, self.config["limits"]).ads:
                self.assertFalse(
                    vj.is_touched(survivor, added, deletions),
                    f"removing ad {index} wrongly blamed {survivor.heading!r}",
                )


if __name__ == "__main__":
    unittest.main()
