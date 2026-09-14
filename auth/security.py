from __future__ import annotations
import hashlib
import hmac
import secrets
import re
from typing import Tuple

PBKDF2_ITERATIONS = 100000
HASH_ALGORITHM = 'sha256'


def hash_password(password: str) -> str:
    """Securely hash a password using PBKDF2-HMAC-SHA256 with a unique random salt (OWASP standard)."""
    if not password:
        raise ValueError("Password cannot be empty")
    salt = secrets.token_hex(16)
    pw_bytes = password.encode('utf-8')
    salt_bytes = salt.encode('utf-8')
    key = hashlib.pbkdf2_hmac(HASH_ALGORITHM, pw_bytes, salt_bytes, PBKDF2_ITERATIONS)
    key_hex = key.hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${key_hex}"


def verify_password(password: str, hashed_password: str) -> bool:
    """Verify a plain-text password against a stored PBKDF2 hash using constant-time comparison."""
    if not password or not hashed_password:
        return False
    try:
        parts = hashed_password.split('$')
        if len(parts) != 4:
            return False
        algorithm, iterations_str, salt, stored_key_hex = parts
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_str)
        pw_bytes = password.encode('utf-8')
        salt_bytes = salt.encode('utf-8')
        computed_key = hashlib.pbkdf2_hmac(HASH_ALGORITHM, pw_bytes, salt_bytes, iterations)
        return hmac.compare_digest(computed_key.hex(), stored_key_hex)
    except Exception:
        return False


def validate_email_format(email: str) -> bool:
    """Validate standard email format."""
    if not email or len(email) > 254:
        return False
    email_regex = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    return bool(re.match(email_regex, email.strip()))


def validate_password_strength(password: str) -> Tuple[bool, str]:
    """Validate password meets minimum security standards."""
    if not password:
        return False, "Password is required"
    if len(password) < 6:
        return False, "Password must be at least 6 characters long"
    if len(password) > 128:
        return False, "Password is too long (maximum 128 characters)"
    return True, ""
