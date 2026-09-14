# Sync module for Spac2 Ecosystem Apps
from sync.note import router as note_router
from sync.task import router as task_router
from sync.calendar import router as calendar_router
from sync.countday import router as countday_router
from sync.mindmap import router as mindmap_router
from sync.table import router as table_router
from sync.doc import router as doc_router

__all__ = ["note_router", "task_router", "calendar_router", "countday_router", "mindmap_router", "table_router", "doc_router"]



