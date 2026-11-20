#!/usr/bin/env python3
"""Compile v2's store: one library per mod, built against a game build (a Nightly), kept in this
repository's releases, where the game reads it straight from GitHub (the Compile service, worker/, is
asked only for a mod the store doesn't have).

    store/builds.json           every game build the store serves, newest first: its Nightly, its
                                SDK, and per ABI the game's own runtime archive (url, sha256, size)
    mods-<build>/index.json     that build's mods: id, version and the .hmod's SHA-256, to the
                                library's asset and SHA-256, and the mods that failed there
    mods-<build>/<job>.so       one mod's library (job: the .hmod's SHA-256, 32 digits, and the ABI)
    mods-<build>/<job>.json     its answer, which the index is made from; <job>.failed.json if none

    python3 scripts/store.py register              # BUILD, NIGHTLY, SDK, ABIS: builds.json gets BUILD
    python3 scripts/store.py plan OUT              # BUILD, ABIS: the catalog's native mods BUILD lacks
    python3 scripts/store.py fetch PLAN DIR        # each planned .hmod, checked against the catalog
    python3 scripts/store.py sdk DIR               # BUILD, ABIS: DIR/<abi>/ the SDK's runtime and api
    python3 scripts/store.py publish RESULT        # BUILD (+ JOB, ASK, BUILT for one asked mod)
    python3 scripts/store.py index                 # BUILD: mods-<BUILD>/index.json from its answers

Only `publish`, `index` and `register` write, from jobs that run nothing of the mods: the library is
checked and uploaded, and each answer is read as JSON and written again with the fields the game
reads, so nothing a mod made can say more than that.
"""
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

SCHEMA = 1
STORE = "store"
BUILDS = "builds.json"
INDEX = "index.json"
CATALOG = "https://horizon-rust.github.io/horizon-mods/index.json"
# Each ABI the store builds for, and the Rust target its SDK slices are named after.
TARGETS = {
    "arm64-v8a": "aarch64-linux-android",
    "armeabi-v7a": "armv7-linux-androideabi",
    "x86_64": "x86_64-linux-android",
    "x86": "i686-linux-android",
}
BUILD = re.compile(r"[0-9a-f]{40}")
JOB = re.compile(r"[0-9a-f]{32}\.(%s)" % "|".join(re.escape(abi) for abi in TARGETS))
SHA256 = re.compile(r"[0-9a-f]{64}")
# What a mod's id and version may be in the index (horizon-mod.toml's rules are stricter).
ID = re.compile(r"[a-z0-9_][a-z0-9_.-]{0,63}")
VERSION = re.compile(r"[0-9A-Za-z.+-]{1,64}")
# A library's name on the phone: one plain file name, as native-mods.json takes it.
LIBRARY = re.compile(r"lib[a-z0-9_]{1,64}\.so")
# What a build's .hmod may be at most (the service's MAX_BODY).
LARGEST_MOD = 32 << 20
DID_NOT_FINISH = "The build on GitHub did not finish. Try again."


def mods_tag(build):
    return f"mods-{build}"


def job_of(source, abi):
    """The job of a .hmod whose SHA-256 is `source`, for `abi`: the Worker's `modJobOf`."""
    return f"{source[:32]}.{abi}"


def repo():
    return os.environ["GH_REPO"]


def download_url(tag, name):
    return f"https://github.com/{repo()}/releases/download/{tag}/{name}"


def fetch(url, limit=LARGEST_MOD):
    """The bytes at `url`, or None on a 404."""
    request = urllib.request.Request(url, headers={"User-Agent": "starling-mod-builds"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if len(body) > limit:
        raise ValueError(f"{url}: more than {limit} bytes")
    return body


def fetch_json(url):
    """The JSON at `url`, or None on a 404."""
    body = fetch(url, 64 << 20)
    return None if body is None else json.loads(body)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


# ---- the answer of one build ----------------------------------------------------------------------


def answer(result, job, built):
    """What a build's `result` folder says, as the store keeps it: ("done", status, library) or
    ("failed", failure, None). `result/<job>.status.json` is what scripts/mod_lib.py wrote, beside the
    library it names; only the fields below are kept, each checked."""
    status = None
    try:
        status = json.loads((result / f"{job}.status.json").read_text())
    except (OSError, ValueError):
        pass
    if not isinstance(status, dict):
        status = None
    about = about_of(status or {}, job)
    if built and status and status.get("state") == "done" and about:
        library = result / f"{job}.so"
        name = status.get("library")
        size, want = status.get("size"), status.get("sha256")
        identity = status.get("identity")
        if (
            isinstance(name, str)
            and LIBRARY.fullmatch(name)
            and isinstance(identity, str)
            and len(identity) <= 400
            and identity.split(" ")[:1] == [about.get("build")]
            and library.is_file()
            and library.stat().st_size == size
            and sha256_of(library) == want
        ):
            done = {"state": "done", **about, "library": name, "sha256": want, "size": size, "identity": identity}
            return "done", done, library
        return "failed", failure(about or {}, "The build came out different from what it said.", False), None
    if built and status and status.get("state") == "failed":
        error = str(status.get("error") or "The build failed.")[:600]
        return "failed", failure(about or {}, error, bool(status.get("settled"))), None
    return "failed", failure(about or {}, DID_NOT_FINISH, False), None


def about_of(status, job):
    """The mod the answer is of: id, version, the .hmod's SHA-256 and the ABI, when they are plain
    and agree with `job`; else {} (and the index leaves it out)."""
    abi = job.split(".", 1)[1] if JOB.fullmatch(job) else None
    build = status.get("build")
    mod, version, source = status.get("id"), status.get("version"), status.get("source")
    if not (
        abi
        and isinstance(build, str) and BUILD.fullmatch(build)
        and isinstance(mod, str) and ID.fullmatch(mod)
        and isinstance(version, str) and VERSION.fullmatch(version)
        and isinstance(source, str) and SHA256.fullmatch(source)
        and job_of(source, abi) == job
    ):
        return {}
    return {"id": mod, "version": version, "source": source, "abi": abi, "build": build}


def failure(about, error, settled):
    return {"state": "failed", **about, "error": error, "settled": settled, "at": int(time.time())}


def ensure_release(gh, tag, title):
    """Release `tag`, made as a prerelease when it isn't there."""
    if gh("release", "view", tag, "--json", "tagName", check=False, quiet=True) != 0:
        gh("release", "create", tag, "--prerelease", "--title", title, "--notes", "", check=False)


def publish(result, build, gh, out, ask=None, built=True):
    """Puts the answer of every job in `result` up in mods-<build>, the library before its answer, and
    lets the sealed request go (one asked mod: `ask`, from the Compile service). The jobs are the
    `*.status.json` in `result`, and for an asked mod JOB even when its build left nothing."""
    if not BUILD.fullmatch(build) or (ask is not None and not re.fullmatch(r"[0-9a-f]{32}", ask)):
        sys.exit(f"Not a request: {build} {ask}")
    jobs = sorted({p.name[: -len(".status.json")] for p in result.glob("*.status.json")} if result.is_dir() else set())
    asked = os.environ.get("JOB")
    if asked:
        if not JOB.fullmatch(asked):
            sys.exit(f"Not a job: {asked}")
        jobs = [asked]
    jobs = [job for job in jobs if JOB.fullmatch(job)]
    tag = mods_tag(build)
    ensure_release(gh, tag, f"Mods for {build[:12]}")
    out.mkdir(parents=True, exist_ok=True)
    told = []
    for job in jobs:
        state, status, library = answer(result, job, built)
        if state == "done":
            so = out / f"{job}.so"
            os.replace(library, so)
            gh("release", "upload", tag, str(so), "--clobber")
            (out / f"{job}.json").write_text(json.dumps(status))
            gh("release", "upload", tag, str(out / f"{job}.json"), "--clobber")
            gh("release", "delete-asset", tag, f"{job}.failed.json", "-y", check=False)
        else:
            (out / f"{job}.failed.json").write_text(json.dumps(status))
            gh("release", "upload", tag, str(out / f"{job}.failed.json"), "--clobber")
        told.append({k: v for k, v in status.items() if k in ("id", "version", "state", "error")})
    if ask:
        gh("release", "delete-asset", "requests", f"{ask}.bin", "-y", check=False)
    print(json.dumps(told))


# ---- the index of one build -----------------------------------------------------------------------


def index(build, listed, read, previous=None):
    """mods-<build>/index.json from the release's assets `listed` ({name, id}), reading an answer
    (`read(name)`, its JSON) only when the index before (`previous`) doesn't hold it as it is: the
    index job runs after every build, one at a time. A library gone (clean.py) takes its mod out."""
    names = {a["name"]: a for a in listed}
    kept = {}
    for row in (previous or {}).get("mods", []) + (previous or {}).get("failed", []):
        if isinstance(row, dict) and isinstance(row.get("job"), str):
            kept[(row["job"], row.get("state"))] = row
    mods, failed = [], []
    for name, asset in sorted(names.items()):
        done = name.endswith(".json") and not name.endswith(".failed.json") and name != INDEX
        job = name[: -len(".failed.json")] if name.endswith(".failed.json") else name[: -len(".json")]
        if not (done or name.endswith(".failed.json")) or not JOB.fullmatch(job):
            continue
        # An answer the build replaced since (built again) is read again: its asset is another.
        row = kept.get((job, "done" if done else "failed"))
        if not row or row.get("asset") != asset["id"]:
            status = read(name)
            row = entry(status, job, asset["id"]) if isinstance(status, dict) else None
        if not row or row.get("build", build) != build:
            continue
        # A job once done is never built again (the Worker answers it at once), so its answer
        # stands over any failure before it that publish couldn't delete.
        if done:
            if f"{job}.so" in names:
                mods.append(row)
        elif f"{job}.json" not in names:
            failed.append(row)
    return {"schema": SCHEMA, "build": build, "mods": mods, "failed": failed}


def entry(status, job, asset):
    """One row of the index from an answer, with the fields the game reads, or None."""
    about = about_of(status, job)
    if not about:
        return None
    if status.get("state") == "done":
        name, sha, size, identity = (status.get(k) for k in ("library", "sha256", "size", "identity"))
        if not (
            isinstance(name, str) and LIBRARY.fullmatch(name)
            and isinstance(sha, str) and SHA256.fullmatch(sha)
            and isinstance(size, int) and size > 0
            and isinstance(identity, str)
        ):
            return None
        return {"state": "done", "job": job, **about, "file": f"{job}.so", "library": name, "sha256": sha,
                "size": size, "identity": identity, "asset": asset}
    if status.get("state") == "failed":
        return {"state": "failed", "job": job, **about, "error": str(status.get("error", ""))[:600],
                "settled": bool(status.get("settled")), "asset": asset}
    return None


def release_assets(tag):
    pages = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo()}/releases/tags/{tag}"],
        check=True, capture_output=True,
    ).stdout
    release = json.loads(pages)[0]
    pages = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo()}/releases/{release['id']}/assets?per_page=100"],
        check=True, capture_output=True,
    ).stdout
    return [{"name": a["name"], "id": a["id"]} for page in json.loads(pages) for a in page]


def write_index(build, gh, out):
    tag = mods_tag(build)
    listed = release_assets(tag)
    previous = fetch_json(download_url(tag, INDEX)) if any(a["name"] == INDEX for a in listed) else None
    made = index(build, listed, lambda name: fetch_json(download_url(tag, name)), previous)
    out.mkdir(parents=True, exist_ok=True)
    (out / INDEX).write_text(json.dumps(made, indent=1))
    gh("release", "upload", tag, str(out / INDEX), "--clobber")
    print(f"{tag}: {len(made['mods'])} mods, {len(made['failed'])} failed")


# ---- the builds the store serves -------------------------------------------------------------------


def register(builds, sdk_index, base, nightly, abis):
    """builds.json with the build whose SDK index is `sdk_index` (its sdk.json, at `base`), first:
    the game's runtime archive for each of `abis`, as sdk.json lists it. A build already there is
    replaced."""
    build = (sdk_index.get("build") or {}).get("commit", "")
    if sdk_index.get("schema") != 1 or not BUILD.fullmatch(build):
        raise ValueError("not an SDK index this store reads")
    if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/releases/download/[\w.+-]+/", base):
        raise ValueError(f"an SDK is a GitHub release's download folder, not {base}")
    games = {}
    for abi in abis:
        target = TARGETS[abi]
        name = f"runtime-{target}.tar.xz"
        asset = (sdk_index.get("assets") or {}).get(name)
        if not isinstance(asset, dict) or not SHA256.fullmatch(str(asset.get("sha256", ""))):
            raise ValueError(f"sdk.json has no {name}")
        games[abi] = {"target": target, "file": name, "url": base + name, "sha256": asset["sha256"],
                      "size": int(asset["size"])}
    row = {
        "build": build,
        "nightly": nightly or (sdk_index["build"].get("nightly")),
        "date": sdk_index["build"].get("date"),
        "game": sdk_index["build"].get("game"),
        "rust": (sdk_index.get("toolchain") or {}).get("rust"),
        "sdk": base,
        "abis": games,
    }
    rows = [b for b in (builds or {}).get("builds", []) if isinstance(b, dict) and b.get("build") != build]
    rows.append(row)
    rows.sort(key=lambda b: str(b.get("date") or ""), reverse=True)
    return {"schema": SCHEMA, "builds": rows}


# ---- what the catalog asks for --------------------------------------------------------------------


def plan(catalog, build, abis, have, game=None):
    """The catalog's native mods to build for `build`: every version of each (not revoked, for the
    build's vanilla `game` when it says one) and each ABI, but those its index `have` holds done or
    failed for good. Each: id, version, the .hmod's url, sha256 and size, its job."""
    revoked = {(r.get("id"), r.get("sha256")) for r in catalog.get("revoked", []) if isinstance(r, dict)}
    held = {row["job"] for row in (have or {}).get("mods", [])}
    held |= {row["job"] for row in (have or {}).get("failed", []) if row.get("settled")}
    planned, seen = [], set()
    for project in catalog.get("projects", []):
        if project.get("kind") != "native-mod" or not ID.fullmatch(str(project.get("id", ""))):
            continue
        if (project["id"], None) in revoked:
            continue
        for version in project.get("versions", []):
            file = version.get("file") or {}
            source = str(file.get("sha256", ""))
            if not SHA256.fullmatch(source) or (project["id"], source) in revoked:
                continue
            if game and version.get("game") not in (None, game):
                continue
            if not str(file.get("url", "")).startswith("https://"):
                continue
            if not (0 < int(file.get("size", 0)) <= LARGEST_MOD):
                continue
            for abi in abis:
                job = job_of(source, abi)
                if job in held or job in seen:
                    continue
                seen.add(job)
                planned.append({"id": project["id"], "version": str(version.get("version", "")), "url": file["url"],
                                "sha256": source, "size": int(file["size"]), "abi": abi, "job": job})
    return planned


def fetch_planned(planned, into):
    """Each planned .hmod into `into` as <sha256>.hmod, checked against the catalog's SHA-256; one
    that doesn't match is left out (and so not built)."""
    into.mkdir(parents=True, exist_ok=True)
    kept = []
    for row in planned:
        path = into / f"{row['sha256']}.hmod"
        if not path.is_file():
            body = fetch(row["url"])
            if body is None or hashlib.sha256(body).hexdigest() != row["sha256"]:
                print(f"{row['id']} {row['version']}: not the catalog's file, left out")
                continue
            path.write_bytes(body)
        kept.append(row)
    return kept


# ---- a build's SDK ---------------------------------------------------------------------------------

# The SDK slices a mod's library is built from (prebuilt_game.py `pack`): the game's runtime, which it
# links against, and the API crates' metadata. The engine slice is never read on this route.
SLICES = ("runtime", "api")


def fetch_sdk(row, abi, into, get=fetch):
    """Build `row`'s (builds.json) SDK for `abi` into `into`: sdk.json, and each slice in SLICES
    unpacked into into/<slice>, each checked against sdk.json's SHA-256 and size before it is
    unpacked, as plain files and folders only."""
    base = row["sdk"]
    index = get(base + "sdk.json")
    if index is None:
        raise ValueError(f"no sdk.json at {base}")
    listed = json.loads(index)
    if (listed.get("build") or {}).get("commit") != row["build"]:
        raise ValueError(f"{base}sdk.json is not build {row['build']}")
    into.mkdir(parents=True, exist_ok=True)
    (into / "sdk.json").write_bytes(index)
    for kind in SLICES:
        name = f"{kind}-{TARGETS[abi]}.tar.xz"
        asset = (listed.get("assets") or {}).get(name)
        if not isinstance(asset, dict):
            raise ValueError(f"sdk.json lists no {name}")
        body = get(base + name, limit=4 << 30)
        if body is None or len(body) != asset.get("size") or hashlib.sha256(body).hexdigest() != asset.get("sha256"):
            raise ValueError(f"{name} is not the one sdk.json says")
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:xz") as archive:
            archive.extractall(into / kind, filter="data")
    return listed


# ---- the GitHub CLI --------------------------------------------------------------------------------


def run_gh(*args, check=True, quiet=False):
    done = subprocess.run(["gh", *args], check=check, capture_output=quiet)
    return done.returncode


def abis():
    listed = [a.strip() for a in os.environ.get("ABIS", "arm64-v8a").split(",") if a.strip()]
    unknown = [a for a in listed if a not in TARGETS]
    if unknown:
        sys.exit(f"Not an ABI: {', '.join(unknown)}")
    return listed


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    build = os.environ.get("BUILD", "")
    if command != "register" and not BUILD.fullmatch(build):
        sys.exit(f"Not a build: {build!r}")
    if command == "publish":
        ask = os.environ.get("ASK") or None
        publish(pathlib.Path(argv[2]), build, run_gh, pathlib.Path("answer"), ask,
                os.environ.get("BUILT", "success") == "success")
    elif command == "index":
        write_index(build, run_gh, pathlib.Path("answer"))
    elif command == "plan":
        catalog = fetch_json(CATALOG)
        listed = fetch_json(download_url(STORE, BUILDS)) or {}
        known = next((b for b in listed.get("builds", []) if b.get("build") == build), None)
        if not known:
            sys.exit(f"{build} is not in the store's {BUILDS}: register it first")
        have = fetch_json(download_url(mods_tag(build), INDEX))
        planned = plan(catalog, build, [a for a in abis() if a in known["abis"]], have, known.get("game"))
        pathlib.Path(argv[2]).write_text(json.dumps(planned, indent=1))
        print(f"{len(planned)} to build for {build[:12]}")
        if out := os.environ.get("GITHUB_OUTPUT"):
            with open(out, "a") as f:
                f.write(f"count={len(planned)}\n")
    elif command == "fetch":
        planned = json.loads(pathlib.Path(argv[2]).read_text())
        kept = fetch_planned(planned, pathlib.Path(argv[3]))
        pathlib.Path(argv[2]).write_text(json.dumps(kept, indent=1))
    elif command == "sdk":
        listed = fetch_json(download_url(STORE, BUILDS)) or {}
        row = next((b for b in listed.get("builds", []) if b.get("build") == build), None)
        if not row:
            sys.exit(f"{build} is not in the store's {BUILDS}")
        for abi in abis():
            sdk = fetch_sdk(row, abi, pathlib.Path(argv[2]) / abi)
        if out := os.environ.get("GITHUB_ENV"):
            with open(out, "a") as f:
                f.write(f"RUST={(sdk.get('toolchain') or {}).get('rust', '')}\n")
    elif command == "register":
        base = os.environ["SDK"].rstrip("/") + "/"
        sdk_index = fetch_json(base + "sdk.json")
        if sdk_index is None:
            sys.exit(f"No sdk.json at {base}")
        if build and (sdk_index.get("build") or {}).get("commit") != build:
            sys.exit(f"{base}sdk.json is not build {build}")
        made = register(fetch_json(download_url(STORE, BUILDS)), sdk_index, base, os.environ.get("NIGHTLY"), abis())
        ensure_release(run_gh, STORE, "Compile store")
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / BUILDS
            path.write_text(json.dumps(made, indent=1))
            run_gh("release", "upload", STORE, str(path), "--clobber")
        print(f"{BUILDS}: {len(made['builds'])} builds")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
