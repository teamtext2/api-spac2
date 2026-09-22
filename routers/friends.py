import json
from fastapi import APIRouter, HTTPException, Depends
from database.postgres import execute_pg_query
from auth.deps import get_auth_token, verify_google_token
from models.schemas import FriendRequest
from websocket.manager import active_connections, get_email_by_username
from config import DEFAULT_AVATAR_URL

router = APIRouter(prefix="/api", tags=["friends"])


@router.post("/add_friend")
async def api_add_friend(req: FriendRequest, token: str = Depends(get_auth_token)):
    username = req.username.strip().lower()
    friend_username = req.friend_username.strip().lower()

    if username == friend_username:
        raise HTTPException(status_code=400, detail="You cannot add yourself as a friend")

    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_res = await execute_pg_query("SELECT id, name, avatar FROM users WHERE username = $1", username)
    friend_res = await execute_pg_query("SELECT id, name, avatar FROM users WHERE username = $1", friend_username)
    if not user_res or not friend_res:
        raise HTTPException(status_code=404, detail="User not found")

    uid = user_res[0]["id"]
    sender_name = user_res[0].get("name") or username
    sender_avatar = user_res[0].get("avatar") or DEFAULT_AVATAR_URL
    fid = friend_res[0]["id"]

    id1, id2 = min(uid, fid), max(uid, fid)

    try:
        await execute_pg_query(
            """
            INSERT INTO chat_friends (user_id, friend_id) 
            VALUES ($1, $2)
            ON CONFLICT (user_id, friend_id) DO NOTHING
            """,
            id1, id2
        )

        if friend_username in active_connections:
            for ws in list(active_connections[friend_username]):
                try:
                    await ws.send_text(json.dumps({
                        "type": "friend_request",
                        "sender": username,
                        "senderName": sender_name,
                        "senderAvatar": sender_avatar,
                        "senderEmail": email,
                        "text": f"@{username} added you as a friend!"
                    }))
                    print(f"Real-time friend notification sent to a connection of @{friend_username}")
                except Exception as e:
                    print(f"Failed to send real-time friend notification to a connection of @{friend_username}: {e}")

        return {"status": "success", "message": f"You are now friends with @{friend_username}."}
    except Exception as e:
        print(f"Error adding friend: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/friends/{username}")
async def api_get_friends(username: str, token: str = Depends(get_auth_token)):
    username = username.strip().lower()

    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        user_res = await execute_pg_query("SELECT id FROM users WHERE username = $1", username)
        if not user_res:
            return []
        user_id = user_res[0]["id"]

        friend_rows = await execute_pg_query(
            "SELECT user_id, friend_id FROM chat_friends WHERE user_id = $1 OR friend_id = $1",
            user_id
        )

        friend_ids = []
        for r in friend_rows:
            fid = r["friend_id"] if r["user_id"] == user_id else r["user_id"]
            friend_ids.append(fid)

        if not friend_ids:
            return []

        friends = await execute_pg_query(
            "SELECT username, name, bio, email, status, avatar, last_seen FROM users WHERE id = ANY($1::integer[])",
            friend_ids
        )
        for friend in friends:
            friend_username = friend.get("username", "").strip().lower()
            friend["status"] = "online" if friend_username in active_connections else "offline"
            fav = friend.get("avatar") or DEFAULT_AVATAR_URL
            friend["avatar"] = fav
            friend["avatar_url"] = fav
        return friends
    except Exception as e:
        print(f"Error getting friends list: {e}")
        return []
