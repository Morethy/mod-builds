#!/usr/bin/env python3
"""A Compile v2 build server on one Linux machine (vps/README.md): what Starling's Worker and mod.yml do together,
with this machine as the builder. The phone sends a mod the store doesn't have; this builds it in the sandbox
(scripts/sandbox.sh) beside the game build's base (scripts/store_base.sh) and puts the answer in a release of
your fork of this repository (scripts/store.py), where the phone downloads it, as it does from Starling's own.

    POST /v2/mod?build=B&abi=A   the mod's .hmod in the body: {"job", "repo", "tag"}, or {"error"}
    GET  /v2/mod/B/J             {"state": "queued" | "building", "ahead"} while it waits, then its answer

One build at a time. A base is made the first time a build is asked for (half an hour or so) and kept; one base
at a time, the newest asked for. Python's standard library only; behind HTTPS (Caddy), on 127.0.0.1.

    STARLING_REPO   your fork, owner/name: the answers go to its releases mods-<build> (public)
    GH_TOKEN        a fine-grained token for that repository only, Contents read and write (the gh CLI reads it)
    PORT            default 8790
    MAX_PER_DAY     builds an address may ask for in a day, default 20
"""
import hashlib
import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent.parent
REPO = os.environ["STARLING_REPO"]
MAIN = "starling-client/mod-builds"
PORT = int(os.environ.get("PORT", "8790"))
MAX_PER_DAY = int(os.environ.get("MAX_PER_DAY", "20"))
LARGEST = 32 << 20
WORK = pathlib.Path.home() / "vps-work"
BUILD = re.compile(r"^[0-9a-f]{40}$")
ABIS = ("arm64-v8a",)

lock = threading.Lock()
wake = threading.Condition(lock)
line = []  # (build, abi, job, body path), in order
building = None
asked = {}  # address -> [times]


def release_file(tag, name):
    """The JSON of `name` in `tag` of your fork, or None."""
    url = f"https://github.com/{REPO}/releases/download/{tag}/{name}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def builds():
    """Starling's store's game builds: the ones a phone may ask for."""
    url = f"https://github.com/{MAIN}/releases/download/store/builds.json"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read())


def run(command, env=None, cwd=HERE):
    print("$", " ".join(command), flush=True)
    return subprocess.run(command, cwd=cwd, env={**os.environ, **(env or {})}).returncode


def ensure_base(build, abi, profile):
    """scripts/store_base.sh for `build`, unless the base there is it. It replaces ~/store, ~/.rustup and
    ~/android's NDK: run this server as a user of its own."""
    marker = pathlib.Path.home() / "store" / ".base"
    if marker.is_file() and marker.read_text().strip() == f"{build} {abi} {profile}":
        return True
    android = os.environ.get("ANDROID_HOME", str(pathlib.Path.home() / "android-sdk"))
    if run(["bash", "scripts/store_base.sh"], {"BUILD": build, "ABI": abi, "PROFILE": profile,
                                               "ANDROID_HOME": android, "RUNNER_TEMP": str(WORK)}) != 0:
        return False
    # store_base.sh leaves Cargo's home in the base, where the sandbox reads it; builds find it there.
    marker.write_text(f"{build} {abi} {profile}\n")
    return True


def build_one(build, abi, job, body):
    profile = next((b.get("profile") or "fast" for b in builds() if b.get("build") == build), "fast")
    folder = WORK / job
    shutil.rmtree(folder, ignore_errors=True)
    (folder / "mods").mkdir(parents=True)
    source = hashlib.sha256(body.read_bytes()).hexdigest()
    shutil.copyfile(body, folder / "mods" / f"{source}.hmod")
    (folder / "jobs.json").write_text(json.dumps([{"job": job, "abi": abi, "sha256": source}]))
    env = {"BUILD": build, "ABI": abi, "PROFILE": profile, "JOB": job, "GH_REPO": REPO, "SANDBOX_SECONDS": "1200"}
    built = ensure_base(build, abi, profile)
    if built:
        run(["bash", str(HERE / "scripts/sandbox.sh"), "jobs.json", "mods", "result"], env, cwd=folder)
    # The answer, a library or a failure, goes up whatever happened: a phone waits for one.
    run(["python3", str(HERE / "scripts/store.py"), "publish", str(folder / "result")],
        {**env, "BUILT": "success" if built else "failure"}, cwd=folder)
    run(["python3", str(HERE / "scripts/store.py"), "index"], env, cwd=folder)
    shutil.rmtree(folder, ignore_errors=True)
    body.unlink(missing_ok=True)


def worker():
    global building
    while True:
        with wake:
            while not line:
                wake.wait()
            building = line.pop(0)
        try:
            build_one(*building)
        except Exception as e:  # noqa: BLE001 - one job's trouble must not stop the line
            print(f"job {building[2]}: {e}", flush=True)
        finally:
            with lock:
                building = None


class Handler(http.server.BaseHTTPRequestHandler):
    def answer(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def address(self):
        # Behind Caddy: the player's address is the last hop it adds.
        return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[-1].strip()

    def do_POST(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        build, abi = params.get("build", ""), params.get("abi", "")
        if path != "/v2/mod":
            return self.answer(404, {"error": "No such thing here."})
        if not BUILD.match(build) or abi not in ABIS or not any(b.get("build") == build for b in builds()):
            return self.answer(400, {"error": "This server doesn't build for that game."})
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= LARGEST:
            return self.answer(413, {"error": "That is too big to compile."})
        body = self.rfile.read(size)
        job = f"{hashlib.sha256(body).hexdigest()[:32]}.{abi}"
        tag = f"mods-{build}"
        told = {"job": job, "repo": REPO, "tag": tag}
        if release_file(tag, f"{job}.json") or (release_file(tag, f"{job}.failed.json") or {}).get("settled"):
            return self.answer(200, told)
        with wake:
            if any(entry[2] == job for entry in line) or (building and building[2] == job):
                return self.answer(200, told)
            now = time.time()
            times = [t for t in asked.get(self.address(), []) if now - t < 86400]
            if len(times) >= MAX_PER_DAY:
                return self.answer(429, {"error": "This server's builds for you today are used up."})
            asked[self.address()] = times + [now]
            WORK.mkdir(parents=True, exist_ok=True)
            saved = WORK / f"{job}.hmod"
            saved.write_bytes(body)
            line.append((build, abi, job, saved))
            wake.notify()
        return self.answer(200, told)

    def do_GET(self):
        parts = self.path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["v2", "mod"] or not BUILD.match(parts[2]):
            return self.answer(404, {"error": "No such thing here."})
        build, job = parts[2], parts[3]
        done = release_file(f"mods-{build}", f"{job}.json")
        if done:
            return self.answer(200, done)
        with lock:
            if building and building[2] == job:
                return self.answer(200, {"state": "building"})
            for ahead, entry in enumerate(line):
                if entry[2] == job:
                    return self.answer(200, {"state": "queued", "ahead": ahead + (1 if building else 0)})
        failure = release_file(f"mods-{build}", f"{job}.failed.json")
        if failure:
            return self.answer(200, {"state": "failed", "error": failure.get("error"),
                                     "settled": bool(failure.get("settled"))})
        return self.answer(404, {"error": "No such job."})

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
