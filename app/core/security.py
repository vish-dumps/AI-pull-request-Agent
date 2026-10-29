import hashlib
import hmac


def verify_github_signature(body: bytes, secret: str, signature: str | None) -> bool:
    """Compare the SHA-256 signature over the exact received bytes in constant time."""
    if not secret or not signature or not signature.isascii():
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
