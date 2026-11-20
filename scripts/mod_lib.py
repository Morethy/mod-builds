#!/usr/bin/env python3
"""Compile v2's build of one mod at a time: each .hmod's library against a game build's SDK, in the
layout the game's loader reads (crates/horizon-mod-host/src/native_libraries.rs in Starling): a
`lib<crate>.so` exporting `horizon_native_build`, whose text is the library's identity
("<build> <mod> <version> <rustc>"), and `horizon_native_mod_v1`, with the entry module generated
from native_mod_entry.rs.template.

    python3 scripts/mod_lib.py build JOBS MODS SDK RESULT   # BUILD; JOBS a JSON list of {job, abi, sha256}

Each job's .hmod is MODS/<sha256>.hmod. Its answer is RESULT/<job>.status.json and, when it built,
RESULT/<job>.so; scripts/store.py publishes them from a job that runs nothing of the mods. This runs
in the sandbox (scripts/sandbox.sh): no network, no token, nothing kept.

Only a mod the API route takes is built here (the launcher's `prebuilt::project_refusal`): no build
script, no features, and no crate but the SDK's own. A mod the route doesn't take is told to use the
whole-game Compile, for good, and is never built again.

The compiling itself is the SDK's (MOD_LIB_COMPILER, a path in the SDK folder or an absolute one,
from the Android SDK the game side makes):

    $MOD_LIB_COMPILER --sdk SDK --target TARGET --mod FOLDER --out OUT

builds FOLDER (an unpacked .hmod) as a dylib against SDK's runtime and api slices for TARGET, and
writes OUT/library.json, {"library": "lib<crate>.so", "identity": "..."}, beside the library. Exit 0
built, 1 the mod doesn't compile (for good), 75 the service couldn't tell (may pass another time).
Until that compiler exists every job fails here, not for good, saying so.
"""
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import store  # noqa: E402

# What an unpacked .hmod may be at most, and how many files (a zip bomb stops here).
UNPACKED = 64 << 20
FILES = 4000
# A mod's build, at most.
SECONDS = int(os.environ.get("MOD_LIB_SECONDS", "600"))
# The SDK packages a mod may depend on (launcher/src/native_mods/api.rs `PACKAGES`).
API_PACKAGES = {"horizon-mod-api", "horizon-mod-points", "horizon-internals"}
NOT_YET = "This build of the game can't compile mods one by one yet."
TEMPFAIL = 75
WHOLE_GAME = "needs the whole-game Compile"


class Refused(Exception):
    """A mod this route can't build, for good."""


def unpack(hmod, into):
    """The .hmod's files into `into`: plain files and folders inside it, within UNPACKED bytes."""
    with zipfile.ZipFile(hmod) as archive:
        entries = archive.infolist()
        if len(entries) > FILES:
            raise Refused(f"has more than {FILES} files")
        total = 0
        for entry in entries:
            name = entry.filename
            parts = pathlib.PurePosixPath(name).parts
            if not parts or name.startswith("/") or "\\" in name or any(p in ("..", "") for p in parts):
                raise Refused(f"has a file outside itself: {name!r}")
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise Refused(f"has a link: {name}")
            if entry.is_dir():
                (into / name).mkdir(parents=True, exist_ok=True)
                continue
            total += entry.file_size
            if total > UNPACKED:
                raise Refused(f"is more than {UNPACKED >> 20} MB unpacked")
            (into / name).parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, open(into / name, "wb") as target:
                shutil.copyfileobj(source, target, 1 << 20)
            if (into / name).stat().st_size != entry.file_size:
                raise Refused(f"{name}: not the size it says")


def mod_of(folder):
    """The mod's id and version (horizon-mod.toml), and its Cargo.toml."""
    try:
        manifest = tomllib.loads((folder / "horizon-mod.toml").read_text())
        cargo = tomllib.loads((folder / "Cargo.toml").read_text())
    except (OSError, ValueError) as e:
        raise Refused(f"is not a native mod: {e}")
    mod, version = manifest.get("id"), manifest.get("version")
    if not isinstance(manifest.get("native"), dict):
        raise Refused("is not a native mod")
    if not (isinstance(mod, str) and store.ID.fullmatch(mod)) or not (
        isinstance(version, str) and store.VERSION.fullmatch(version)
    ):
        raise Refused("has no plain id and version")
    return mod, version, cargo


def check(cargo, folder):
    """Refused, for good, when the API route won't build it."""
    why = refusal(cargo, folder)
    if why:
        raise Refused(f"{why}: it {WHOLE_GAME}")


def refusal(cargo, folder):
    """Why the API route won't build this crate, or None (the launcher's `project_refusal`)."""
    package = cargo.get("package") or {}
    build = package.get("build")
    if (build is not None and build is not False) or (build is None and (folder / "build.rs").is_file()):
        return "has a build script"
    if cargo.get("build-dependencies"):
        return "has build dependencies"
    if cargo.get("features"):
        return "has features"
    for table in dependency_tables(cargo):
        for key, value in table.items():
            krate = value.get("package", key) if isinstance(value, dict) else key
            if krate not in API_PACKAGES:
                return f"uses the crate {krate}"
            if isinstance(value, dict) and (value.get("features") or value.get("default-features") is False):
                return f"chooses features of {krate}"
    return None


def dependency_tables(cargo):
    yield cargo.get("dependencies") or {}
    for target in (cargo.get("target") or {}).values():
        if isinstance(target, dict):
            yield target.get("dependencies") or {}


def build_one(row, mods, sdk, result, build, compiler):
    """One job's answer into `result`."""
    job, abi, source = row["job"], row["abi"], row["sha256"]
    status = {"build": build, "source": source, "abi": abi}
    work = result / "work" / job
    shutil.rmtree(work, ignore_errors=True)
    folder, out = work / "mod", work / "out"
    folder.mkdir(parents=True)
    out.mkdir()
    try:
        hmod = mods / f"{source}.hmod"
        if store.job_of(store.sha256_of(hmod), abi) != job:
            raise Refused("is not the file that was asked for")
        try:
            unpack(hmod, folder)
        except zipfile.BadZipFile:
            raise Refused("is not a .hmod")
        mod, version, cargo = mod_of(folder)
        status.update(id=mod, version=version)
        check(cargo, folder)
        if not compiler:
            return said(result, job, {**status, "state": "failed", "error": NOT_YET, "settled": False})
        command = [*compiler, "--sdk", str(sdk), "--target", store.TARGETS[abi], "--mod", str(folder), "--out", str(out)]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=SECONDS)
        except subprocess.TimeoutExpired:
            return said(result, job, {**status, "state": "failed", "error": "The build took too long.", "settled": False})
        if done.returncode != 0:
            error = (done.stderr or done.stdout or "The build failed.").strip()[-600:]
            return said(result, job, {**status, "state": "failed", "error": error,
                                      "settled": done.returncode != TEMPFAIL})
        made = json.loads((out / "library.json").read_text())
        name, identity = made.get("library"), made.get("identity")
        if not (isinstance(name, str) and store.LIBRARY.fullmatch(name) and (out / name).is_file()):
            raise ValueError("the compiler named no library")
        words = str(identity).split(" ")
        if len(words) != 4 or words[:3] != [build, mod, version]:
            raise ValueError(f"the library's identity {identity!r} is not {build[:12]} {mod} {version}")
        shutil.copyfile(out / name, result / f"{job}.so")
        library = result / f"{job}.so"
        return said(result, job, {**status, "state": "done", "library": name, "identity": identity,
                                  "sha256": store.sha256_of(library), "size": library.stat().st_size})
    except Refused as e:
        return said(result, job, {**status, "state": "failed", "error": f"This mod {e}.", "settled": True})
    except (OSError, ValueError) as e:
        return said(result, job, {**status, "state": "failed", "error": f"The build went wrong: {e}", "settled": False})
    finally:
        shutil.rmtree(work, ignore_errors=True)


def said(result, job, status):
    (result / f"{job}.status.json").write_text(json.dumps(status))
    print(f"{job}: {status.get('id', '?')} {status['state']}"
          + (f": {status['error']}" if status["state"] == "failed" else ""), flush=True)
    return status


def main(argv):
    if len(argv) != 6 or argv[1] != "build":
        sys.exit(__doc__)
    build = os.environ.get("BUILD", "")
    if not store.BUILD.fullmatch(build):
        sys.exit(f"Not a build: {build!r}")
    jobs = json.loads(pathlib.Path(argv[2]).read_text())
    mods, sdk, result = (pathlib.Path(a) for a in argv[3:6])
    result.mkdir(parents=True, exist_ok=True)
    # The compiler comes with the SDK: a path in it is read from where the SDK is.
    compiler = os.environ.get("MOD_LIB_COMPILER", "").split()
    if compiler and not os.path.isabs(compiler[0]):
        compiler[0] = str(sdk / compiler[0])
    for row in jobs:
        if not (store.JOB.fullmatch(str(row.get("job"))) and store.SHA256.fullmatch(str(row.get("sha256")))):
            continue
        build_one(row, mods, sdk, result, build, compiler)
    shutil.rmtree(result / "work", ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv)
