"""Password and session primitives; no provider credentials in this runtime."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from threading import BoundedSemaphore
from uuid import uuid4

from .db import Database

_HASH_SLOTS = BoundedSemaphore(2)


def password_hash(password: str, salt: str | None = None) -> str:
    if not 12 <= len(password) <= 128:
        raise ValueError("Password must have 12–128 characters")
    salt = salt or secrets.token_hex(16)
    with _HASH_SLOTS:
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                                n=2**17, r=8, p=1, maxmem=256 * 1024 * 1024).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, _ = encoded.split("$")
        if algorithm != "scrypt":
            return False
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_user(database: Database, username: str, password: str) -> str:
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.@+-]{2,79}", username):
        raise ValueError("Username must have 3–80 ASCII letters, digits or _.@+-")
    encoded = password_hash(password)
    user_id = str(uuid4())
    with database.connection() as db:
        db.execute("INSERT INTO users VALUES (%s,%s,%s)", (user_id, username, encoded))
    return user_id
