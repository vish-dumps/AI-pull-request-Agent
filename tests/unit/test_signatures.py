import hashlib
import hmac

import pytest

from app.core.security import verify_github_signature


def test_github_documented_signature_vector():
    assert verify_github_signature(
        b"Hello, World!",
        "It's a Secret to Everybody",
        "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17",
    )


@pytest.mark.parametrize("signature", [None, "", "sha1=invalid", "sha256=wrong", "sha256=\u00e9"])
def test_missing_malformed_and_non_ascii_signatures_are_rejected(signature):
    assert not verify_github_signature(b"{}", "secret", signature)


def test_signatures_cover_exact_utf8_bytes():
    body = '{"title":"Fix caf\u00e9"}'.encode()
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_github_signature(body, "secret", signature)
    assert not verify_github_signature(body + b"\n", "secret", signature)
    assert not verify_github_signature(body, "other-secret", signature)
    assert not verify_github_signature(body, "", signature)
