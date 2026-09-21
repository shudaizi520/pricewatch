"""Encryption for secrets stored in SQLite."""

import base64
import hashlib

from cryptography.fernet import Fernet


class SecretBox:
    """Encrypt and decrypt settings using a key derived from APP_SECRET_KEY."""

    def __init__(self, application_secret: str) -> None:
        digest = hashlib.sha256(application_secret.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
