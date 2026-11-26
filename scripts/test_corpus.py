"""corpus.py: python -m unittest discover -s scripts"""
import io
import json
import pathlib
import tempfile
import unittest
import zipfile
from unittest import mock

import corpus


def hmod(path, mod, version, native=True):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("horizon-mod.toml", f"id = '{mod}'\nversion = '{version}'\n" + ("[native]\nhorizon = 'x'\n" if native else ""))
        z.writestr("src/lib.rs", "")
    path.write_bytes(buffer.getvalue())


class Corpus(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp())
        self.manifest = mock.patch.object(corpus, "MANIFEST", self.temp / "corpus" / "horizon.json")
        self.manifest.start()

    def tearDown(self):
        self.manifest.stop()

    def test_seed_verify_and_jobs(self):
        mods = self.temp / "mods"
        mods.mkdir()
        hmod(mods / "a.hmod", "alpha", "1.0.0")
        hmod(mods / "b.hmod", "beta", "0.2.0")
        hmod(mods / "c.hmod", "content", "1.0.0", native=False)
        corpus.seed(mods, self.temp / "corpus.tar")
        rows = corpus.mods()
        self.assertEqual([r["id"] for r in rows], ["alpha", "beta"])
        out = self.temp / "out"
        out.mkdir()
        import tarfile
        with tarfile.open(self.temp / "corpus.tar") as tar:
            tar.extractall(out, filter="data")
        corpus.verify(out)
        (out / f"{rows[0]['sha256']}.hmod").write_bytes(b"other")
        with self.assertRaisesRegex(SystemExit, "alpha"):
            corpus.verify(out)
        corpus.jobs(self.temp / "jobs.json", "arm64-v8a")
        jobs = json.loads((self.temp / "jobs.json").read_text())
        self.assertEqual(jobs[0], {"job": f"{rows[0]['sha256'][:32]}.arm64-v8a", "abi": "arm64-v8a",
                                   "sha256": rows[0]["sha256"]})

    def answer(self, states, game_ok=True):
        jobs = {f"{i:032x}.arm64-v8a": {"id": mod, "version": "1.0", "state": state,
                                         **({"first": [f"E0308 {mod} broke (src/lib.rs:1)"]} if state == "failed" else {})}
                for i, (mod, state) in enumerate(states.items())}
        path = self.temp / "check.json"
        path.write_text(json.dumps({"build": "f" * 40, "game": {"ok": game_ok, "first": [] if game_ok else ["E0425 x"]},
                                    "jobs": jobs}))
        return path

    def baseline(self, ids):
        path = self.temp / "baseline.txt"
        path.write_text("# measured on " + "e" * 40 + "\n" + "".join(f"{m}\n" for m in ids))
        return path

    def test_a_baseline_mod_that_no_longer_compiles_fails_the_run(self):
        text, passes = corpus.compare(self.answer({"alpha": "ok", "beta": "failed", "gamma": "ok"}),
                                      base=self.baseline(["alpha", "beta"]))
        self.assertFalse(passes)
        self.assertIn("No longer compile (1)", text)
        self.assertIn("| beta | 1.0 | E0308 beta broke", text)
        self.assertIn("Compile now, not in the baseline (1)", text)

    def test_more_than_the_baseline_passes_and_writes_the_new_one(self):
        ok = self.temp / "ok.txt"
        text, passes = corpus.compare(self.answer({"alpha": "ok", "beta": "ok", "delta": "refused"}), ok,
                                      base=self.baseline(["alpha"]))
        self.assertTrue(passes)
        self.assertIn("Every mod of the baseline still compiles", text)
        self.assertEqual(ok.read_text().splitlines()[1:], ["alpha", "beta"])

    def test_no_baseline_passes(self):
        _, passes = corpus.compare(self.answer({"alpha": "failed"}), base=self.temp / "none.txt")
        self.assertTrue(passes)

    def test_the_games_crates_failing_fails_the_run(self):
        text, passes = corpus.compare(self.answer({"alpha": "failed"}, game_ok=False), base=self.baseline([]))
        self.assertFalse(passes)
        self.assertIn("game's own crates do not compile", text)


if __name__ == "__main__":
    unittest.main()
