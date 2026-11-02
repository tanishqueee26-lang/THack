import hashlib
import hmac

from backend.twitch import verify_signature


def _sig(secret, mid, ts, body):
    return "sha256=" + hmac.new(secret.encode(), (mid + ts).encode() + body, hashlib.sha256).hexdigest()


def test_valid_signature():
    body = b'{"a":1}'
    assert verify_signature("s3cret", "id1", "2026-11-02T10:00:00Z", body, _sig("s3cret", "id1", "2026-11-02T10:00:00Z", body))


def test_tampered_body_rejected():
    s = _sig("s3cret", "id1", "t", b"{}")
    assert not verify_signature("s3cret", "id1", "t", b"{ }", s)


def test_missing_signature_rejected():
    assert not verify_signature("s3cret", "id1", "t", b"{}", "")
