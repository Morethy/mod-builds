"""Sunrise's signed v2 registry index (sunrise-org/sunrise-mods docs/registry-v2.md, "Reader rules"), as the
store's plan reads it: `v2/index.signed` is the minisign signature file's four lines, then the exact bytes of
`index.json`, checked with the registry's public key before they are parsed. Sunrise stopped serving the old
`index.json` on 2026-11-26; the game reads this file too (sunrise-addresses MOD_REGISTRY_INDEX, the key as
cargo-sunrise-mod's registry_source.rs has it).

    python3 scripts/registry_v2.py [FILE]    # the catalog store.py plans from, a summary of it (FILE: a local copy)

The store keeps no state between runs, so the rule against an older serial is not applied here: each run reads
the live file, and every package it builds is checked against the SHA-256 this signed index gives."""
import base64
import hashlib
import json
import pathlib
import re
import sys

INDEX = "https://sunrise-org.github.io/sunrise-mods/v2/index.signed"
KEY = "RWTNJaFJ4AekHG/RgVrgmJpvy9B2EaeqbdSNuseKUx2XPxaOZkChLV0q"
SCHEMA = 2
SHA256 = re.compile(r"[0-9a-f]{64}")

# ---- Ed25519 verification (RFC 8032, section 5.1.7) ----------------------------------------------

_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    p1 = (y1 - x1) * (y2 - x2) % _P
    p2 = (y1 + x1) * (y2 + x2) % _P
    p3 = 2 * t1 * t2 * _D % _P
    p4 = 2 * z1 * z2 % _P
    e, f, g, h = p2 - p1, p4 - p3, p4 + p3, p2 + p1
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _mul(scalar, point):
    out = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            out = _add(out, point)
        point = _add(point, point)
        scalar >>= 1
    return out


def _x(y, sign):
    if y >= _P:
        return None
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if xx == 0:
        return None if sign else 0
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - xx) % _P:
        return None
    return _P - x if (x & 1) != sign else x


def _point(encoded):
    if len(encoded) != 32:
        return None
    y = int.from_bytes(encoded, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_G = (_x(_GY, 0), _GY, 1, _x(_GY, 0) * _GY % _P)


def ed25519_verify(public, message, signature):
    """Whether `signature` (64 bytes) is `public`'s (32 bytes) over `message`."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a, r = _point(public), _point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _Q:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _Q
    left, right = _mul(s, _G), _add(r, _mul(h, a))
    return (left[0] * right[2] - right[0] * left[2]) % _P == 0 and (left[1] * right[2] - right[1] * left[2]) % _P == 0


# ---- minisign ---------------------------------------------------------------------------------------


def minisign_verify(key, signature_text, message):
    """Raise ValueError unless `signature_text` (a .minisig file's four lines) is `key`'s (minisign's base64 public
    key) over `message`, its trusted comment included."""
    raw_key = base64.b64decode(key)
    if len(raw_key) != 42 or raw_key[:2] != b"Ed":
        raise ValueError("not a minisign Ed25519 public key")
    lines = signature_text.split("\n")
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith("trusted comment: "):
        raise ValueError("not a minisign signature file")
    sig = base64.b64decode(lines[1])
    if len(sig) != 74 or sig[:2] not in (b"Ed", b"ED"):
        raise ValueError("not a minisign Ed25519 signature")
    if sig[2:10] != raw_key[2:10]:
        raise ValueError("signed by another key")
    signed = hashlib.blake2b(message, digest_size=64).digest() if sig[:2] == b"ED" else message
    if not ed25519_verify(raw_key[10:], signed, sig[10:]):
        raise ValueError("the signature doesn't match the registry's key")
    trusted = lines[2][len("trusted comment: "):].encode("utf-8")
    if not ed25519_verify(raw_key[10:], sig[10:] + trusted, base64.b64decode(lines[3])):
        raise ValueError("the trusted comment's signature doesn't match the registry's key")


def verified(signed, key=KEY):
    """The index in `signed` (index.signed's bytes), parsed once its signature is checked over its exact bytes."""
    at = 0
    for _ in range(4):
        at = signed.index(b"\n", at) + 1
    minisign_verify(key, signed[:at].decode("utf-8"), signed[at:])
    index = json.loads(signed[at:])
    if index.get("schema") != SCHEMA:
        raise ValueError(f"schema {index.get('schema')} where {SCHEMA} is expected")
    return index


# ---- the store's catalog shape ----------------------------------------------------------------------


def _version_key(text):
    """A semver's order (pre-releases before their release), for newest first."""
    core, _, pre = str(text).partition("-")
    numbers = [int(n) if n.isdigit() else 0 for n in core.split("+")[0].split(".")]
    return numbers, not pre, pre


def catalog(index):
    """The v2 index as store.py's plan reads a catalog: per project its in-game ID (`mod`, the slot a port is
    named by), kind and releases newest first, each with its file's url, size and SHA-256; takedowns by slot."""
    slots = {p.get("id"): p.get("mod") for p in index.get("projects", [])}
    projects = []
    for p in index.get("projects", []):
        releases = sorted(p.get("releases", []), key=lambda r: _version_key(r.get("version")), reverse=True)
        versions = []
        for r in releases:
            file = r.get("file") or {}
            if SHA256.fullmatch(str(file.get("sha256", ""))):
                versions.append({"version": str(r.get("version", "")),
                                 "file": {"url": file.get("url"), "size": file.get("size"), "sha256": file["sha256"]}})
        projects.append({"id": p.get("mod"), "project": p.get("id"), "kind": p.get("kind"), "versions": versions})
    revoked = [{"id": slots[r["project"]], "sha256": r.get("sha256")}
               for r in index.get("revoked", []) if slots.get(r.get("project"))]
    return {"projects": projects, "revoked": revoked}


def fetch_catalog(fetch):
    """The live index as a catalog (`fetch(url)`: the bytes, None on a 404), or None when it isn't there."""
    signed = fetch(INDEX)
    return None if signed is None else catalog(verified(signed))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        shaped = catalog(verified(pathlib.Path(sys.argv[1]).read_bytes()))
    else:
        import store
        shaped = fetch_catalog(lambda url: store.fetch(url, 64 << 20))
    kinds = {}
    for p in shaped["projects"]:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(f"{len(shaped['projects'])} projects {kinds}, {sum(len(p['versions']) for p in shaped['projects'])} "
          f"releases, {len(shaped['revoked'])} takedowns")
