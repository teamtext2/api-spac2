from __future__ import annotations
import json
import re
import time
import urllib.parse
from typing import Optional, Dict, Any
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Depends, Request, Response, Header

from database.postgres import execute_pg_query
from auth.deps import get_auth_token, create_spac2_token, decode_spac2_token, create_text2_token, decode_text2_token
from auth.security import hash_password, verify_password, validate_email_format, validate_password_strength
from services.user_service import get_profile

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str
    password: str
    confirm_password: Optional[str] = None
    name: Optional[str] = None
    username: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: Optional[str] = None
    new_password: str
    confirm_password: Optional[str] = None


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
        "avatar": user.get("avatar") or "",
        "avatar_url": user.get("avatar") or "",
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
async def login(req: LoginRequest, response: Response):
    """Sign in with email/username and password."""
    ident = (req.email or "").strip().lower()
    password = req.password or ""

    if not ident:
        raise HTTPException(status_code=400, detail="Email or username is required.")
    if not password:
        raise HTTPException(status_code=400, detail="Password is required.")

    # Search by email or username
    users = await execute_pg_query(
        """
        SELECT id, user_id, username, name, email, password_hash, avatar, bio, status 
        FROM users 
        WHERE email = $1 OR username = $2
        """,
        ident, ident
    )

    if not users:
        raise HTTPException(status_code=401, detail="Invalid email/username or password.")

    user = users[0]
    stored_hash = user.get("password_hash")

    if not stored_hash or not verify_password(password, stored_hash):
        raise HTTPException(status_code=401, detail="Invalid email/username or password.")

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
        "avatar": user.get("avatar") or "",
        "avatar_url": user.get("avatar") or "",
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

