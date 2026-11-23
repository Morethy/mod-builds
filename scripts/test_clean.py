"""clean.py's choice of what goes: python -m unittest discover -s scripts"""
import calendar
import time
import unittest

import clean


def asset(name, days_ago, now):
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days_ago * 86400))
    return {"name": name, "created_at": at}


class Stale(unittest.TestCase):
    now = calendar.timegm((2026, 11, 15, 12, 0, 0))

    def test_a_full_mods_release_drops_retryable_failures_and_older_versions(self):
        asset = lambda name, when="2026-11-20T00:00:00Z": {"name": name, "id": name, "created_at": when}
        listed = [asset("a.arm64-v8a.so"), asset("a.arm64-v8a.json", "2026-11-20T00:00:00Z"),
                  asset("b.arm64-v8a.so"), asset("b.arm64-v8a.json", "2026-11-22T00:00:00Z"),
                  asset("c.arm64-v8a.failed.json"), asset("d.arm64-v8a.failed.json"),
                  asset("e.arm64-v8a.failed.json"), asset("e.arm64-v8a.json")]
        index = {"mods": [{"id": "x", "job": "a.arm64-v8a", "version": "1"},
                          {"id": "x", "job": "b.arm64-v8a", "version": "2"},
                          {"id": "e", "job": "e.arm64-v8a", "version": "1"}],
                 "failed": [{"job": "c.arm64-v8a", "settled": False}, {"job": "d.arm64-v8a", "settled": True},
                            {"job": "e.arm64-v8a", "settled": True}]}
        gone = sorted(a["name"] for a in clean.mods_prunable(listed, index))
        self.assertEqual(gone, ["a.arm64-v8a.json", "a.arm64-v8a.so", "c.arm64-v8a.failed.json",
                                "e.arm64-v8a.failed.json"])

    def test_old_builds_go_and_new_ones_stay(self):
        listed = [asset("new.so", 1, self.now), asset("old.so", 15, self.now)]
        self.assertEqual([a["name"] for a in clean.stale(listed, 14, 900, self.now)], ["old.so"])

    def test_beyond_the_newest_kept_go_oldest_first(self):
        listed = [asset(f"{i}.so", i / 10, self.now) for i in range(5)]
        self.assertEqual([a["name"] for a in clean.stale(listed, 14, 3, self.now)], ["3.so", "4.so"])


if __name__ == "__main__":
    unittest.main()
