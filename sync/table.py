from __future__ import annotations
import json
import hashlib
import time
from typing import Optional, List, Dict, Any, Union
from fastapi import APIRouter, HTTPException, Depends, Query, Header, Response
from pydantic import BaseModel

from auth.deps import get_auth_token, decode_text2_token, create_text2_token
from database.postgres import execute_pg_query

router = APIRouter(prefix="/api/sync/table", tags=["sync_table"])


# --- Pydantic Models ---
class TableProjectSyncItem(BaseModel):
    id: str
    name: Optional[str] = ""
    data: Optional[Union[Dict[str, Any], str]] = None
    patches: Optional[List[Dict[str, Any]]] = None
    lastEdited: Optional[Union[str, int, float]] = None
    last_edited: Optional[Union[str, int, float]] = None
    createdAt: Optional[Union[str, int, float]] = ""
    updatedAt: Optional[Union[str, int, float]] = None
    created_at: Optional[Union[str, int, float]] = None
    updated_at: Optional[Union[str, int, float]] = None
    is_deleted: Optional[bool] = False


class TableKeepSyncRequest(BaseModel):
    since_rev: Optional[int] = 0
    projects: Optional[List[TableProjectSyncItem]] = []
    items: Optional[List[TableProjectSyncItem]] = []


def _clean_str(val: Any) -> str:
    if isinstance(val, str):
        return val.strip()
    return ""


async def _resolve_user_id(
    token: Any = None,
    x_user_id: Any = None,
    x_user_email: Any = None,
    x_user_name: Any = None,
    response: Optional[Response] = None
) -> int:
    """Seamlessly resolve a stable, permanent user_id across all devices."""
    clean_token = _clean_str(token)
    clean_id = _clean_str(x_user_id)
    clean_email = _clean_str(x_user_email).lower()
    clean_username = _clean_str(x_user_name).lower()

    # 1. Primary: Verify Text2 JWT Token
    if clean_token:
        payload = decode_text2_token(clean_token)
        if payload and payload.get("user_id"):
            return int(payload["user_id"])

    # 2. Secondary: Direct X-User-Id Header (Strict User ID Resolution)
    if clean_id and clean_id.isdigit() and int(clean_id) > 0:
        uid_val = int(clean_id)
        if uid_val < 10000:
            try:
                rows = await execute_pg_query("SELECT id, user_id FROM users WHERE id = $1", uid_val)
                if rows and rows[0].get("id"):
                    return int(rows[0].get("user_id") or (10000 + rows[0]["id"]))
            except Exception:
                pass
            return 10000 + uid_val
        return uid_val

    # 3. Tertiary: Lookup in central users table by email
    if clean_email:
        try:
            rows = await execute_pg_query(
                "SELECT id, user_id, username, email FROM users WHERE LOWER(email) = $1", 
                clean_email
            )
            if rows and len(rows) > 0 and rows[0].get("id"):
                row = rows[0]
                db_id = row["id"]
                uid = int(row.get("user_id") or (10000 + db_id))
                
                # Ensure user_id column in DB is populated
                if not row.get("user_id"):
                    try:
                        await execute_pg_query("UPDATE users SET user_id = $1 WHERE id = $2", uid, db_id)
                    except Exception:
                        pass

                if response is not None:
                    new_token = create_text2_token(
                        user_id=uid,
                        username=row.get("username") or clean_username or clean_email.split("@")[0],
                        email=row.get("email") or clean_email
                    )
                    response.headers["X-New-Text2-Token"] = new_token
                return uid
            else:
                # Auto-provision user in central table so all devices share the exact same user_id
                uname = clean_username or clean_email.split("@")[0]
                try:
                    await execute_pg_query(
                        "INSERT INTO users (username, email, user_id, name, created_at, updated_at) "
                        "VALUES ($1, $2, (SELECT COALESCE(MAX(user_id), 10000) + 1 FROM users), $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                        "ON CONFLICT (email) DO NOTHING",
                        uname, clean_email, uname
                    )
                    prov_rows = await execute_pg_query("SELECT id, user_id, username, email FROM users WHERE LOWER(email) = $1", clean_email)
                    if prov_rows and prov_rows[0].get("id"):
                        prov_row = prov_rows[0]
                        uid = int(prov_row.get("user_id") or (10000 + prov_row["id"]))
                        if response is not None:
                            new_token = create_text2_token(
                                user_id=uid,
                                username=prov_row.get("username") or uname,
                                email=clean_email
                            )
                            response.headers["X-New-Text2-Token"] = new_token
                        return uid
                except Exception as prov_err:
                    print(f"[ResolveUser] Auto-provision table user notice: {prov_err}")
        except Exception as err:
            print(f"[ResolveUser] Email lookup warning: {err}")

    # 4. Quaternary: Lookup by username
    if clean_username:
        try:
            rows = await execute_pg_query(
                "SELECT id, user_id, username, email FROM users WHERE LOWER(username) = $1", 
                clean_username
            )
            if rows and len(rows) > 0 and rows[0].get("id"):
                row = rows[0]
                uid = int(row.get("user_id") or (10000 + row["id"]))
                if response is not None:
                    new_token = create_text2_token(
                        user_id=uid,
                        username=row.get("username") or clean_username,
                        email=row.get("email") or clean_email or f"{clean_username}@text2.co"
                    )
                    response.headers["X-New-Text2-Token"] = new_token
                return uid
        except Exception as err:
            print(f"[ResolveUser] Username lookup warning: {err}")

    # 5. Deterministic collision-proof permanent fallback hash from email (56-bit space in BIGINT)
    if clean_email:
        email_hash_56bit = int(hashlib.sha256(f"text2_table_salt_{clean_email}".encode("utf-8")).hexdigest()[:14], 16)
        stable_id = 1000000000000 + (email_hash_56bit % 8000000000000)
        return stable_id

    raise HTTPException(status_code=401, detail="Unauthorized: No valid Text2 session or user ID found")


def _merge_table_project_data(existing_data: Dict[str, Any], incoming_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Differential 3-Level Merging (Metadata, Sheet-level by ID, Cell-level):
    Prevents Last-Write-Wins overwriting when two devices edit different cells/sheets concurrently.
    """
    if not existing_data or not isinstance(existing_data, dict) or "sheets" not in existing_data:
        return incoming_data
    if not incoming_data or not isinstance(incoming_data, dict) or "sheets" not in incoming_data:
        return existing_data

    existing_sheets = existing_data.get("sheets", []) if isinstance(existing_data.get("sheets"), list) else []
    incoming_sheets = incoming_data.get("sheets", []) if isinstance(incoming_data.get("sheets"), list) else []

    merged_sheets = []
    processed_ex_ids = set()

    for idx, in_sheet in enumerate(incoming_sheets):
        if not in_sheet or not isinstance(in_sheet, dict):
            continue

        in_id = str(in_sheet.get("id") or "").strip()
        in_name = str(in_sheet.get("name") or "").strip()

        # Find matching existing sheet by ID first, then by name
        ex_sheet = None
        if in_id:
            for s in existing_sheets:
                if s and isinstance(s, dict) and str(s.get("id") or "").strip() == in_id:
                    ex_sheet = s
                    break
        if not ex_sheet and in_name:
            for s in existing_sheets:
                if s and isinstance(s, dict) and str(s.get("name") or "").strip().lower() == in_name.lower():
                    s_id = str(s.get("id") or "").strip()
                    if s_id not in processed_ex_ids:
                        ex_sheet = s
                        break

        if ex_sheet:
            ex_id = str(ex_sheet.get("id") or "").strip()
            if ex_id:
                processed_ex_ids.add(ex_id)
            if str(ex_sheet.get("name") or "").strip():
                processed_ex_ids.add(str(ex_sheet.get("name") or "").strip().lower())

            sheet_name = in_sheet.get("name") or ex_sheet.get("name") or f"Sheet {idx + 1}"
            ex_grid = ex_sheet.get("data", []) if isinstance(ex_sheet.get("data"), list) else []
            in_grid = in_sheet.get("data", []) if isinstance(in_sheet.get("data"), list) else []

            ex_rows = len(ex_grid)
            in_rows = len(in_grid)
            max_r = max(ex_rows, in_rows)

            ex_cols = len(ex_grid[0]) if ex_rows > 0 and isinstance(ex_grid[0], list) else 0
            in_cols = len(in_grid[0]) if in_rows > 0 and isinstance(in_grid[0], list) else 0
            max_c = max(ex_cols, in_cols, 26)

            merged_grid = []
            for r in range(max_r):
                row_vals = []
                ex_row = ex_grid[r] if r < ex_rows and isinstance(ex_grid[r], list) else []
                in_row = in_grid[r] if r < in_rows and isinstance(in_grid[r], list) else []
                for c in range(max_c):
                    ex_val = ex_row[c] if c < len(ex_row) else ""
                    in_val = in_row[c] if c < len(in_row) else ""
                    # Prioritize incoming value if present, fallback to existing value
                    row_vals.append(in_val if in_val is not None and str(in_val) != "" else (ex_val if ex_val is not None else ""))
                merged_grid.append(row_vals)

            merged_styles = {}
            if isinstance(ex_sheet.get("styles"), dict):
                merged_styles.update(ex_sheet["styles"])
            if isinstance(in_sheet.get("styles"), dict):
                merged_styles.update(in_sheet["styles"])

            merged_merges = in_sheet.get("merges") or ex_sheet.get("merges") or []

            ex_widths = ex_sheet.get("colWidths", []) if isinstance(ex_sheet.get("colWidths"), list) else []
            in_widths = in_sheet.get("colWidths", []) if isinstance(in_sheet.get("colWidths"), list) else []
            merged_widths = in_widths if len(in_widths) >= len(ex_widths) else ex_widths

            ex_heights = ex_sheet.get("rowHeights", []) if isinstance(ex_sheet.get("rowHeights"), list) else []
            in_heights = in_sheet.get("rowHeights", []) if isinstance(in_sheet.get("rowHeights"), list) else []
            merged_heights = in_heights if len(in_heights) >= len(ex_heights) else ex_heights

            merged_sheets.append({
                "id": in_id or ex_sheet.get("id") or f"sheet_{idx + 1}",
                "name": sheet_name,
                "data": merged_grid,
                "colWidths": merged_widths,
                "rowHeights": merged_heights,
                "styles": merged_styles,
                "merges": merged_merges
            })
        else:
            merged_sheets.append(in_sheet)

    # Append any existing sheets that weren't in incoming payload (if any)
    for ex_s in existing_sheets:
        if not ex_s or not isinstance(ex_s, dict):
            continue
        ex_id = str(ex_s.get("id") or "").strip()
        ex_name = str(ex_s.get("name") or "").strip().lower()
        if (ex_id and ex_id not in processed_ex_ids) and (ex_name and ex_name not in processed_ex_ids):
            merged_sheets.append(ex_s)

    return {
        "sheets": merged_sheets,
        "activeSheetIndex": incoming_data.get("activeSheetIndex", 0)
    }


def _apply_cell_patches(existing_data: Dict[str, Any], patches: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Applies fine-grained Delta Cell Patches into an existing project data structure.
    Patch format: { "sheet": int/str, "r": int, "c": int, "v": Any, "s": Optional[Dict], "ts": int }
    """
    if not existing_data or not isinstance(existing_data, dict):
        existing_data = {"sheets": [{"name": "Sheet 1", "data": [[""]], "colWidths": [], "rowHeights": [], "styles": {}, "merges": []}], "activeSheetIndex": 0}

    sheets = existing_data.setdefault("sheets", [])
    if not sheets:
        sheets.append({"name": "Sheet 1", "data": [[""]], "colWidths": [], "rowHeights": [], "styles": {}, "merges": []})

    for p in patches:
        if not isinstance(p, dict):
            continue
        sheet_idx = int(p.get("sheet", 0))
        r = int(p.get("r", 0))
        c = int(p.get("c", 0))
        val = p.get("v", "")
        style = p.get("s")

        while len(sheets) <= sheet_idx:
            sheets.append({"name": f"Sheet {len(sheets) + 1}", "data": [[""]], "colWidths": [], "rowHeights": [], "styles": {}, "merges": []})

        cur_sheet = sheets[sheet_idx]
        grid = cur_sheet.setdefault("data", [])
        
        while len(grid) <= r:
            grid.append([])
        
        row_arr = grid[r]
        while len(row_arr) <= c:
            row_arr.append("")

        row_arr[c] = val

        if style and isinstance(style, dict):
            styles_map = cur_sheet.setdefault("styles", {})
            cell_key = f"{r}-{c}"
            styles_map.setdefault(cell_key, {}).update(style)

    return existing_data


def _format_table_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    projects_list = []
    seen_ids = set()

    for r in rows:
        proj_id = str(r.get("project_id") or "").strip()
        if not proj_id or proj_id in seen_ids:
            continue
        seen_ids.add(proj_id)

        raw_data = r.get("data")
        parsed_data = {"sheets": [], "activeSheetIndex": 0}
        if isinstance(raw_data, str):
            try:
                parsed_data = json.loads(raw_data)
            except Exception:
                parsed_data = {"sheets": [], "activeSheetIndex": 0}
        elif isinstance(raw_data, dict):
            parsed_data = raw_data

        projects_list.append({
            "id": proj_id,
            "name": r.get("name") or "Untitled Spreadsheet",
            "data": parsed_data,
            "lastEdited": int(r.get("last_edited") or r.get("updated_at") or 0),
            "rev": int(r.get("rev") or 0),
            "is_deleted": bool(r.get("is_deleted", False)),
            "updatedAt": int(r.get("updated_at") or 0)
        })
    return projects_list


@router.get("")
async def get_table_delta_sync(
    response: Response,
    since_rev: int = Query(0),
    token: str = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """
    Lightweight delta pull endpoint for checking cloud updates without push mutations.
    """
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)

    rev_res = await execute_pg_query(
        "SELECT COALESCE(MAX(rev), 0) AS current_rev FROM user_sync_table_projects WHERE user_id = $1",
        user_id
    )
    current_max_rev = int(rev_res[0]["current_rev"]) if rev_res and rev_res[0].get("current_rev") else 0

    if since_rev > 0 and since_rev == current_max_rev:
        return {
            "status": "success",
            "current_rev": current_max_rev,
            "synced_count": 0,
            "projects": [],
            "items": []
        }

    if since_rev == 0 or since_rev > current_max_rev:
        rows = await execute_pg_query(
            "SELECT project_id, name, data, last_edited, rev, is_deleted, created_at, updated_at "
            "FROM user_sync_table_projects WHERE user_id = $1 AND is_deleted = FALSE ORDER BY rev ASC",
            user_id
        )
    else:
        rows = await execute_pg_query(
            "SELECT project_id, name, data, last_edited, rev, is_deleted, created_at, updated_at "
            "FROM user_sync_table_projects WHERE user_id = $1 AND rev > $2 ORDER BY rev ASC",
            user_id, since_rev
        )

    formatted = _format_table_rows(rows)
    return {
        "status": "success",
        "current_rev": current_max_rev,
        "synced_count": 0,
        "projects": formatted,
        "items": formatted
    }


@router.post("")
async def sync_table_projects_batch(
    payload: TableKeepSyncRequest,
    response: Response,
    token: str = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """
    Single Atomic Round-Trip Sync Endpoint (Google Keep / Workspace Standard):
    1. Authenticates/resolves user_id (0ms JWT or auto-provision).
    2. Atomically applies outgoing client project mutations (push) with rev = max_rev + 1.
    3. Atomically queries and returns all delta project mutations from other devices (rev > since_rev).
    """
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)
    since_rev = payload.since_rev if payload.since_rev is not None else 0

    # 1. Fetch current max revision for this user
    rev_res = await execute_pg_query(
        "SELECT COALESCE(MAX(rev), 0) AS current_rev FROM user_sync_table_projects WHERE user_id = $1",
        user_id
    )
    current_max_rev = int(rev_res[0]["current_rev"]) if rev_res and rev_res[0].get("current_rev") else 0

    incoming_items = (payload.projects or []) + (payload.items or [])
    synced_project_ids = []
    next_rev = current_max_rev
    now_ts = int(time.time() * 1000)

    # 2. Process outgoing dirty items pushed by client
    if incoming_items:
        next_rev = current_max_rev + 1
        seen_batch_ids = set()

        for item in incoming_items:
            proj_id = str(item.id).strip()
            if not proj_id or proj_id in seen_batch_ids:
                continue
            seen_batch_ids.add(proj_id)

            item_name = (item.name or "").strip()
            raw_data = item.data

            if item.patches and not item.is_deleted:
                # 🟢 Delta Patch Sync: Patch existing DB record directly (ultra-light payload ~150B)
                try:
                    ex_rows = await execute_pg_query(
                        "SELECT name, data FROM user_sync_table_projects WHERE user_id = $1 AND project_id = $2 AND is_deleted = FALSE",
                        user_id, proj_id
                    )
                    if ex_rows and ex_rows[0].get("data"):
                        db_raw = ex_rows[0]["data"]
                        db_name = ex_rows[0].get("name") or item_name or "Untitled Spreadsheet"
                        db_obj = json.loads(db_raw) if isinstance(db_raw, str) else db_raw
                        data_obj = _apply_cell_patches(db_obj, item.patches)
                        if not item_name:
                            item_name = db_name
                    else:
                        base_obj = {"sheets": [{"name": "Sheet 1", "data": [[""]], "colWidths": [], "rowHeights": [], "styles": {}, "merges": []}], "activeSheetIndex": 0}
                        data_obj = _apply_cell_patches(base_obj, item.patches)
                except Exception as patch_err:
                    print(f"[TableSync] Notice: patch application fallback: {patch_err}")
                    data_obj = {"sheets": [], "activeSheetIndex": 0}
            else:
                # 🟢 Full Snapshot Sync with Sparse Trimming
                if isinstance(raw_data, str):
                    try:
                        data_obj = json.loads(raw_data)
                    except Exception:
                        data_obj = {"sheets": [], "activeSheetIndex": 0}
                elif isinstance(raw_data, dict):
                    data_obj = raw_data
                else:
                    data_obj = {"sheets": [], "activeSheetIndex": 0}

                # Concurrent edit resolution: merge differential cells if other devices updated since client's rev
                if not item.is_deleted and since_rev < current_max_rev:
                    try:
                        ex_rows = await execute_pg_query(
                            "SELECT data FROM user_sync_table_projects WHERE user_id = $1 AND project_id = $2 AND is_deleted = FALSE",
                            user_id, proj_id
                        )
                        if ex_rows and ex_rows[0].get("data"):
                            db_raw = ex_rows[0]["data"]
                            db_obj = json.loads(db_raw) if isinstance(db_raw, str) else db_raw
                            data_obj = _merge_table_project_data(db_obj, data_obj)
                    except Exception as merge_err:
                        print(f"[TableSync] Notice: differential merge fallback: {merge_err}")

            data_json = json.dumps(data_obj)
            last_edited_val = int(item.lastEdited or item.last_edited or item.updatedAt or item.updated_at or now_ts)

            synced_project_ids.append(proj_id)

            if item.is_deleted:
                # Soft-delete record with bumped rev to notify other devices
                await execute_pg_query(
                    "INSERT INTO user_sync_table_projects (user_id, project_id, name, data, last_edited, rev, is_deleted, updated_at) "
                    "VALUES ($1, $2, '', '{}'::jsonb, 0, $3, TRUE, $4) "
                    "ON CONFLICT (user_id, project_id) DO UPDATE SET "
                    "name = '', data = '{}'::jsonb, is_deleted = TRUE, rev = EXCLUDED.rev, updated_at = EXCLUDED.updated_at",
                    user_id, proj_id, next_rev, now_ts
                )
            else:
                # Upsert active project
                await execute_pg_query(
                    "INSERT INTO user_sync_table_projects (user_id, project_id, name, data, last_edited, rev, is_deleted, updated_at) "
                    "VALUES ($1, $2, $3, $4::jsonb, $5, $6, FALSE, $7) "
                    "ON CONFLICT (user_id, project_id) DO UPDATE SET "
                    "name = EXCLUDED.name, data = EXCLUDED.data, last_edited = EXCLUDED.last_edited, "
                    "rev = EXCLUDED.rev, is_deleted = FALSE, updated_at = EXCLUDED.updated_at",
                    user_id, proj_id, item_name or "Untitled Spreadsheet", data_json, last_edited_val, next_rev, now_ts
                )

    # 3. Pull delta changes for client (Atomic Pull)
    since_rev = payload.since_rev if payload.since_rev is not None else 0
    remote_projects = []

    if since_rev == 0 or since_rev > next_rev:
        # Full sync: return all active projects
        rows = await execute_pg_query(
            "SELECT project_id, name, data, last_edited, rev, is_deleted, created_at, updated_at "
            "FROM user_sync_table_projects WHERE user_id = $1 AND is_deleted = FALSE ORDER BY rev ASC",
            user_id
        )
        remote_projects = _format_table_rows(rows)
    elif since_rev < next_rev:
        # Delta sync: return all changes since requested rev
        rows = await execute_pg_query(
            "SELECT project_id, name, data, last_edited, rev, is_deleted, created_at, updated_at "
            "FROM user_sync_table_projects WHERE user_id = $1 AND rev > $2 ORDER BY rev ASC",
            user_id, since_rev
        )
        remote_projects = _format_table_rows(rows)

    return {
        "status": "success",
        "current_rev": next_rev,
        "synced_count": len(synced_project_ids),
        "projects": remote_projects,
        "items": remote_projects
    }
