"""registry_v2.py: the signed v2 index checked and shaped as store.py plans from it.
python -m unittest discover -s scripts"""
import base64
import hashlib
import json
import unittest

import registry_v2 as v2

# RFC 8032 section 7.1, test 1.
SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
EMPTY_SIGNATURE = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
KEY_ID = b"\x01\x02\x03\x04\x05\x06\x07\x08"


def encode(point):
    zinv = pow(point[2], v2._P - 2, v2._P)
    x, y = point[0] * zinv % v2._P, point[1] * zinv % v2._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def sign(secret, message):
    """RFC 8032's Ed25519 signature, for the fixtures."""
    h = hashlib.sha512(secret).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    public = encode(v2._mul(a, v2._G))
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % v2._Q
    big_r = encode(v2._mul(r, v2._G))
    k = int.from_bytes(hashlib.sha512(big_r + public + message).digest(), "little") % v2._Q
    return big_r + ((r + k * a) % v2._Q).to_bytes(32, "little")


def public_key(key_id=KEY_ID):
    return base64.b64encode(b"Ed" + key_id + PUBLIC).decode()


def signed(index, secret=SECRET, key_id=KEY_ID, trusted="timestamp:1 file:index.json"):
    body = json.dumps(index).encode()
    sig = sign(secret, hashlib.blake2b(body, digest_size=64).digest())
    glob = sign(secret, sig + trusted.encode())
    head = (f"untrusted comment: signature from the registry\n{base64.b64encode(b'ED' + key_id + sig).decode()}\n"
            f"trusted comment: {trusted}\n{base64.b64encode(glob).decode()}\n")
    return head.encode() + body


def release(version, digest):
    return {"version": version, "published": "2026-11-26", "fits": [], "workflow": "w@refs/tags/release-v1",
            "file": {"url": f"https://example.com/{version}.smod", "size": 10, "sha256": digest * 64,
                     "attestation": "https://example.com/a.sigstore.json"}}


INDEX = {
    "schema": 2, "serial": 7, "generated": "2026-11-27T00:00:00Z", "builds": [], "categories": [], "collections": [],
    "projects": [
        {"id": "AbCdEfGh", "slug": "villagefolk", "mod": "villagefolk", "kind": "native-mod", "name": "Villagefolk",
         "releases": [release("1.0.0", "a"), release("1.2.0", "b"), release("1.10.0", "c")]},
        {"id": "ZyXwVuTs", "slug": "tiny", "mod": "tiny", "kind": "mod", "name": "Tiny",
         "releases": [release("0.1.0", "d")]},
    ],
    "revoked": [{"project": "AbCdEfGh", "sha256": "a" * 64}, {"project": "Unknown1", "sha256": None}],
}


class Ed25519(unittest.TestCase):
    def test_rfc_8032_vector(self):
        self.assertTrue(v2.ed25519_verify(PUBLIC, b"", EMPTY_SIGNATURE))
        self.assertEqual(sign(SECRET, b""), EMPTY_SIGNATURE)

    def test_a_changed_message_or_signature_fails(self):
        self.assertFalse(v2.ed25519_verify(PUBLIC, b"x", EMPTY_SIGNATURE))
        self.assertFalse(v2.ed25519_verify(PUBLIC, b"", EMPTY_SIGNATURE[:-1] + b"\x0c"))


class Verified(unittest.TestCase):
    def test_reads_a_signed_index(self):
        self.assertEqual(v2.verified(signed(INDEX), public_key())["serial"], 7)

    def test_refuses_a_changed_byte(self):
        data = bytearray(signed(INDEX))
        data[-3] ^= 1
        with self.assertRaisesRegex(ValueError, "doesn't match"):
            v2.verified(bytes(data), public_key())

    def test_refuses_a_changed_trusted_comment(self):
        data = signed(INDEX).replace(b"timestamp:1", b"timestamp:2", 1)
        with self.assertRaisesRegex(ValueError, "trusted comment"):
            v2.verified(data, public_key())

    def test_refuses_another_key(self):
        with self.assertRaisesRegex(ValueError, "another key"):
            v2.verified(signed(INDEX, key_id=b"\x09" * 8), public_key())

    def test_refuses_another_schema(self):
        with self.assertRaisesRegex(ValueError, "schema 3"):
            v2.verified(signed({**INDEX, "schema": 3}), public_key())


class Catalog(unittest.TestCase):
    def test_slots_newest_first_and_takedowns_by_slot(self):
        shaped = v2.catalog(INDEX)
        villagefolk, tiny = shaped["projects"]
        self.assertEqual((villagefolk["id"], villagefolk["kind"], tiny["id"], tiny["kind"]),
                         ("villagefolk", "native-mod", "tiny", "mod"))
        self.assertEqual([v["version"] for v in villagefolk["versions"]], ["1.10.0", "1.2.0", "1.0.0"])
        self.assertEqual(villagefolk["versions"][0]["file"],
                         {"url": "https://example.com/1.10.0.smod", "size": 10, "sha256": "c" * 64})
        self.assertEqual(shaped["revoked"], [{"id": "villagefolk", "sha256": "a" * 64}])

    def test_the_store_plans_from_it(self):
        import store
        planned = store.plan(v2.catalog(INDEX), "5e1f0c2d3a4b5c6d7e8f90a1b2c3d4e5f6a7b8c9", ["arm64-v8a"], None)
        self.assertEqual(sorted((row["id"], row["version"]) for row in planned),
                         [("tiny", "0.1.0"), ("villagefolk", "1.10.0"), ("villagefolk", "1.2.0")])


if __name__ == "__main__":
    unittest.main()
