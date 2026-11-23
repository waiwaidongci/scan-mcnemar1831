"""口令摘要与无第三方依赖的 HMAC 签名令牌。"""

import hashlib
import hmac
import secrets
import time
from pathlib import Path

from .config import SECRET_PATH, ensure_dirs

_TTL_SECONDS = 8 * 3600


def _load_secret():
    ensure_dirs()
    if not SECRET_PATH.exists():
        SECRET_PATH.write_bytes(secrets.token_bytes(32))
        SECRET_PATH.chmod(0o600)
    return SECRET_PATH.read_bytes()


SECRET = _load_secret()


def digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 210_000).hex()


def hash_password(password):
    salt = secrets.token_hex(16)
    return f"{salt}${digest(password, salt)}"


def matches(password, encoded):
    salt, expected = encoded.split("$", maxsplit=1)
    return hmac.compare_digest(digest(password, salt), expected)


def _sign(body):
    return hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()


def issue_token(username, role):
    expiry = int(time.time()) + _TTL_SECONDS
    body = f"{username}|{role}|{expiry}"
    return f"{body}|{_sign(body)}"


def verify_token(token):
    try:
        username, role, expiry, signature = token.split("|")
        body = f"{username}|{role}|{expiry}"
    except (ValueError, AttributeError):
        return None
    if not hmac.compare_digest(signature, _sign(body)):
        return None
    if int(expiry) < int(time.time()):
        return None
    return {"username": username, "role": role}
