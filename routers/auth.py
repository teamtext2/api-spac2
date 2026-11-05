from __future__ import annotations
import json
import re
import time
import secrets
import urllib.parse
import hmac
import hashlib
import base64
from typing import Optional, Dict, Any, List
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Depends, Request, Response, Header, Query

from database.postgres import execute_pg_query
from auth.deps import get_auth_token, create_spac2_token, decode_spac2_token, create_text2_token, decode_text2_token
from auth.security import hash_password, verify_password, validate_email_format, validate_password_strength
from services.user_service import get_profile
from services.email_service import send_verification_otp_email, send_password_reset_otp_email
from config import DEFAULT_AVATAR_URL, WORKER_SECRET_KEY
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
# OUTBOUND EMAIL OTP SYSTEM (POWERED BY RESEND API)
# ==============================================================================

# In-memory OTP storage for 6-digit numeric codes (TTL 5 mins = 300s)
# Key: email (lowercased) -> { "code": str, "expires_at": float, "attempts": int, "user_id": Optional[int], "sent_at": float }
_email_verification_otp: Dict[str, Dict[str, Any]] = {}
_password_reset_otp: Dict[str, Dict[str, Any]] = {}


class SendEmailVerifyOtpRequest(BaseModel):
    email: Optional[str] = None


class ConfirmEmailVerifyOtpRequest(BaseModel):
    otp: str
    email: Optional[str] = None


class SendPasswordResetOtpRequest(BaseModel):
    email: str


class ResetPasswordWithOtpRequest(BaseModel):
    email: str
    otp: str
    new_password: str
    confirm_password: Optional[str] = None


class ResetPasswordWithTicketRequest(BaseModel):
    email: str
    ticket: str
    new_password: str
    confirm_password: Optional[str] = None


class ConfirmEmailVerifyTicketRequest(BaseModel):
    email: str
    ticket: str


def _verify_worker_ticket(ticket: str, expected_email: str, expected_type: str) -> bool:
    """Validate HMAC-SHA256 signed ticket issued by Cloudflare Edge Worker."""
    if not ticket or "." not in ticket:
        return False
    try:
        parts = ticket.strip().split(".")
        if len(parts) != 2:
            return False
        b64_payload, signature = parts

        # Verify signature
        key = WORKER_SECRET_KEY.encode("utf-8")
        expected_sig_bytes = hmac.new(key, b64_payload.encode("utf-8"), hashlib.sha256).digest()
        expected_sig = base64.urlsafe_b64encode(expected_sig_bytes).decode("utf-8").rstrip("=")

        if not hmac.compare_digest(signature, expected_sig):
            return False

        # Decode JSON payload
        padding = "=" * ((4 - len(b64_payload) % 4) % 4)
        payload_json = base64.urlsafe_b64decode((b64_payload + padding).encode("utf-8")).decode("utf-8")
        payload = json.loads(payload_json)

        if payload.get("email", "").strip().lower() != expected_email.strip().lower():
            return False

        if payload.get("type", "").strip().lower() != expected_type.strip().lower():
            return False

        exp = payload.get("exp", 0) / 1000.0
        if time.time() > exp:
            return False

        return True
    except Exception as ex:
        print("[Ticket Verify Error]:", ex)
        return False


@router.post("/forgot-password/reset-with-ticket")
async def reset_password_with_ticket(req: ResetPasswordWithTicketRequest):
    """Verify cryptographically signed ticket from Cloudflare Edge Worker and update user password in PostgreSQL."""
    clean_email = (req.email or "").strip().lower()
    if not clean_email or not validate_email_format(clean_email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    new_pw = (req.new_password or "").strip()
    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters.")

    if req.confirm_password and req.confirm_password.strip() != new_pw:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    # Verify ticket
    is_valid = _verify_worker_ticket(req.ticket, clean_email, "reset")
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail="Verification ticket has expired or is invalid. Please request a new verification code."
        )

    # Check if user exists
    users = await execute_pg_query("SELECT id, user_id FROM users WHERE LOWER(email) = $1", clean_email)
    if not users:
        raise HTTPException(status_code=404, detail="No Spac2 account found with this email.")

    hashed_pw = hash_password(new_pw)
    user_id = users[0].get("id") or users[0].get("user_id")

    if user_id:
        await execute_pg_query(
            "UPDATE users SET password_hash = $1, is_verified = TRUE, email_verified = TRUE, updated_at = CURRENT_TIMESTAMP WHERE id = $2 OR user_id = $2 OR LOWER(email) = $3",
            hashed_pw, user_id, clean_email
        )
    else:
        await execute_pg_query(
            "UPDATE users SET password_hash = $1, is_verified = TRUE, email_verified = TRUE, updated_at = CURRENT_TIMESTAMP WHERE LOWER(email) = $2",
            hashed_pw, clean_email
        )

    print(f"[Password Reset Ticket] Successfully updated password for {clean_email}")
    return {
        "success": True,
        "message": "Password reset successfully! You can now sign in with your new password."
    }


@router.post("/email-verify/confirm-ticket")
async def confirm_email_verify_ticket(
    req: ConfirmEmailVerifyTicketRequest,
    token: Optional[str] = Depends(get_auth_token)
):
    """Verify cryptographically signed ticket from Cloudflare Edge Worker and mark account verified in PostgreSQL."""
    clean_email = (req.email or "").strip().lower()
    if not clean_email:
        raise HTTPException(status_code=400, detail="Email is required.")

    is_valid = _verify_worker_ticket(req.ticket, clean_email, "verify")
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail="Verification ticket has expired or is invalid. Please request a new verification code."
        )

    await execute_pg_query(
        "UPDATE users SET is_verified = TRUE, email_verified = TRUE, updated_at = CURRENT_TIMESTAMP WHERE LOWER(email) = $1",
        clean_email
    )

    try:
        ws_msg = json.dumps({
            "type": "EMAIL_VERIFIED",
            "email": clean_email,
            "message": "Your email address has been verified successfully!"
        })
        await send_to_user_by_email(clean_email, ws_msg)
    except Exception as ws_err:
        print("[Ticket Verify] WS notify error:", ws_err)

    return {
        "success": True,
        "message": "Your email has been verified successfully!",
        "email": clean_email
    }


@router.post("/email-verify/send-otp")
async def send_email_verify_otp(
    req: SendEmailVerifyOtpRequest,
    token: Optional[str] = Depends(get_auth_token),
    x_user_email: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None)
):
    """Generate and dispatch a 6-digit OTP to the user's email via Resend."""
    user_email = (req.email or "").strip().lower()
    user_id = None
    username = None

    if token:
        payload = decode_spac2_token(token)
        if payload:
            if not user_email:
                user_email = (payload.get("email") or "").strip().lower()
            user_id = payload.get("user_id") or payload.get("id")
            username = payload.get("username") or payload.get("name")

    if not user_email:
        for ident in [x_user_email, x_user_id]:
            if ident and ident.strip():
                p = await get_profile(ident.strip())
                if p:
                    user_email = (p.get("email") or "").strip().lower()
                    user_id = p.get("id") or p.get("user_id")
                    username = p.get("username") or p.get("name")
                    break

    if not user_email or not validate_email_format(user_email):
        raise HTTPException(status_code=400, detail="A valid email address is required to send verification code.")

    # Check if user exists in database
    users = await execute_pg_query(
        "SELECT id, user_id, email, username, is_verified, email_verified FROM users WHERE LOWER(email) = $1",
        user_email
    )
    if users:
        u = users[0]
        user_id = u.get("id") or u.get("user_id")
        username = username or u.get("username")
        if bool(u.get("is_verified") or u.get("email_verified")):
            return {
                "success": True,
                "already_verified": True,
                "message": "Your email address is already verified."
            }

    # Anti-spam rate limiting: 45s between requests
    now = time.time()
    existing = _email_verification_otp.get(user_email)
    if existing and now < existing.get("expires_at", 0):
        time_since_last = now - existing.get("sent_at", 0)
        if time_since_last < 45:
            wait_sec = int(45 - time_since_last) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Please wait {wait_sec} seconds before requesting another code."
            )

    # Generate 6-digit numeric OTP code
    otp_code = f"{secrets.randbelow(900000) + 100000}"

    _email_verification_otp[user_email] = {
        "code": otp_code,
        "expires_at": now + 300,  # 5 minutes
        "attempts": 0,
        "user_id": user_id,
        "sent_at": now
    }

    # Dispatch email via Resend
    delivery = await send_verification_otp_email(to_email=user_email, otp_code=otp_code, user_name=username)
    if not delivery.get("success"):
        print(f"[Email Verify OTP] Delivery failure to {user_email}: {delivery.get('error')}")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to send email via Resend: {delivery.get('error') or 'Service unavailable'}"
        )

    return {
        "success": True,
        "message": f"Verification code sent successfully to {user_email}",
        "email": user_email,
        "expires_in_seconds": 300
    }


@router.post("/email-verify/confirm-otp")
async def confirm_email_verify_otp(
    req: ConfirmEmailVerifyOtpRequest,
    token: Optional[str] = Depends(get_auth_token),
    x_user_email: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None)
):
    """Verify the 6-digit OTP and mark user account as verified in PostgreSQL."""
    user_email = (req.email or "").strip().lower()

    if not user_email and token:
        payload = decode_spac2_token(token)
        if payload:
            user_email = (payload.get("email") or "").strip().lower()

    if not user_email:
        for ident in [x_user_email, x_user_id]:
            if ident and ident.strip():
                p = await get_profile(ident.strip())
                if p:
                    user_email = (p.get("email") or "").strip().lower()
                    break

    if not user_email:
        raise HTTPException(status_code=400, detail="Email address is required.")

    code_input = (req.otp or "").strip()
    if not code_input or len(code_input) < 4:
        raise HTTPException(status_code=400, detail="Please enter a valid 6-digit verification code.")

    record = _email_verification_otp.get(user_email)
    if not record or time.time() > record.get("expires_at", 0):
        _email_verification_otp.pop(user_email, None)
        raise HTTPException(status_code=400, detail="Verification code has expired or was not requested. Please request a new code.")

    record["attempts"] += 1
    if record["attempts"] > 5:
        _email_verification_otp.pop(user_email, None)
        raise HTTPException(status_code=400, detail="Too many incorrect attempts. Please request a new code.")

    if record["code"] != code_input:
        remaining = max(0, 5 - record["attempts"])
        raise HTTPException(
            status_code=400,
            detail=f"Incorrect verification code. {remaining} attempt(s) remaining."
        )

    # Valid OTP! Mark verified in database
    user_id = record.get("user_id")
    if user_id:
        await execute_pg_query(
            "UPDATE users SET is_verified = TRUE, email_verified = TRUE, updated_at = CURRENT_TIMESTAMP WHERE id = $1 OR user_id = $1 OR LOWER(email) = $2",
            user_id, user_email
        )
    else:
        await execute_pg_query(
            "UPDATE users SET is_verified = TRUE, email_verified = TRUE, updated_at = CURRENT_TIMESTAMP WHERE LOWER(email) = $1",
            user_email
        )

    _email_verification_otp.pop(user_email, None)

    # Realtime WebSocket notification
    try:
        ws_msg = json.dumps({
            "type": "EMAIL_VERIFIED",
            "email": user_email,
            "message": "Your email address has been verified successfully!"
        })
        await send_to_user_by_email(user_email, ws_msg)
    except Exception as ws_err:
        print("[Email Verify OTP] WS notify fallback:", ws_err)

    return {
        "success": True,
        "message": "Your email has been verified successfully!",
        "email": user_email
    }


@router.post("/forgot-password/send-otp")
async def send_forgot_password_otp(req: SendPasswordResetOtpRequest):
    """Generate and dispatch a 6-digit password reset OTP via Resend."""
    clean_email = (req.email or "").strip().lower()
    if not clean_email or not validate_email_format(clean_email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    users = await execute_pg_query(
        "SELECT id, user_id, email, username FROM users WHERE LOWER(email) = $1",
        clean_email
    )
    if not users:
        raise HTTPException(status_code=404, detail="No Spac2 account found with this email address.")

    user = users[0]
    user_id = user.get("id") or user.get("user_id")
    username = user.get("username")

    # Anti-spam rate limiting: 45s between requests
    now = time.time()
    existing = _password_reset_otp.get(clean_email)
    if existing and now < existing.get("expires_at", 0):
        time_since_last = now - existing.get("sent_at", 0)
        if time_since_last < 45:
            wait_sec = int(45 - time_since_last) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Please wait {wait_sec} seconds before requesting another code."
            )

    otp_code = f"{secrets.randbelow(900000) + 100000}"

    _password_reset_otp[clean_email] = {
        "code": otp_code,
        "expires_at": now + 300,  # 5 minutes
        "attempts": 0,
        "user_id": user_id,
        "sent_at": now
    }

    delivery = await send_password_reset_otp_email(to_email=clean_email, otp_code=otp_code, user_name=username)
    if not delivery.get("success"):
        print(f"[Password Reset OTP] Delivery failure to {clean_email}: {delivery.get('error')}")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to send email via Resend: {delivery.get('error') or 'Service unavailable'}"
        )

    return {
        "success": True,
        "message": f"Password reset code sent to {clean_email}",
        "expires_in_seconds": 300
    }


@router.post("/forgot-password/reset-with-otp")
async def reset_password_with_otp(req: ResetPasswordWithOtpRequest):
    """Verify 6-digit OTP and update user's password in PostgreSQL."""
    clean_email = (req.email or "").strip().lower()
    if not clean_email or not validate_email_format(clean_email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    new_pw = (req.new_password or "").strip()
    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters.")

    if req.confirm_password and req.confirm_password.strip() != new_pw:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    code_input = (req.otp or "").strip()
    if not code_input:
        raise HTTPException(status_code=400, detail="Please enter the 6-digit verification code.")

    record = _password_reset_otp.get(clean_email)
    if not record or time.time() > record.get("expires_at", 0):
        _password_reset_otp.pop(clean_email, None)
        raise HTTPException(status_code=400, detail="Password reset code has expired or was not requested. Please request a new code.")

    record["attempts"] += 1
    if record["attempts"] > 5:
        _password_reset_otp.pop(clean_email, None)
        raise HTTPException(status_code=400, detail="Too many incorrect attempts. Please request a new code.")

    if record["code"] != code_input:
        remaining = max(0, 5 - record["attempts"])
        raise HTTPException(
            status_code=400,
            detail=f"Incorrect verification code. {remaining} attempt(s) remaining."
        )

    # Valid! Hash new password & update DB
    hashed_pw = hash_password(new_pw)
    user_id = record.get("user_id")

    if user_id:
        await execute_pg_query(
            "UPDATE users SET password_hash = $1, is_verified = TRUE, email_verified = TRUE, updated_at = CURRENT_TIMESTAMP WHERE id = $2 OR user_id = $2 OR LOWER(email) = $3",
            hashed_pw, user_id, clean_email
        )
    else:
        await execute_pg_query(
            "UPDATE users SET password_hash = $1, is_verified = TRUE, email_verified = TRUE, updated_at = CURRENT_TIMESTAMP WHERE LOWER(email) = $2",
            hashed_pw, clean_email
        )

    _password_reset_otp.pop(clean_email, None)
    print(f"[Password Reset OTP] Successfully updated password for {clean_email}")

    return {
        "success": True,
        "message": "Password reset successfully! You can now sign in with your new password."
    }


