"""corpus.py: python -m unittest discover -s scripts"""
import io
import json
import pathlib
import tempfile
import unittest
import zipfile
from unittest import mock

import corpus
import mod_lib
import store


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


# Starling's `android_native_sdk.py mods OUT PACKAGE... --check`: check.json for every package, a package with
# src/broken.rs failing, as the base's checkout has it.
CHECK_SDK = """
import json, os, pathlib, sys, tomllib, zipfile
out = pathlib.Path(sys.argv[2])
packages = [pathlib.Path(a) for a in sys.argv[3:] if a.endswith((".hmod", ".smod"))]
assert "--check" in sys.argv and os.environ["RUSTFLAGS"] == "-C prefer-dynamic"
answers = {}
for package in packages:
    with zipfile.ZipFile(package) as z:
        name = "sunrise-mod.toml" if "sunrise-mod.toml" in z.namelist() else "horizon-mod.toml"
        manifest = tomllib.loads(z.read(name).decode())
        broken = "src/broken.rs" in z.namelist()
        layer = ".starling-horizon" in z.namelist()
    answers[package.name] = {"mod": manifest["id"], "version": manifest["version"], "ok": not broken,
                             "errors": int(broken), "first": ["E0425 cannot find value (src/broken.rs:1)"] if broken else [],
                             "layer": layer}
out.mkdir(parents=True)
(out / "check.json").write_text(json.dumps({"game": {"ok": True, "first": []}, "packages": answers}))
"""
MANIFEST = "id = '{id}'\nversion = '1.0.0'\n[native]\nhorizon = 'x'\n"
CARGO = ("[package]\nname = '{id}'\nversion = '1.0.0'\nedition = '2024'\n[dependencies]\n"
         "horizon-mod-api = {{ path = '.horizon/sdk/api/crates/horizon-mod-api' }}\n")


class Check(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.mods, self.result, self.source = self.dir / "mods", self.dir / "result", self.dir / "source"
        self.mods.mkdir()
        (self.source / "tools" / "android_compat").mkdir(parents=True)
        (self.source / "crates" / "sunrise-mod-api").mkdir(parents=True)
        (self.source / "tools" / "android_native_sdk.py").write_text(CHECK_SDK)
        (self.source / "tools" / "android_compat" / "fixes.py").write_text("")

    def job(self, mod, extra=None):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("horizon-mod.toml", MANIFEST.format(id=mod))
            z.writestr("Cargo.toml", CARGO.format(id=mod))
            z.writestr("src/lib.rs", "")
            for name, text in (extra or {}).items():
                z.writestr(name, text)
        body = buffer.getvalue()
        digest = __import__("hashlib").sha256(body).hexdigest()
        (self.mods / f"{digest}.hmod").write_bytes(body)
        return {"job": store.job_of(digest, "arm64-v8a"), "abi": "arm64-v8a", "sha256": digest}

    def test_every_job_in_one_check_with_the_layer_and_the_stores_refusals(self):
        jobs = [self.job("alpha"), self.job("beta", {"src/broken.rs": ""}), self.job("gamma", {"build.rs": ""})]
        mod_lib.check_all(jobs, self.mods, self.source, self.result, "f" * 40, "fast", "arm64-v8a")
        answer = json.loads((self.result / "check.json").read_text())
        states = {a["id"]: a for a in answer["jobs"].values()}
        self.assertTrue(answer["game"]["ok"])
        self.assertEqual(states["alpha"]["state"], "ok")
        self.assertEqual(states["beta"]["state"], "failed")
        self.assertEqual(states["beta"]["first"], ["E0425 cannot find value (src/broken.rs:1)"])
        self.assertEqual(states["gamma"]["state"], "refused")
        self.assertIn("whole-game Compile", states["gamma"]["error"])
        self.assertFalse((self.result / "work").exists())


if __name__ == "__main__":
    unittest.main()
