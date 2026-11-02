"""Encryption at rest for Twitch tokens (Fernet = AES-128-CBC + HMAC-SHA256)."""
from cryptography.fernet import Fernet

from backend import config


def _fernet() -> Fernet:
    if not config.ENCRYPTION_KEY:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    return Fernet(config.ENCRYPTION_KEY.encode())


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
