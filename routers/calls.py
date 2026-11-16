import json
from fastapi import APIRouter
from websocket.manager import send_ws_to_user, is_user_online
from models.schemas import CallRejectRequest

router = APIRouter(prefix="/api", tags=["calls"])


@router.post("/call/reject")
async def api_call_reject(req: CallRejectRequest):
    caller = req.caller.strip().lower()
    recipient = req.recipient.strip().lower()

    payload = json.dumps({
        "type": "call_reject",
        "sender": recipient,
        "recipient": caller
    })
    local_sent = await send_ws_to_user(caller, payload)
    if local_sent > 0 or await is_user_online(caller):
        print(f"[REST Call Reject] Sent call_reject to @{caller} from @{recipient}")
        return {"status": "success"}

    print(f"[REST Call Reject] Caller @{caller} is offline. Rejection ignored.")
    return {"status": "caller_offline"}
