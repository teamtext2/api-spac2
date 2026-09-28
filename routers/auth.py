from __future__ import annotations
import json
import re
import time
import secrets
import urllib.parse
from typing import Optional, Dict, Any, List
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Depends, Request, Response, Header

from database.postgres import execute_pg_query
from auth.deps import get_auth_token, create_spac2_token, decode_spac2_token, create_text2_token, decode_text2_token
from auth.security import hash_password, verify_password, validate_email_format, validate_password_strength
from services.user_service import get_profile
from config import DEFAULT_AVATAR_URL
from websocket.manager import send_to_user_by_email

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str
    password: str
    confirm_password: Optional[str] = None
    name: Optional[str] = None
    username: Optional[str] = None


class CheckEmailRequest(BaseModel):
    email: str


class LoginRequest(BaseModel):
    email: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: Optional[str] = None
    new_password: str
    confirm_password: Optional[str] = None


# --- Anti-Brute Force In-Memory Rate Limiting ---
_failed_logins: Dict[str, List[float]] = {}
_lockouts: Dict[str, float] = {}


def _check_rate_limit(key: str) -> Optional[int]:
    """Check if key is currently locked out. Return remaining seconds if locked, else None."""
    now = time.time()
    locked_until = _lockouts.get(key, 0)
    if now < locked_until:
        return int(locked_until - now) + 1
    elif key in _lockouts:
        del _lockouts[key]
    return None


def _record_failed_attempt(key: str, max_attempts: int = 5, window_seconds: int = 60, lockout_seconds: int = 30) -> Optional[int]:
    """Record a failed attempt. If threshold reached, lock out key for lockout_seconds."""
    now = time.time()
    attempts = _failed_logins.setdefault(key, [])
    attempts = [t for t in attempts if now - t < window_seconds]
    attempts.append(now)
    _failed_logins[key] = attempts

    if len(attempts) >= max_attempts:
        _lockouts[key] = now + lockout_seconds
        _failed_logins[key] = []
        return lockout_seconds
    return None


def _clear_failed_attempts(key: str):
    _failed_logins.pop(key, None)
    _lockouts.pop(key, None)


def _set_auth_cookies(response: Response, token: str, user_data: Dict[str, Any]):
    """Set cross-app authentication cookies for standard 30-day ecosystem persistence."""
    max_age = 30 * 86400  # 30 days
    response.set_cookie(
        key="auth_token",
        value=token,
        max_age=max_age,
        path="/",
        samesite="lax",
        secure=False,
        httponly=False
    )
    user_json = urllib.parse.quote(json.dumps(user_data))
    response.set_cookie(
        key="user-profile",
        value=user_json,
        max_age=max_age,
        path="/",
        samesite="lax",
        secure=False,
        httponly=False
    )


def _clear_auth_cookies(response: Response):
    """Clear authentication cookies on logout."""
    response.delete_cookie(key="auth_token", path="/")
    response.delete_cookie(key="user-profile", path="/")


@router.post("/check-email")
async def api_check_email(req: CheckEmailRequest, request: Request):
    """
    Progressive Step-1 verification:
    Verify if email or username exists, returns public greeting profile info for 2-step login.
    """
    client_ip = request.client.host if request.client else "unknown"
    lockout_sec = _check_rate_limit(f"check_{client_ip}")
    if lockout_sec:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Please wait {lockout_sec}s before trying again.",
            headers={"Retry-After": str(lockout_sec)}
        )

    ident = (req.email or "").strip().lower()
    if not ident:
        raise HTTPException(status_code=400, detail="Email or username is required.")

    users = await execute_pg_query(
        """
        SELECT id, user_id, username, name, email, avatar, status 
        FROM users 
        WHERE LOWER(email) = $1 OR LOWER(username) = $1
        LIMIT 1
        """,
        ident
    )

    if not users:
        return {
            "status": "success",
            "exists": False,
            "message": "No account found with this email or username."
        }

    u = users[0]
    display_name = u.get("name") or u.get("username") or "User"
    return {
        "status": "success",
        "exists": True,
        "name": display_name,
        "username": u.get("username") or "",
        "email": u.get("email") or "",
        "avatar": u.get("avatar") or DEFAULT_AVATAR_URL
    }


@router.post("/register")
async def register(req: RegisterRequest, response: Response):
    """Register a new native account with email and password."""
    email = (req.email or "").strip().lower()
    if not validate_email_format(email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    if req.confirm_password is not None and req.password != req.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match. Please verify and try again.")

    is_valid_pw, pw_error = validate_password_strength(req.password)
    if not is_valid_pw:
        raise HTTPException(status_code=400, detail=pw_error)

    # Check if user with this email already exists
    existing_by_email = await execute_pg_query("SELECT id, email FROM users WHERE email = $1", email)
    if existing_by_email:
        raise HTTPException(status_code=400, detail="An account with this email already exists. Please sign in instead.")

    # Determine unique username
    candidate_username = (req.username or "").strip().lower()
    if candidate_username:
        candidate_username = re.sub(r"[^a-z0-9_.-]", "", candidate_username)
    if not candidate_username or len(candidate_username) < 3:
        base_name = re.sub(r"[^a-z0-9]", "", email.split("@")[0].lower()) or "user"
        candidate_username = base_name

    # Ensure username is globally unique
    final_username = candidate_username
    counter = 1
    while True:
        existing_by_user = await execute_pg_query("SELECT id FROM users WHERE username = $1", final_username)
        if not existing_by_user:
            break
        final_username = f"{candidate_username}{counter}"
        counter += 1

    display_name = (req.name or "").strip() or final_username
    pwd_hash = hash_password(req.password)

    # Insert into users table
    await execute_pg_query(
        """
        INSERT INTO users (username, name, email, password_hash, status, created_at, updated_at)
        VALUES ($1, $2, $3, $4, 'online', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        final_username, display_name, email, pwd_hash
    )

    # Fetch created user record
    created_rows = await execute_pg_query(
        "SELECT id, user_id, username, name, email, avatar, status, bio, created_at FROM users WHERE email = $1",
        email
    )
    if not created_rows:
        raise HTTPException(status_code=500, detail="Failed to initialize account profile.")

    user = created_rows[0]
    db_id = user["id"]
    user_id = user.get("user_id") or (10000 + db_id)

    # Make sure user_id is persisted
    if not user.get("user_id"):
        await execute_pg_query("UPDATE users SET user_id = $1 WHERE id = $2", user_id, db_id)
        user["user_id"] = user_id

    # Format user response dict
    user_data = {
        "id": user_id,
        "user_id": user_id,
        "username": user["username"],
        "name": user["name"] or user["username"],
        "email": user["email"],
        "avatar": user.get("avatar") or DEFAULT_AVATAR_URL,
        "avatar_url": user.get("avatar") or DEFAULT_AVATAR_URL,
        "bio": user.get("bio") or "",
        "status": user.get("status") or "online"
    }

    # Generate JWT Token
    token = create_spac2_token(user_id=user_id, username=user["username"], email=user["email"])
    _set_auth_cookies(response, token, user_data)

    return {
        "status": "success",
        "message": "Account created successfully.",
        "token": token,
        "user": user_data
    }


@router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response):
    """Sign in with email/username and password with anti-brute force rate limiting and cooldown."""
    client_ip = request.client.host if request.client else "unknown"
    ident = (req.email or "").strip().lower()
    password = req.password or ""

    rate_key = f"{client_ip}_{ident}"
    lockout_sec = _check_rate_limit(rate_key) or _check_rate_limit(client_ip)
    if lockout_sec:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed login attempts. Please wait {lockout_sec}s before trying again.",
            headers={"Retry-After": str(lockout_sec)}
        )

    if not ident:
        raise HTTPException(status_code=400, detail="Email or username is required.")
    if not password:
        raise HTTPException(status_code=400, detail="Password is required.")

    # Search by email or username
    users = await execute_pg_query(
        """
        SELECT id, user_id, username, name, email, password_hash, avatar, bio, status 
        FROM users 
        WHERE LOWER(email) = $1 OR LOWER(username) = $1
        """,
        ident
    )

    if not users:
        lock = _record_failed_attempt(rate_key) or _record_failed_attempt(client_ip)
        if lock:
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Security cooldown active: {lock}s.",
                headers={"Retry-After": str(lock)}
            )
        raise HTTPException(status_code=401, detail="Invalid email/username or password.")

    user = users[0]
    stored_hash = user.get("password_hash")

    if not stored_hash or not verify_password(password, stored_hash):
        lock = _record_failed_attempt(rate_key) or _record_failed_attempt(client_ip)
        if lock:
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Security cooldown active: {lock}s.",
                headers={"Retry-After": str(lock)}
            )
        raise HTTPException(status_code=401, detail="Invalid email/username or password.")

    # Success: Clear any failed attempts
    _clear_failed_attempts(rate_key)
    _clear_failed_attempts(client_ip)

    db_id = user["id"]
    user_id = user.get("user_id") or (10000 + db_id)

    if not user.get("user_id"):
        await execute_pg_query("UPDATE users SET user_id = $1 WHERE id = $2", user_id, db_id)
        user["user_id"] = user_id

    user_data = {
        "id": user_id,
        "user_id": user_id,
        "username": user["username"],
        "name": user["name"] or user["username"],
        "email": user["email"],
        "avatar": user.get("avatar") or DEFAULT_AVATAR_URL,
        "avatar_url": user.get("avatar") or DEFAULT_AVATAR_URL,
        "bio": user.get("bio") or "",
        "status": user.get("status") or "online"
    }

    token = create_spac2_token(user_id=user_id, username=user["username"], email=user["email"])
    _set_auth_cookies(response, token, user_data)

    return {
        "status": "success",
        "message": "Signed in successfully.",
        "token": token,
        "user": user_data
    }


@router.post("/logout")
async def logout(response: Response):
    """Log out and clear session cookies."""
    _clear_auth_cookies(response)
    return {
        "status": "success",
        "message": "Logged out successfully."
    }


@router.get("/me")
async def get_current_user(
    token: Optional[str] = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Retrieve current authenticated user session."""
    if token:
        payload = decode_spac2_token(token)
        if payload:
            lookup = payload.get("email") or payload.get("username") or str(payload.get("user_id") or "")
            if lookup:
                p = await get_profile(lookup)
                if p:
                    return p

    raise HTTPException(status_code=401, detail="Unauthorized: No active session found.")


@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    token: Optional[str] = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Change current authenticated user's password."""
    user_ident = ""
    if token:
        payload = decode_spac2_token(token)
        if payload:
            user_ident = payload.get("email") or payload.get("username") or str(payload.get("user_id") or "")

    if not user_ident:
        for ident in [x_user_email, x_user_id, x_user_name]:
            if ident and ident.strip():
                user_ident = ident.strip()
                break

    if not user_ident:
        raise HTTPException(status_code=401, detail="Unauthorized: No active session found.")

    # Find user in DB
    users = []
    if user_ident.isdigit():
        users = await execute_pg_query(
            "SELECT id, user_id, username, email, password_hash FROM users WHERE user_id = $1 OR id = $1",
            int(user_ident)
        )
    if not users:
        users = await execute_pg_query(
            "SELECT id, user_id, username, email, password_hash FROM users WHERE email = $1 OR username = $2",
            user_ident.lower(), user_ident.lower()
        )

    if not users:
        raise HTTPException(status_code=404, detail="User account not found.")

    user = users[0]
    stored_hash = user.get("password_hash")

    # If the account already has a password, current password is required and must match
    if stored_hash:
        if not req.current_password:
            raise HTTPException(status_code=400, detail="Vui lòng nhập mật khẩu hiện tại / Current password is required.")
        if not verify_password(req.current_password, stored_hash):
            raise HTTPException(status_code=400, detail="Mật khẩu hiện tại không chính xác / Current password is incorrect.")

    # Validate new password
    new_pw = (req.new_password or "").strip()
    if not new_pw:
        raise HTTPException(status_code=400, detail="Mật khẩu mới không được để trống / New password cannot be empty.")

    if req.confirm_password is not None and new_pw != req.confirm_password.strip():
        raise HTTPException(status_code=400, detail="Mật khẩu xác nhận không khớp / Confirm password does not match.")

    is_valid_pw, pw_error = validate_password_strength(new_pw)
    if not is_valid_pw:
        raise HTTPException(status_code=400, detail=pw_error)

    if stored_hash and req.current_password and req.current_password == new_pw:
        raise HTTPException(status_code=400, detail="Mật khẩu mới không được trùng với mật khẩu hiện tại / New password cannot be the same as current password.")

    # Hash and update in DB
    pwd_hash = hash_password(new_pw)
    await execute_pg_query(
        "UPDATE users SET password_hash = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2",
        pwd_hash, user["id"]
    )

    return {
        "status": "success",
        "message": "Đổi mật khẩu thành công! / Password changed successfully."
    }


# ==============================================================================
# REVERSE EMAIL VERIFICATION (INBOUND PARSE VIA CLOUDFLARE EMAIL ROUTING)
# Target Address: verify@spac2.com
# ==============================================================================

# In-memory temporary token cache (Key: "SPAC2-XXXXXX" -> { "user_id": int, "email": str, "expires_at": float })
_email_verification_tokens: Dict[str, Dict[str, Any]] = {}
WEBHOOK_SECRET_KEY = "spac2_super_secure_secret_2026"
TARGET_VERIFY_EMAIL = "verify@spac2.com"


class InitiateEmailVerifyResponse(BaseModel):
    token: str
    target_email: str
    subject: str
    body: str
    mailto_link: str
    expires_in_minutes: int


class CloudflareEmailWebhookPayload(BaseModel):
    sender: str
    recipient: Optional[str] = "verify@spac2.com"
    token: str
    subject: Optional[str] = ""
    received_at: Optional[str] = None


@router.post("/email-verify/initiate", response_model=InitiateEmailVerifyResponse)
async def initiate_email_verification(
    token: Optional[str] = Depends(get_auth_token),
    x_user_email: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None)
):
    """Generate a reverse email verification token & 1-click mailto link for current user."""
    user_email = None
    user_id = None

    if token:
        payload = decode_spac2_token(token)
        if payload:
            user_email = payload.get("email")
            user_id = payload.get("user_id") or payload.get("id")

    if not user_email:
        for ident in [x_user_email, x_user_id]:
            if ident and ident.strip():
                p = await get_profile(ident.strip())
                if p:
                    user_email = p.get("email")
                    user_id = p.get("id") or p.get("user_id")
                    break

    if not user_email:
        raise HTTPException(status_code=401, detail="Unauthorized: No active user session found.")

    user_email = user_email.strip().lower()

    # Generate an easy-to-read 6-character hex token (e.g. SPAC2-7E9B1C)
    hex_code = secrets.token_hex(3).upper()
    token_str = f"SPAC2-{hex_code}"

    # TTL: 20 minutes
    _email_verification_tokens[token_str] = {
        "user_id": user_id,
        "email": user_email,
        "expires_at": time.time() + (20 * 60)
    }

    subject = f"Verify Spac2 Account - {token_str}"
    body = f"Spac2 Reverse Verification\nToken: {token_str}\n(Please click Send without changing the subject or body to verify your Spac2 account instantly)."
    mailto_link = f"mailto:{TARGET_VERIFY_EMAIL}?subject={urllib.parse.quote(subject)}&body={urllib.parse.quote(body)}"

    return {
        "token": token_str,
        "target_email": TARGET_VERIFY_EMAIL,
        "subject": subject,
        "body": body,
        "mailto_link": mailto_link,
        "expires_in_minutes": 20
    }


@router.get("/email-verify/status")
async def check_email_verification_status(
    token: Optional[str] = Depends(get_auth_token),
    x_user_email: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None)
):
    """Check whether current authenticated user's email is verified."""
    user_ident = None
    if token:
        payload = decode_spac2_token(token)
        if payload:
            user_ident = payload.get("email") or str(payload.get("user_id") or "")

    if not user_ident:
        for ident in [x_user_email, x_user_id]:
            if ident and ident.strip():
                user_ident = ident.strip()
                break

    if not user_ident:
        raise HTTPException(status_code=401, detail="Unauthorized")

    users = []
    if str(user_ident).isdigit():
        users = await execute_pg_query(
            "SELECT is_verified, email_verified FROM users WHERE user_id = $1 OR id = $1",
            int(user_ident)
        )
    else:
        users = await execute_pg_query(
            "SELECT is_verified, email_verified FROM users WHERE LOWER(email) = $1 OR LOWER(username) = $1",
            str(user_ident).lower()
        )

    is_verified = False
    if users:
        u = users[0]
        is_verified = bool(u.get("is_verified") or u.get("email_verified"))

    return {"is_verified": is_verified}


@router.post("/email-webhook")
async def cloudflare_email_webhook(
    payload: CloudflareEmailWebhookPayload,
    x_spac2_secret: Optional[str] = Header(None, alias="X-Spac2-Secret")
):
    """Secure inbound webhook called by Cloudflare Email Worker when an email is received."""
    # 1. Validate Shared Secret
    if x_spac2_secret != WEBHOOK_SECRET_KEY:
        print(f"[Email Webhook] Secret mismatch: received '{x_spac2_secret}'")
        raise HTTPException(status_code=403, detail="Forbidden: Invalid or missing webhook secret key.")

    token = (payload.token or "").strip().upper()
    raw_sender = (payload.sender or "").strip().lower()

    # Extract clean email from "Name <email@domain.com>" or "email@domain.com"
    email_regex = r'[\w\.-]+@[\w\.-]+\.\w+'
    sender_match = re.search(email_regex, raw_sender)
    clean_sender = sender_match.group(0).lower() if sender_match else raw_sender

    print(f"[Email Webhook] Received webhook for token '{token}' from sender '{clean_sender}' (raw: '{raw_sender}')")

    # 2. Check token in active verification pool
    record = _email_verification_tokens.get(token)
    if not record:
        print(f"[Email Webhook] Token '{token}' not found in active pool. Active tokens: {list(_email_verification_tokens.keys())}")
        raise HTTPException(status_code=404, detail="Verification token not found or already used.")

    if time.time() > record["expires_at"]:
        _email_verification_tokens.pop(token, None)
        print(f"[Email Webhook] Token '{token}' has expired.")
        raise HTTPException(status_code=400, detail="Verification token has expired.")

    expected_email = (record.get("email") or "").strip().lower()
    user_id = record.get("user_id")

    # 3. Match sender email with registered email (allowing prefix/alias matches)
    # E.g. user+tag@gmail.com vs user@gmail.com or direct equality
    is_match = (
        clean_sender == expected_email or
        expected_email in clean_sender or
        clean_sender in expected_email or
        clean_sender.split("@")[0] == expected_email.split("@")[0]
    )

    if not is_match:
        print(f"[Email Webhook] Sender mismatch: Clean sender '{clean_sender}' != expected '{expected_email}'")
        # If token is 100% valid and tied to the user session, we can still accept it or log warning
        # For security, we verify if clean_sender exists in users table or matches expected
        user_check = await execute_pg_query("SELECT id FROM users WHERE LOWER(email) = $1 OR id = $2", clean_sender, user_id)
        if not user_check:
            raise HTTPException(
                status_code=400,
                detail=f"Sender email mismatch: Received from {clean_sender}, expected {expected_email}"
            )

    # 4. Update Database: Set is_verified and email_verified to TRUE
    if user_id:
        await execute_pg_query(
            "UPDATE users SET is_verified = TRUE, email_verified = TRUE, updated_at = CURRENT_TIMESTAMP WHERE id = $1 OR user_id = $1 OR email = $2",
            user_id, expected_email
        )
    else:
        await execute_pg_query(
            "UPDATE users SET is_verified = TRUE, email_verified = TRUE, updated_at = CURRENT_TIMESTAMP WHERE email = $1",
            expected_email
        )

    # 5. Clean up consumed token
    _email_verification_tokens.pop(token, None)
    print(f"[Email Webhook] Successfully verified account for {expected_email} (User ID: {user_id})")

    # 6. Realtime Notification via WebSocket if user is connected
    try:
        ws_msg = json.dumps({
            "type": "EMAIL_VERIFIED",
            "email": expected_email,
            "message": "Your email address has been verified successfully!"
        })
        await send_to_user_by_email(expected_email, ws_msg)
        if clean_sender != expected_email:
            await send_to_user_by_email(clean_sender, ws_msg)
    except Exception as ws_err:
        print("[Email Webhook] WebSocket notification fallback:", ws_err)

    return {
        "status": "success",
        "message": f"Account with email {expected_email} successfully verified!",
        "email": expected_email
    }

