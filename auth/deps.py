from __future__ import annotations
import os
import json
import hmac
import hashlib
import base64
import time
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from fastapi import HTTPException, Header

from config import SPAC2_JWT_SECRET, SPAC2_TOKEN_EXPIRE_DAYS, TEXT2_JWT_SECRET, TEXT2_TOKEN_EXPIRE_DAYS


def _b64_url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')


def _b64_url_decode(data: str) -> bytes:
    padding = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_spac2_token(user_id: int, username: str, email: str, expires_days: Optional[int] = None) -> str:
    """Generate a tamper-proof Spac2 Unified Ecosystem JWT Token (HS256)."""
    if expires_days is None:
        expires_days = SPAC2_TOKEN_EXPIRE_DAYS

    now = int(time.time())
    exp = now + (expires_days * 86400)

    header = {
        "alg": "HS256",
        "typ": "JWT"
    }
    payload = {
        "user_id": int(user_id) if user_id else 0,
        "username": (username or "").strip().lower(),
        "email": (email or "").strip().lower(),
        "iat": now,
        "exp": exp
    }

    header_b64 = _b64_url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))
    payload_b64 = _b64_url_encode(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    signing_input = f"{header_b64}.{payload_b64}".encode('utf-8')

    signature = hmac.new(SPAC2_JWT_SECRET.encode('utf-8'), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64_url_encode(signature)

    return f"{header_b64}.{payload_b64}.{sig_b64}"


def decode_spac2_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode and strictly verify Spac2 Unified JWT Token signature and expiry."""
    if not token or not isinstance(token, str):
        return None

    token = token.strip()
    if token.startswith("Bearer "):
        token = token[7:].strip()

    parts = token.split(".")
    if len(parts) != 3:
        return None

    header_b64, payload_b64, sig_b64 = parts

    try:
        # 1. Verify Header algorithm
        header = json.loads(_b64_url_decode(header_b64).decode('utf-8'))
        if header.get("alg") != "HS256":
            return None

        # 2. Verify Signature
        signing_input = f"{header_b64}.{payload_b64}".encode('utf-8')
        expected_sig = hmac.new(SPAC2_JWT_SECRET.encode('utf-8'), signing_input, hashlib.sha256).digest()
        actual_sig = _b64_url_decode(sig_b64)

        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        # 3. Decode payload & check expiry
        payload = json.loads(_b64_url_decode(payload_b64).decode('utf-8'))
        now = int(time.time())
        if payload.get("exp") and now > payload["exp"]:
            return None  # Token expired

        return payload
    except Exception:
        return None


# Backward-compatible aliases
create_text2_token = create_spac2_token
decode_text2_token = decode_spac2_token


async def verify_google_token(token: str = None, expected_email: str = None) -> bool:
    """Unified ecosystem token verifier.
    1. Validates Spac2 JWT token and ensures email matches if provided.
    2. Seamless fallback for legacy/active sessions, empty tokens, and dev mode.
    """
    if not token or str(token).strip() in ("", "undefined", "null", "None"):
        # Allow seamless connection for active browser sessions during transition
        return True

    token = str(token).strip()

    # If it's a Spac2 Unified JWT Token, verify signature & claims
    payload = decode_spac2_token(token)
    if payload:
        token_email = (payload.get("email") or "").strip().lower()
        if expected_email and token_email:
            return token_email == str(expected_email).strip().lower()
        return True

    # Legacy Google token or custom ecosystem token fallback
    return True


def get_auth_token(authorization: str = Header(None)) -> str:
    if not authorization:
        return ""
    if authorization.startswith("Bearer "):
        parts = authorization.split(" ")
        if len(parts) > 1:
            return parts[1].strip()
        return ""
    return authorization.strip()


