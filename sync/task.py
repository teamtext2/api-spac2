from __future__ import annotations
import json
import hashlib
import time
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Query, Header, Response
from pydantic import BaseModel

from auth.deps import get_auth_token, decode_spac2_token, create_spac2_token, decode_text2_token, create_text2_token
from database.postgres import execute_pg_query

router = APIRouter(prefix="/api/sync/task", tags=["sync_task"])


# --- Pydantic Models ---
class TaskProjectSyncItem(BaseModel):
    id: str
    name: Optional[str] = ""
    color: Optional[Dict[str, Any]] = None
    createdAt: Optional[str] = ""
    is_deleted: Optional[bool] = False


class TaskItemSyncItem(BaseModel):
    id: str
    projectId: Optional[str] = ""
    title: Optional[str] = ""
    note: Optional[str] = ""
    priority: Optional[str] = "normal"
    dueDate: Optional[str] = ""
    completed: Optional[bool] = False
    completedAt: Optional[str] = ""
    subtasks: Optional[List[Dict[str, Any]]] = []
    createdAt: Optional[str] = ""
    is_deleted: Optional[bool] = False


class TaskKeepSyncRequest(BaseModel):
    since_rev: Optional[int] = 0
    projects: Optional[List[TaskProjectSyncItem]] = []
    tasks: Optional[List[TaskItemSyncItem]] = []


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

    # 1. Primary: Verify Spac2 JWT Token
    if clean_token:
        payload = decode_spac2_token(clean_token)
        if payload and payload.get("user_id"):
            return int(payload["user_id"])

    # 2. Secondary: Lookup in central users table by email
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
                    new_token = create_spac2_token(
                        user_id=uid,
                        username=row.get("username") or clean_username or clean_email.split("@")[0],
                        email=row.get("email") or clean_email
                    )
                    response.headers["X-New-Spac2-Token"] = new_token
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
                            new_token = create_spac2_token(
                                user_id=uid,
                                username=prov_row.get("username") or uname,
                                email=clean_email
                            )
                            response.headers["X-New-Spac2-Token"] = new_token
                        return uid
                except Exception as prov_err:
                    print(f"[ResolveUser] Auto-provision task user notice: {prov_err}")
        except Exception as err:
            print(f"[ResolveUser] Email lookup warning: {err}")

    # 3. Tertiary: Lookup by username
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
                    new_token = create_spac2_token(
                        user_id=uid,
                        username=row.get("username") or clean_username,
                        email=row.get("email") or clean_email or f"{clean_username}@spac2.com"
                    )
                    response.headers["X-New-Spac2-Token"] = new_token
                return uid
        except Exception as err:
            print(f"[ResolveUser] Username lookup warning: {err}")

    # 4. Direct X-User-Id header if provided
    if x_user_id and str(x_user_id).isdigit() and int(x_user_id) > 0:
        uid_val = int(x_user_id)
        if uid_val < 10000:
            try:
                rows = await execute_pg_query("SELECT id, user_id FROM users WHERE id = $1", uid_val)
                if rows and rows[0].get("id"):
                    return int(rows[0].get("user_id") or (10000 + rows[0]["id"]))
            except Exception:
                pass
            return 10000 + uid_val
        return uid_val

    # 5. Deterministic permanent fallback hash from email
    if clean_email:
        email_hash_int = int(hashlib.sha256(clean_email.encode("utf-8")).hexdigest()[:8], 16)
        stable_id = 50000 + (email_hash_int % 40000)
        return stable_id

    raise HTTPException(status_code=401, detail="Unauthorized: No valid Spac2 session or token found")


def _is_dark_color(hex_color: str) -> bool:
    if not hex_color or not isinstance(hex_color, str):
        return False
    c = hex_color.replace("#", "").strip()
    if len(c) == 3:
        c = "".join([x + x for x in c])
    if len(c) != 6:
        return False
    try:
        r = int(c[0:2], 16)
        g = int(c[2:4], 16)
        b = int(c[4:6], 16)
        yiq = (r * 299 + g * 587 + b * 114) / 1000
        return yiq < 140
    except Exception:
        return False


def _normalize_project_color(color_val: Any) -> Dict[str, str]:
    if not color_val:
        return {"bg": "#3B82F6", "text": "#FFFFFF", "name": "Blue"}
    
    cur = color_val
    for _ in range(3):
        if isinstance(cur, str):
            cur_s = cur.strip()
            if cur_s.startswith("{"):
                try:
                    cur = json.loads(cur_s)
                except Exception:
                    break
            elif cur_s.startswith('"') and cur_s.endswith('"'):
                try:
                    cur = json.loads(cur_s)
                except Exception:
                    break
            elif cur_s.startswith("#"):
                return {"bg": cur_s, "text": "#FFFFFF", "name": "Color"}
            else:
                break
        else:
            break

    if isinstance(cur, dict):
        bg = str(cur.get("bg") or "#3B82F6")
        text = str(cur.get("text") or "#FFFFFF")
        name = str(cur.get("name") or "Project")
        return {"bg": bg, "text": text, "name": name}

    return {"bg": "#3B82F6", "text": "#FFFFFF", "name": "Blue"}


def _format_project_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items_list = []
    seen_ids = set()

    for r in rows:
        proj_id = str(r.get("project_id") or "").strip()
        if not proj_id or proj_id in seen_ids or proj_id in ['proj-1', 'proj-2', 'proj-3']:
            continue

        seen_ids.add(proj_id)
        name = (r.get("name") or "").strip()
        created_at_str = (r.get("created_at_str") or "").strip()
        is_deleted = bool(r.get("is_deleted"))

        color_val = _normalize_project_color(r.get("color"))

        items_list.append({
            "id": proj_id,
            "name": name,
            "color": color_val,
            "createdAt": created_at_str,
            "rev": int(r.get("rev") or 1),
            "is_deleted": is_deleted
        })
    return items_list


def _format_task_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items_list = []
    seen_ids = set()
    seen_sigs = set()

    for r in rows:
        task_id = str(r.get("task_id") or "").strip()
        if not task_id or task_id in seen_ids or task_id in ['task-1', 'task-2', 'task-3', 'task-4']:
            continue

        title = (r.get("title") or "").strip()
        project_id = str(r.get("project_id") or "").strip()
        created_at_str = (r.get("created_at") or r.get("date") or "").strip()
        if isinstance(created_at_str, str) and len(created_at_str) > 0:
            pass
        else:
            created_at_str = ""

        is_deleted = bool(r.get("is_deleted"))

        # Deduplicate identical tasks created within the same minute in the same project
        if not is_deleted and title:
            time_key = created_at_str[:16] if len(created_at_str) >= 16 else ""
            sig = f"{project_id}|||{title.lower()}|||{time_key}"
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)

        seen_ids.add(task_id)

        # Parse subtasks safely
        subtasks_raw = r.get("subtasks")
        subtasks_list = []
        if isinstance(subtasks_raw, list):
            subtasks_list = subtasks_raw
        elif isinstance(subtasks_raw, str) and subtasks_raw.strip():
            try:
                parsed_sub = json.loads(subtasks_raw)
                if isinstance(parsed_sub, list):
                    subtasks_list = parsed_sub
            except Exception:
                subtasks_list = []

        items_list.append({
            "id": task_id,
            "projectId": project_id,
            "title": title,
            "note": (r.get("note") or "").strip(),
            "priority": (r.get("priority") or "normal").strip(),
            "dueDate": (r.get("due_date") or "").strip(),
            "completed": bool(r.get("completed")),
            "completedAt": (r.get("completed_at") or "").strip() or None,
            "subtasks": subtasks_list,
            "createdAt": created_at_str,
            "rev": int(r.get("rev") or 1),
            "is_deleted": is_deleted
        })
    return items_list


async def _get_current_max_rev(user_id: int) -> int:
    rev_res = await execute_pg_query(
        """
        SELECT COALESCE(MAX(rev), 0) AS current_rev FROM (
            SELECT MAX(rev) AS rev FROM user_sync_task_projects WHERE user_id = $1
            UNION ALL
            SELECT MAX(rev) AS rev FROM user_sync_tasks WHERE user_id = $1
        ) AS combined_rev
        """,
        user_id
    )
    return int(rev_res[0]["current_rev"]) if rev_res and rev_res[0].get("current_rev") else 0


@router.get("")
async def get_task_delta_sync(
    response: Response,
    since_rev: int = Query(0),
    token: str = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Google Keep-Style Lightweight Task Delta Pull.
    Returns projects and tasks created/updated/deleted since since_rev.
    """
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)

    try:
        current_rev = await _get_current_max_rev(user_id)

        if since_rev > 0 and since_rev == current_rev:
            return {
                "status": "success",
                "current_rev": current_rev,
                "projects": [],
                "tasks": []
            }

        if since_rev == 0 or since_rev > current_rev:
            # Full active projects & tasks pull
            p_rows = await execute_pg_query(
                "SELECT project_id, name, color, created_at_str, rev, is_deleted "
                "FROM user_sync_task_projects "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY rev ASC",
                user_id
            )
            t_rows = await execute_pg_query(
                "SELECT task_id, project_id, title, note, priority, due_date, completed, completed_at, subtasks, date, rev, is_deleted "
                "FROM user_sync_tasks "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY rev ASC",
                user_id
            )
        else:
            # Delta pull
            p_rows = await execute_pg_query(
                "SELECT project_id, name, color, created_at_str, rev, is_deleted "
                "FROM user_sync_task_projects "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY rev ASC",
                user_id, since_rev
            )
            t_rows = await execute_pg_query(
                "SELECT task_id, project_id, title, note, priority, due_date, completed, completed_at, subtasks, date, rev, is_deleted "
                "FROM user_sync_tasks "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY rev ASC",
                user_id, since_rev
            )

        return {
            "status": "success",
            "current_rev": current_rev,
            "projects": _format_project_rows(p_rows),
            "tasks": _format_task_rows(t_rows)
        }
    except Exception as e:
        print(f"[KeepSyncTask] Delta fetch error for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch delta tasks: {str(e)}")


@router.post("")
async def sync_keep_tasks_batch(
    payload: TaskKeepSyncRequest,
    response: Response,
    token: str = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Google Keep-Style Single-Trip Atomic Full Sync for Spac2 Task (Push + Pull).
    1. Saves all client project & task mutations with next revision.
    2. Atomically queries and returns remote projects & tasks updated since since_rev.
    """
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)

    try:
        # 1. Get current max revision
        current_max_rev = await _get_current_max_rev(user_id)
        
        has_mutations = (payload.projects and len(payload.projects) > 0) or (payload.tasks and len(payload.tasks) > 0)
        next_rev = (current_max_rev + 1) if has_mutations else current_max_rev
        now_ts = int(time.time() * 1000)

        synced_project_ids = []
        synced_task_ids = []

        # 2. Process Project mutations
        if payload.projects and len(payload.projects) > 0:
            for p in payload.projects:
                proj_id = str(p.id).strip()
                if not proj_id or proj_id in ['proj-1', 'proj-2', 'proj-3']:
                    continue

                p_name = (p.name or "").strip()
                color_dict = _normalize_project_color(p.color)
                color_json = json.dumps(color_dict)
                synced_project_ids.append(proj_id)

                if p.is_deleted:
                    await execute_pg_query(
                        "INSERT INTO user_sync_task_projects (user_id, project_id, name, color, created_at_str, rev, is_deleted, updated_at) "
                        "VALUES ($1, $2, '', '{}'::jsonb, '', $3, TRUE, $4) "
                        "ON CONFLICT (user_id, project_id) DO UPDATE SET "
                        "name = '', is_deleted = TRUE, rev = EXCLUDED.rev, updated_at = EXCLUDED.updated_at",
                        user_id, proj_id, next_rev, now_ts
                    )
                    # Cascade: Also mark all child tasks of this deleted project as is_deleted = TRUE
                    await execute_pg_query(
                        "UPDATE user_sync_tasks SET is_deleted = TRUE, rev = $1, updated_at = $2 "
                        "WHERE user_id = $3 AND project_id = $4 AND is_deleted = FALSE",
                        next_rev, now_ts, user_id, proj_id
                    )
                else:
                    await execute_pg_query(
                        "INSERT INTO user_sync_task_projects (user_id, project_id, name, color, created_at_str, rev, is_deleted, updated_at) "
                        "VALUES ($1, $2, $3, $4::jsonb, $5, $6, FALSE, $7) "
                        "ON CONFLICT (user_id, project_id) DO UPDATE SET "
                        "name = EXCLUDED.name, color = EXCLUDED.color, created_at_str = EXCLUDED.created_at_str, "
                        "rev = EXCLUDED.rev, is_deleted = FALSE, updated_at = EXCLUDED.updated_at",
                        user_id, proj_id, p_name, color_json, p.createdAt or "", next_rev, now_ts
                    )

        # 3. Process Task mutations
        if payload.tasks and len(payload.tasks) > 0:
            for t in payload.tasks:
                task_id = str(t.id).strip()
                if not task_id or task_id in ['task-1', 'task-2', 'task-3', 'task-4']:
                    continue

                t_title = (t.title or "").strip()
                t_proj_id = str(t.projectId or "").strip()
                subtasks_json = json.dumps(t.subtasks or [])
                synced_task_ids.append(task_id)


                if t.is_deleted:
                    await execute_pg_query(
                        "INSERT INTO user_sync_tasks (user_id, task_id, project_id, title, note, priority, due_date, completed, completed_at, subtasks, date, rev, is_deleted, updated_at) "
                        "VALUES ($1, $2, '', '', '', 'normal', '', FALSE, '', '[]'::jsonb, '', $3, TRUE, $4) "
                        "ON CONFLICT (user_id, task_id) DO UPDATE SET "
                        "title = '', note = '', is_deleted = TRUE, rev = EXCLUDED.rev, updated_at = EXCLUDED.updated_at",
                        user_id, task_id, next_rev, now_ts
                    )
                else:
                    await execute_pg_query(
                        "INSERT INTO user_sync_tasks (user_id, task_id, project_id, title, note, priority, due_date, completed, completed_at, subtasks, date, rev, is_deleted, updated_at) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, FALSE, $13) "
                        "ON CONFLICT (user_id, task_id) DO UPDATE SET "
                        "project_id = EXCLUDED.project_id, title = EXCLUDED.title, note = EXCLUDED.note, "
                        "priority = EXCLUDED.priority, due_date = EXCLUDED.due_date, completed = EXCLUDED.completed, "
                        "completed_at = EXCLUDED.completed_at, subtasks = EXCLUDED.subtasks, date = EXCLUDED.date, "
                        "rev = EXCLUDED.rev, is_deleted = FALSE, updated_at = EXCLUDED.updated_at",
                        user_id, task_id, t_proj_id, t_title, t.note or "",
                        t.priority or "normal", t.dueDate or "", bool(t.completed),
                        t.completedAt or "", subtasks_json, t.createdAt or "", next_rev, now_ts
                    )

        # 4. Pull Remote Updates
        since_rev = payload.since_rev if payload.since_rev is not None else 0
        remote_projects = []
        remote_tasks = []

        if since_rev == 0 or since_rev > next_rev:
            # Full sync: return all active projects and tasks
            p_rows = await execute_pg_query(
                "SELECT project_id, name, color, created_at_str, rev, is_deleted "
                "FROM user_sync_task_projects "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY rev ASC",
                user_id
            )
            t_rows = await execute_pg_query(
                "SELECT task_id, project_id, title, note, priority, due_date, completed, completed_at, subtasks, date, rev, is_deleted "
                "FROM user_sync_tasks "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY rev ASC",
                user_id
            )
            remote_projects = _format_project_rows(p_rows)
            remote_tasks = _format_task_rows(t_rows)
        elif since_rev < next_rev:
            # Delta sync
            p_rows = await execute_pg_query(
                "SELECT project_id, name, color, created_at_str, rev, is_deleted "
                "FROM user_sync_task_projects "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY rev ASC",
                user_id, since_rev
            )
            t_rows = await execute_pg_query(
                "SELECT task_id, project_id, title, note, priority, due_date, completed, completed_at, subtasks, date, rev, is_deleted "
                "FROM user_sync_tasks "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY rev ASC",
                user_id, since_rev
            )
            remote_projects = _format_project_rows(p_rows)
            remote_tasks = _format_task_rows(t_rows)

        return {
            "status": "success",
            "current_rev": next_rev,
            "synced_projects_count": len(synced_project_ids),
            "synced_tasks_count": len(synced_task_ids),
            "projects": remote_projects,
            "tasks": remote_tasks
        }
    except Exception as e:
        print(f"[KeepSyncTask] Batch sync error for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Task Keep Sync Error: {str(e)}")
