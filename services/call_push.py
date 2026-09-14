import asyncio
import json
from database.postgres import execute_pg_query
from services.push import send_single_web_push_with_retry

async def send_call_push(recipient_username: str, sender: str, avatar: str, is_video: bool = True):
    """
    Send a push notification for an incoming call.
    Uses the same single push logic as messages but with a distinct 'call_push' payload.
    """
    try:
        subscriptions = await execute_pg_query(
            "SELECT endpoint, p256dh, auth FROM chat_push_subscriptions WHERE username = $1",
            recipient_username.lower()
        )
    except Exception as e:
        print(f"[CALL_PUSH] Error fetching subscriptions for @{recipient_username}: {e}")
        return

    if not subscriptions:
        print(f"[CALL_PUSH] No subscriptions found for @{recipient_username}")
        return

    print(f"[CALL_PUSH] Sending call notification to @{recipient_username} ({len(subscriptions)} subscription(s))")

    # Ensure avatar is an absolute URL
    if avatar:
        avatar_str = str(avatar).strip()
        if not (avatar_str.startswith("http://") or avatar_str.startswith("https://") or avatar_str.startswith("data:")):
            if avatar_str.startswith("/"):
                avatar = "https://text2.co" + avatar_str
            else:
                avatar = "https://text2.co/" + avatar_str

    safe_icon = avatar if (avatar and any(avatar.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"])) \
        else "https://text2.co/chat/apple-touch-icon.png"

    call_type_str = "Video Call" if is_video else "Audio Call"
    title = f"🔴 Incoming {call_type_str} 📞"
    body = f"{sender.upper()} is calling you right now! 🥵📞"

    payload = json.dumps({
        "type": "call_push",
        "recipient": recipient_username.lower(),
        "sender": sender,
        "title": title,
        "body": body,
        "avatar": safe_icon,
    })

    tasks = []
    for sub in subscriptions:
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}
        }
        tasks.append(send_single_web_push_with_retry(subscription_info, payload))

    if tasks:
        await asyncio.gather(*tasks)
