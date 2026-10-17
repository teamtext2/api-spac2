import asyncio
import json
from config import VAPID_PRIVATE_KEY, VAPID_CLAIMS, DEFAULT_AVATAR_URL
from database.postgres import execute_pg_query


async def send_single_web_push_with_retry(subscription_info: dict, payload: str, retries: int = 2) -> bool:
    """
    Send a single web push notification with exponential backoff retry.
    Includes strict HTTP timeouts to prevent thread pool exhaustion and event loop freezes.
    """
    try:
        import anyio
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("[PUSH] pywebpush or anyio is not installed. Skipping push notification sending.")
        return False

    delay = 0.5
    endpoint = subscription_info.get("endpoint", "")
    endpoint_preview = endpoint[:80] + "..." if len(endpoint) > 80 else endpoint

    # Detect platform by endpoint URL — only reliable signal at backend
    is_apple = "push.apple.com" in endpoint
    is_fcm   = "fcm.googleapis.com" in endpoint or "firebase.com" in endpoint
    if is_apple:
        platform = "iOS"
    elif is_fcm:
        platform = "Android/FCM"
    else:
        platform = "Desktop/Firefox"

    for attempt in range(retries):
        try:
            def _push():
                effective_ttl = 86400
                headers = {"Urgency": "high"}
                if is_apple:
                    headers["apns-priority"] = "10"
                    headers["apns-push-type"] = "alert"
                webpush(
                    subscription_info=subscription_info,
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims=VAPID_CLAIMS.copy(),
                    ttl=effective_ttl,
                    content_encoding="aes128gcm",
                    headers=headers,
                    timeout=5.0
                )

            # Strict 7-second ceiling per push delivery attempt
            await asyncio.wait_for(anyio.to_thread.run_sync(_push), timeout=7.0)
            return True
        except asyncio.TimeoutError:
            print(f"[PUSH] ✗ Timeout (7s ceiling exceeded) delivering to {platform}: {endpoint_preview}")
        except Exception as ex:
            # Capture full response details for debugging
            status_code = None
            response_body = ""
            if hasattr(ex, 'response') and ex.response is not None:
                status_code = ex.response.status_code
                try:
                    response_body = ex.response.text[:300]
                except Exception:
                    response_body = str(ex.response)

            print(f"[PUSH] ✗ Error ({platform}) attempt {attempt+1}/{retries} — "
                  f"status={status_code} body={response_body!r} error={ex}")

            should_delete = (
                status_code == 410 or
                (status_code == 404 and not is_apple)
            )
            if should_delete:
                try:
                    await execute_pg_query(
                        "DELETE FROM chat_push_subscriptions WHERE endpoint = $1",
                        endpoint
                    )
                    print(f"[PUSH] Deleted revoked subscription ({platform}): {endpoint_preview}")
                except Exception as err:
                    print(f"[PUSH] Failed to delete revoked subscription: {err}")
                break

            if status_code == 413:
                print(f"[PUSH] Payload too large for {platform}. Skipping retry.")
                break

            if attempt < retries - 1:
                await asyncio.sleep(delay)
                delay *= 2
    return False


async def send_web_push(recipient_username: str, title: str, body: str, sender: str, avatar: str):
    """
    Send a push notification for a new chat message.
    Compatible with iOS Safari 16.4+, Android Chrome, Firefox, Edge.

    IMPORTANT: We send a FULL payload (title + body + avatar) rather than a
    silent_ping that requires SW to fetch from API. This avoids:
      - Android Doze/Battery-Saver mode blocking SW network requests
      - Token expiry causing fetch failures in background SW
      - Extra round-trip latency before notification appears
    """
    import re
    # Normalize attachment markdown to a human-friendly body
    attachment_pattern = re.compile(
        r'(?:📎\s*)?\[Attachment:\s*([^\]]+)\]\s*\((https?://[^\s\)]+|blob:[^\s\)]+|/[^\s\)]+)\)'
    )
    attachments = attachment_pattern.findall(body)
    if attachments:
        count = len(attachments)
        body = "You received a new media." if count == 1 else f"You received {count} new media."

    # Truncate body to avoid FCM payload size limit (4KB total)
    if body and len(body) > 200:
        body = body[:197] + "..."

    try:
        from websocket.manager import get_email_by_username
        recipient_user_clean = recipient_username.strip().lower()
        email = await get_email_by_username(recipient_user_clean)
        
        if email:
            subscriptions = await execute_pg_query(
                "SELECT endpoint, p256dh, auth FROM chat_push_subscriptions WHERE username = $1 OR username = $2 ORDER BY id DESC LIMIT 5",
                recipient_user_clean, email.lower()
            )
        else:
            subscriptions = await execute_pg_query(
                "SELECT endpoint, p256dh, auth FROM chat_push_subscriptions WHERE username = $1 ORDER BY id DESC LIMIT 5",
                recipient_user_clean
            )
    except Exception as e:
        print(f"[PUSH] Error fetching subscriptions for @{recipient_username}: {e}")
        return

    if not subscriptions:
        return

    # Ensure avatar is an absolute URL for iOS/Android push daemon compatibility
    if avatar:
        avatar_str = str(avatar).strip()
        if not (avatar_str.startswith("http://") or avatar_str.startswith("https://") or avatar_str.startswith("data:")):
            if avatar_str.startswith("/"):
                avatar = "https://spac2.com" + avatar_str
            else:
                avatar = "https://spac2.com/" + avatar_str

    safe_icon = avatar if (avatar and any(avatar.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"])) \
        else DEFAULT_AVATAR_URL

    payload = json.dumps({
        "type": "message_push",
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
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10.0)
        except Exception as gather_err:
            print(f"[PUSH] Notice during push dispatch: {gather_err}")


# NOTE: send_call_push has been intentionally removed.
# Video/audio calls use WebSocket signaling exclusively.
# Push notifications for calls are NOT sent — calls appear in-app only.
# This avoids disruption on iOS where call push behavior is unreliable,
# and prevents interference with the WebRTC call flow.
