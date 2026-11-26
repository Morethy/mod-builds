#!/usr/bin/env python3
"""The corpus check's files (corpus.yml): the catalog's Horizon mods every new game must still compile.

    python3 scripts/corpus.py seed DIR TAR          # DIR's .hmod files into TAR and corpus/horizon.json
    python3 scripts/corpus.py verify DIR            # DIR holds every file corpus/horizon.json lists, by SHA-256
    python3 scripts/corpus.py jobs JOBS ABI         # mod_lib.py's jobs for the corpus
    python3 scripts/corpus.py compare CHECK [OK]    # mod_lib.py `check`'s answer against the baseline

Horizon's catalog (horizon-rust.github.io/horizon-mods) is gone, so the store keeps the mods it built for Starling
0.9.0 itself: corpus/horizon.json lists them (id, version, the .hmod's SHA-256), and their files are a tar in this
repository's draft release `corpus-horizon` (a draft: only the repository's collaborators see it, as the mods are
their authors', not ours to publish). corpus/horizon-baseline.txt names the ones that compiled on the last game
measured; `compare` fails when one of them no longer does, and names the ones that compile now and did not.
"""
import hashlib
import json
import pathlib
import sys
import tarfile
import tomllib
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import store  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = HERE / "corpus" / "horizon.json"
BASELINE = HERE / "corpus" / "horizon-baseline.txt"
RELEASE = "corpus-horizon"
ASSET = "horizon-corpus.tar"


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def about(hmod):
    """The package's id and version, from its manifest (Horizon's or Sunrise's), or None."""
    try:
        with zipfile.ZipFile(hmod) as z:
            name = next((n for n in ("horizon-mod.toml", "sunrise-mod.toml") if n in z.namelist()), None)
            manifest = tomllib.loads(z.read(name).decode("utf-8")) if name else {}
    except (OSError, ValueError, zipfile.BadZipFile, UnicodeDecodeError):
        return None
    mod, version = manifest.get("id"), manifest.get("version")
    if not (isinstance(mod, str) and store.ID.fullmatch(mod) and isinstance(version, str)
            and store.VERSION.fullmatch(version) and isinstance(manifest.get("native"), dict)):
        return None
    return mod, version


def seed(folder, tar):
    """Every native .hmod in `folder` (one version of each mod: the newest file wins) into `tar`, by SHA-256, and
    corpus/horizon.json."""
    chosen = {}
    for hmod in sorted(pathlib.Path(folder).glob("*.hmod"), key=lambda p: p.stat().st_mtime):
        found = about(hmod)
        if found is None:
            print(f"{hmod.name}: not a native mod, left out")
            continue
        chosen[found[0]] = (found[1], hmod)
    rows = []
    with tarfile.open(tar, "w") as out:
        for mod in sorted(chosen):
            version, hmod = chosen[mod]
            digest = sha256_of(hmod)
            out.add(hmod, arcname=f"{digest}.hmod")
            rows.append({"id": mod, "version": version, "sha256": digest})
    MANIFEST.parent.mkdir(exist_ok=True)
    MANIFEST.write_text(json.dumps({"release": RELEASE, "asset": ASSET, "mods": rows}, indent=1) + "\n")
    print(f"{len(rows)} mods into {tar} and {MANIFEST}")


def mods():
    return json.loads(MANIFEST.read_text())["mods"]


def verify(folder):
    folder = pathlib.Path(folder)
    wrong = [row["id"] for row in mods()
             if not (folder / f"{row['sha256']}.hmod").is_file() or sha256_of(folder / f"{row['sha256']}.hmod") != row["sha256"]]
    if wrong:
        sys.exit(f"The corpus lacks or has another file for: {' '.join(wrong)}")
    print(f"The corpus has its {len(mods())} files.")


def jobs(path, abi):
    rows = [{"job": store.job_of(row["sha256"], abi), "abi": abi, "sha256": row["sha256"]} for row in mods()]
    pathlib.Path(path).write_text(json.dumps(rows, indent=1) + "\n")
    print(f"{len(rows)} jobs")


def baseline(path=BASELINE):
    """The baseline's mod ids, and the line that says which game it was measured on."""
    if not path.is_file():
        return None, ""
    lines = path.read_text().splitlines()
    measured = next((line.lstrip("# ").strip() for line in lines if line.startswith("# measured")), "")
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}, measured


def compare(check, ok_out=None, base=BASELINE):
    """Markdown for the run's summary, and whether the run passes: the game's crates compile and every mod of the
    baseline still does. `ok_out` gets the ids that compile, as a new baseline would list them."""
    answer = json.loads(pathlib.Path(check).read_text())
    build, game = answer.get("build", ""), answer.get("game", {})
    rows = [a for a in answer.get("jobs", {}).values() if a.get("id")]
    ok = {a["id"] for a in rows if a.get("state") == "ok"}
    by_id = {a["id"]: a for a in rows}
    known, measured = baseline(base)
    lines = [f"## Corpus check: {len(ok)} of {len(rows)} catalog mods compile on `{build[:12]}`", ""]
    passes = bool(game.get("ok", False))
    if not passes:
        lines += ["**The game's own crates do not compile**, so no mod was checked:", "", "```",
                  *(str(x)[:300] for x in game.get("first", [])[:8]), "```", ""]
    if known is None:
        lines += ["No baseline yet (corpus/horizon-baseline.txt): this run measures one.", ""]
    else:
        lost = sorted(known - ok)
        gained = sorted(ok - known)
        lines += [f"Baseline: {len(known)} mods ({measured or 'measured before'}).", ""]
        if lost:
            passes = False
            lines += [f"### No longer compile ({len(lost)})", "", "| Mod | Version | Why |", "|---|---|---|"]
            for mod in lost:
                row = by_id.get(mod, {})
                why = "; ".join(row.get("first", [])[:2]) or row.get("error") or "not in the corpus any more"
                lines.append(f"| {mod} | {row.get('version', '')} | {why.replace('|', '/')[:300]} |")
            lines.append("")
        else:
            lines += ["Every mod of the baseline still compiles.", ""]
        if gained:
            lines += [f"### Compile now, not in the baseline ({len(gained)})", "", ", ".join(gained), ""]
    failing = sorted((a for a in rows if a.get("state") == "failed" and a["id"] not in (known or set())),
                     key=lambda a: a["id"])
    if failing:
        lines += [f"<details><summary>Fail, as on the baseline ({len(failing)})</summary>", ""]
        lines += [f"- {a['id']} {a.get('version', '')}: {('; '.join(a.get('first', [])[:1]) or a.get('error', ''))[:200]}"
                  for a in failing]
        lines += ["", "</details>", ""]
    refused = sorted((a for a in rows if a.get("state") == "refused"), key=lambda a: a["id"])
    if refused:
        lines += [f"<details><summary>Not checked ({len(refused)}: the store builds them another way or not at "
                  "all)</summary>", "", *(f"- {a['id']}: {a.get('error', '')[:200]}" for a in refused), "",
                  "</details>", ""]
    if ok_out:
        pathlib.Path(ok_out).write_text(f"# measured on {build} ({len(ok)} mods)\n" + "".join(f"{m}\n" for m in sorted(ok)))
    return "\n".join(lines), passes


def main(argv):
    if len(argv) == 4 and argv[1] == "seed":
        seed(argv[2], argv[3])
    elif len(argv) == 3 and argv[1] == "verify":
        verify(argv[2])
    elif len(argv) == 4 and argv[1] == "jobs":
        jobs(argv[2], argv[3])
    elif len(argv) in (3, 4) and argv[1] == "compare":
        text, passes = compare(argv[2], argv[3] if len(argv) == 4 else None)
        print(text)
        sys.exit(0 if passes else 1)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
