import unittest
from unittest import mock

from webhook_bridge.signature import DEMO_SECRET, SECRET_ENV, load_secret, sign, verify

SECRET = "test-secret-fictitious"
BODY = b'{"event_id":"evt-1","type":"ticket.created","data":{}}'


class TestSignature(unittest.TestCase):
    def test_valid_signature_accepted(self) -> None:
        header = sign(SECRET, BODY)
        self.assertTrue(header.startswith("sha256="))
        self.assertEqual(len(header), len("sha256=") + 64)
        self.assertTrue(verify(SECRET, BODY, header))

    def test_tampered_body_rejected(self) -> None:
        header = sign(SECRET, BODY)
        self.assertFalse(verify(SECRET, BODY.replace(b"evt-1", b"evt-2"), header))

    def test_wrong_secret_rejected(self) -> None:
        self.assertFalse(verify(SECRET, BODY, sign("another-secret", BODY)))

    def test_missing_or_malformed_header_rejected(self) -> None:
        digest = sign(SECRET, BODY)[len("sha256="):]
        for header in (None, "", "sha256=", digest, "md5=" + digest, "sha256=" + digest.upper(), "sha256=zzé"):
            with self.subTest(header=header):
                self.assertFalse(verify(SECRET, BODY, header))

    def test_secret_from_env_overrides_demo_value(self) -> None:
        with mock.patch.dict("os.environ", {SECRET_ENV: "from-env-fictitious"}):
            self.assertEqual(load_secret(), ("from-env-fictitious", True))
        with mock.patch.dict("os.environ", {SECRET_ENV: ""}):
            self.assertEqual(load_secret(), (DEMO_SECRET, False))


if __name__ == "__main__":
    unittest.main()
