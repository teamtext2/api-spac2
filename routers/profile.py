from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Request, Response, Header
from database.postgres import execute_pg_query
from auth.deps import get_auth_token, verify_google_token, create_spac2_token, decode_spac2_token, create_text2_token, decode_text2_token
from models.schemas import UserProfile
from services.user_service import save_profile, get_profile, get_email_by_username
from websocket.manager import active_connections

router = APIRouter(prefix="/api", tags=["profile"])


@router.get("/profile")
async def api_get_my_profile(
    request: Request,
    token: Optional[str] = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Retrieve current authenticated user profile across Spac2 Ecosystem."""
    # 1. Spac2 JWT Token lookup
    if token:
        payload = decode_spac2_token(token)
        if payload:
            lookup = payload.get("email") or payload.get("username") or str(payload.get("user_id") or "")
            if lookup:
                p = await get_profile(lookup)
                if p:
                    return p

    # 2. Header-based identity lookup
    for ident in [x_user_email, x_user_id, x_user_name]:
        if ident and ident.strip():
            p = await get_profile(ident.strip())
            if p:
                return p

    # 3. Fallback: Check if token is raw identifier
    if token and len(token) < 200:
        p = await get_profile(token)
        if p:
            return p

    raise HTTPException(status_code=401, detail="Unauthorized: No active profile session found")


@router.post("/profile")
async def api_save_profile(
    profile: UserProfile, 
    request: Request, 
    token: Optional[str] = Depends(get_auth_token),
    x_user_email: Optional[str] = Header(None)
):
    # 1. Resolve email from JWT token or header if missing
    auth_email = ""
    token_username = ""
    if token:
        payload = decode_spac2_token(token)
        if payload:
            auth_email = (payload.get("email") or "").strip().lower()
            token_username = (payload.get("username") or "").strip().lower()

    if not auth_email and x_user_email:
        auth_email = x_user_email.strip().lower()

    if not auth_email and profile.email:
        auth_email = profile.email.strip().lower()

    if not auth_email:
        raise HTTPException(status_code=401, detail="Unauthorized: No active authentication session found")

    if not profile.email:
        profile.email = auth_email

    # Map avatar_url to avatar if needed
    if profile.avatar_url and not profile.avatar:
        profile.avatar = profile.avatar_url
    elif profile.avatar and not profile.avatar_url:
        profile.avatar_url = profile.avatar

    username = profile.username.strip().lower()
    if not username:
        username = token_username or re.sub(r"[^a-z0-9_]", "", profile.email.split("@")[0].lower()) or "user"
        profile.username = username

    if not await verify_google_token(token, profile.email):
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or expired authentication session")

    try:
        base_url = str(request.base_url).rstrip("/")
        updated_profile, is_new = await save_profile(profile.dict(), base_url)
        
        # Generate official Spac2 Unified Ecosystem Token (JWT)
        user_uid = updated_profile.get("user_id") or updated_profile.get("id") or 0
        spac2_token = create_spac2_token(
            user_id=user_uid,
            username=updated_profile.get("username", username),
            email=updated_profile.get("email", profile.email)
        )

        return {
            "status": "success",
            "message": f"Profile for @{username} synced.",
            "token": spac2_token,
            "profile": updated_profile,
            "is_new": is_new
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Error saving profile: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save profile: {str(e)}")


@router.get("/profile/{username}")
async def api_get_profile(username: str):

    username = username.strip().lower()
    profile = await get_profile(username)
    if profile:
        return profile
    raise HTTPException(status_code=404, detail="Profile not found")


@router.get("/search_user")
async def api_search_user(q: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    q = q.strip().lower()
    if not q:
        return []
    try:
        if q.isdigit():
            users = await execute_pg_query(
                "SELECT id, user_id, username, name, bio, email, status, avatar, last_seen FROM users WHERE user_id = $1 OR id = $1 OR username ILIKE $2 OR email ILIKE $2 OR name ILIKE $2 LIMIT 10",
                int(q), f"%{q}%"
            )
        else:
            users = await execute_pg_query(
                "SELECT id, user_id, username, name, bio, email, status, avatar, last_seen FROM users WHERE username ILIKE $1 OR email ILIKE $1 OR name ILIKE $1 LIMIT 10",
                f"%{q}%"
            )
        for u in users:
            uid = u.get("user_id") or (10000 + u["id"])
            u["id"] = uid
            u["user_id"] = uid
            uname = u.get("username", "").strip().lower()
            u["status"] = "online" if uname in active_connections else "offline"
        return users
    except Exception as e:
        print(f"Error searching users: {e}")
        return []
