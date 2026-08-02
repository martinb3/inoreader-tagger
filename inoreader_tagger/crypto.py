"""Encryption for Inoreader refresh tokens at rest.

Refresh tokens are long-lived credentials to somebody's reading account, so they
are never written to the database in the clear. The key comes from
ENCRYPTION_KEY when set; otherwise it is generated once and kept in the data
directory, which keeps a fresh deployment zero-config while still surviving
restarts.
"""

import hashlib
import hmac
import logging
import os
import stat

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

KEY_FILENAME = ".encryption_key"


class TokenDecryptionError(Exception):
    """Raised when a stored token cannot be decrypted with the current key."""


def _load_or_create_key(data_dir: str) -> bytes:
    key_path = os.path.join(data_dir, KEY_FILENAME)

    if os.path.exists(key_path):
        with open(key_path, "rb") as handle:
            return handle.read().strip()

    os.makedirs(data_dir, exist_ok=True)
    key = Fernet.generate_key()

    # Write with restrictive permissions from the start rather than chmod-ing
    # after the fact, so the key is never briefly world-readable.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)

    logger.warning(
        "Generated a new encryption key at %s. Back this file up: without it, "
        "every stored refresh token becomes unreadable and users must reconnect.",
        key_path,
    )
    return key


class TokenCipher:
    """Symmetric encryption for secrets held in the database."""

    def __init__(self, key: bytes):
        self._key = key
        self._fernet = Fernet(key)

    def derive_secret(self, label: str) -> str:
        """Deterministically derive a named secret from the master key.

        Used for the cookie signing key. It must be stable across restarts —
        encrypt() cannot be used for this because Fernet is randomised, so it
        would invalidate every session on each redeploy.
        """
        return hmac.new(self._key, label.encode(), hashlib.sha256).hexdigest()

    @classmethod
    def from_settings(cls, settings) -> "TokenCipher":
        env_key = os.environ.get("ENCRYPTION_KEY")
        if env_key:
            return cls(env_key.strip().encode())
        return cls(_load_or_create_key(settings.data_dir))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise TokenDecryptionError(
                "Stored token could not be decrypted. The encryption key has "
                "most likely changed; the user needs to reconnect Inoreader."
            ) from exc
