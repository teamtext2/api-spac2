from fastapi import APIRouter, HTTPException, Depends
from database.postgres import execute_pg_query
from auth.deps import get_auth_token, verify_google_token
from models.schemas import (
    PushSubscriptionPayload,
    PushUnsubscribePayload,
    RegisterDevicePayload,
    LinkDevicePayload,
    UnlinkDevicePayload
)
from websocket.manager import get_email_by_username
from config import VAPID_PUBLIC_KEY

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("/vapid_public_key")
async def get_vapid_public_key():
    return {"vapid_public_key": VAPID_PUBLIC_KEY}


@router.post("/subscribe")
async def save_push_subscription(payload: PushSubscriptionPayload):
    print(f"[PUSH] Received subscription request for user: '{payload.username}'")
    email = await get_email_by_username(payload.username)
    print(f"[PUSH] Looked up email for '{payload.username}': '{email}'")

    is_verified = await verify_google_token(payload.token, email)
    print(f"[PUSH] Token verification result: {is_verified}")

    if not email or not is_verified:
        print(f"[PUSH] Subscription unauthorized. Email empty or token invalid.")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        print(f"[PUSH] Executing PostgreSQL insert for user '{payload.username}' with endpoint: {payload.endpoint[:30]}...")
        await execute_pg_query(
            """
            INSERT INTO chat_push_subscriptions (username, endpoint, p256dh, auth)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (endpoint) DO UPDATE 
            SET username = EXCLUDED.username, p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth
            """,
            payload.username.lower(), payload.endpoint, payload.p256dh, payload.auth
        )
        print(f"[PUSH] Subscription saved successfully to PostgreSQL for '{payload.username}'")
        return {"status": "success", "message": "Push subscription saved successfully"}
    except Exception as e:
        print(f"Error saving push subscription to PostgreSQL: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@router.post("/unsubscribe")
async def remove_push_subscription(payload: PushUnsubscribePayload):
    email = await get_email_by_username(payload.username)
    if not email or not await verify_google_token(payload.token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        await execute_pg_query(
            "DELETE FROM chat_push_subscriptions WHERE username = $1 AND endpoint = $2",
            payload.username.lower(), payload.endpoint
        )
        return {"status": "success", "message": "Push subscription removed"}
    except Exception as e:
        print(f"Error removing push subscription from PostgreSQL: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@router.post("/register_device")
async def register_device(payload: RegisterDevicePayload):
    try:
        # Prevent unique constraint violations for endpoint with a different device_id
        await execute_pg_query(
            "DELETE FROM chat_push_subscriptions WHERE endpoint = $1 AND (device_id IS NULL OR device_id != $2)",
            payload.endpoint, payload.device_id
        )

        await execute_pg_query(
            """
            INSERT INTO chat_push_subscriptions (device_id, endpoint, p256dh, auth)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (device_id) DO UPDATE 
            SET endpoint = EXCLUDED.endpoint, p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth
            """,
            payload.device_id, payload.endpoint, payload.p256dh, payload.auth
        )
        return {"status": "success", "message": "Device registered successfully"}
    except Exception as e:
        print(f"Error registering device to database: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@router.post("/link_device")
async def link_device(payload: LinkDevicePayload):
    email = await get_email_by_username(payload.username)
    if not email or not await verify_google_token(payload.token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        await execute_pg_query(
            "UPDATE chat_push_subscriptions SET username = $1 WHERE device_id = $2",
            payload.username.lower(), payload.device_id
        )
        return {"status": "success", "message": "Device linked to user successfully"}
    except Exception as e:
        print(f"Error linking device to user in database: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@router.post("/unlink_device")
async def unlink_device(payload: UnlinkDevicePayload):
    try:
        await execute_pg_query(
            "UPDATE chat_push_subscriptions SET username = NULL WHERE device_id = $1",
            payload.device_id
        )
        return {"status": "success", "message": "Device unlinked successfully"}
    except Exception as e:
        print(f"Error unlinking device in database: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@router.get("/debug_subscriptions/{username}")
async def debug_subscriptions(username: str):
    """
    DEBUG: Check push subscriptions in DB for a given username.
    Shows endpoint platform, p256dh/auth presence, and row count.
    Remove this endpoint after debugging is complete.
    """
    try:
        rows = await execute_pg_query(
            "SELECT device_id, endpoint, p256dh, auth, username FROM chat_push_subscriptions WHERE username = $1",
            username.lower()
        )
        result = []
        for r in rows:
            ep = r.get("endpoint", "")
            if "push.apple.com" in ep:
                platform = "iOS"
            elif "fcm.googleapis.com" in ep or "firebase.com" in ep:
                platform = "Android/FCM"
            else:
                platform = "Desktop/Firefox"
            result.append({
                "platform": platform,
                "device_id": r.get("device_id"),
                "endpoint_preview": ep[:80] + "..." if len(ep) > 80 else ep,
                "has_p256dh": bool(r.get("p256dh")),
                "has_auth": bool(r.get("auth")),
            })
        return {"username": username, "count": len(result), "subscriptions": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/test_push/{username}")
async def test_push(username: str):
    """
    DEBUG: Manually trigger a test push notification to all devices of a user.
    Used to verify FCM delivery without needing a real message.
    Remove this endpoint after debugging is complete.
    """
    from services.push import send_web_push
    import asyncio
    asyncio.create_task(send_web_push(
        recipient_username=username.lower(),
        title="🧪 Test Push",
        body="This is a test notification from the server.",
        sender="system",
        avatar="https://text2.co/chat/apple-touch-icon.png"
    ))
    return {"status": "triggered", "username": username}
