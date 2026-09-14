"""
Spac2 Realtime API - Entry Point
========================================
This file only wires together the FastAPI application.
All business logic lives in the respective modules:
  - config.py            : Environment variables & constants
  - database/postgres.py : PostgreSQL pool & schema initialization
  - database/sqlite.py   : SQLite fallback
  - storage/r2.py        : Cloudflare R2 & avatar processing
  - auth/deps.py         : Authentication helpers
  - models/schemas.py    : Pydantic models
  - websocket/manager.py : WebSocket connection state
  - websocket/handler.py : WebSocket endpoint logic
  - routers/             : HTTP API route handlers
  - services/push.py     : Web Push notifications
  - services/heartbeat.py: Background heartbeat
  - services/user_service.py: User profile business logic
"""

import asyncio
import os
import sys

# Ensure api directory is always in sys.path
api_dir = os.path.dirname(os.path.abspath(__file__))
if api_dir not in sys.path:
    sys.path.insert(0, api_dir)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import LOCAL_AVATARS_DIR, LOCAL_UPLOADS_DIR
from database.postgres import initialize_pg_pool, initialize_pg_schema
from services.heartbeat import heartbeat_check_loop

# --- Routers ---
from routers import auth, profile, friends, groups, messages, notifications, uploads, calls
from sync import note_router, task_router, calendar_router, countday_router, mindmap_router, table_router, doc_router




# --- WebSocket ---
from websocket.handler import websocket_endpoint

# --- Diagnostics ---
from storage.r2 import r2_client
from websocket.manager import active_connections
from config import (
    CLOUDFLARE_ACCOUNT_ID,
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
    R2_BUCKET_NAME,
)
import boto3
from botocore.config import Config

# Ensure local fallback directories exist
os.makedirs(LOCAL_AVATARS_DIR, exist_ok=True)
os.makedirs(LOCAL_UPLOADS_DIR, exist_ok=True)

app = FastAPI(title="Text2Chat Realtime API", version="1.0.0")

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# --- Static file mounts (local fallback for avatars/uploads) ---
app.mount("/data/avatars", StaticFiles(directory=LOCAL_AVATARS_DIR), name="avatars")
app.mount("/data/uploads", StaticFiles(directory=LOCAL_UPLOADS_DIR), name="uploads")

# --- Register routers ---
app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(friends.router)
app.include_router(groups.router)
app.include_router(messages.router)
app.include_router(notifications.router)
app.include_router(uploads.router)
app.include_router(calls.router)
app.include_router(note_router)
app.include_router(task_router)
app.include_router(calendar_router)
app.include_router(countday_router)
app.include_router(mindmap_router)
app.include_router(table_router)
app.include_router(doc_router)





# --- Register WebSocket endpoint ---
app.websocket("/ws/{username}")(websocket_endpoint)


# --- Startup & Shutdown Events ---
@app.on_event("startup")
async def startup_event():
    await initialize_pg_pool()
    await initialize_pg_schema()
    asyncio.create_task(heartbeat_check_loop())


# --- Health & Diagnostics ---
@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "Spac2 Realtime Server",
        "cloudflare_r2_active": r2_client is not None,
        "active_users": list(active_connections.keys())
    }


@app.get("/api/diagnostics")
async def api_diagnostics():
    r2_status = "inactive"
    r2_error = None
    bucket_contents = []

    try:
        if CLOUDFLARE_ACCOUNT_ID and AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
            endpoint = f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=AWS_ACCESS_KEY_ID,
                aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                config=Config(signature_version="s3v4")
            )
            res = client.list_objects_v2(Bucket=R2_BUCKET_NAME, MaxKeys=5)
            r2_status = "active"
            if "Contents" in res:
                bucket_contents = [obj["Key"] for obj in res["Contents"]]
        else:
            r2_error = "Missing Cloudflare R2 environment credentials"
    except Exception as e:
        r2_status = "error"
        r2_error = str(e)

    return {
        "cloudflare_account_id_set": CLOUDFLARE_ACCOUNT_ID is not None,
        "cloudflare_account_id": CLOUDFLARE_ACCOUNT_ID,
        "aws_access_key_id_set": AWS_ACCESS_KEY_ID is not None,
        "aws_secret_access_key_set": AWS_SECRET_ACCESS_KEY is not None,
        "r2_bucket_name": R2_BUCKET_NAME,
        "r2_status": r2_status,
        "r2_error": r2_error,
        "sample_keys": bucket_contents
    }
