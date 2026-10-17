import json
from fastapi import APIRouter, HTTPException
from database.postgres import execute_pg_query
from websocket.manager import active_connections, safe_send_ws, remove_dead_socket
from models.schemas import CallRejectRequest

router = APIRouter(prefix="/api", tags=["calls"])


@router.post("/call/reject")
async def api_call_reject(req: CallRejectRequest):
    caller = req.caller.strip().lower()
    recipient = req.recipient.strip().lower()

    if caller in active_connections:
        success = False
        payload = json.dumps({
            "type": "call_reject",
            "sender": recipient,
            "recipient": caller
        })
        for ws in list(active_connections[caller]):
            ok = await safe_send_ws(ws, payload, timeout=2.0)
            if ok:
                success = True
            else:
                remove_dead_socket(ws, caller)

        if success:
            print(f"[REST Call Reject] Sent call_reject to @{caller} from @{recipient}")
            return {"status": "success"}
        else:
            raise HTTPException(status_code=500, detail="Failed to send call_reject to any active connection")

    print(f"[REST Call Reject] Caller @{caller} is offline. Rejection ignored.")
    return {"status": "caller_offline"}
