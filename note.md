# Spac2 Note Synchronization & Architecture Specification (`/api/sync/note`)

Tài liệu này đặc tả chi tiết kiến trúc đồng bộ dữ liệu, mô hình cơ sở dữ liệu, bảng màu pastel dành cho ứng dụng **Spac2 Note** (`/app/note/`).

---

## 1. Tổng Quan Kiến Trúc (Architecture Overview)

- **Domain**: Fast Note-taking & Pastel Memo Cards.
- **Client Codebase**: `/app/note/` (`index.html`, `js/main.js`, `js/sync.js`, `css/main.css`).
- **Backend Router**: `/api/sync/note.py` (`FastAPI`).
- **Database Table**: `user_sync_notes` (PostgreSQL).
- **URL Hash Route**: `https://spac2.com/app/note/#NOTE/{note_id}` (ví dụ: `#NOTE/N19s7A2x`).
- **Mô hình đồng bộ**: 1-1 Client-Server Push & Pull Sync, Monotonic Revision Counter, Anti-Resurrection Guard.

---

## 2. Mô Hình Cơ Sở Dữ Liệu (Database Schema)

```sql
CREATE TABLE IF NOT EXISTS user_sync_notes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    note_id VARCHAR(100) NOT NULL,
    title TEXT DEFAULT '',
    content TEXT DEFAULT '',
    color JSONB DEFAULT '{}',
    is_saved BOOLEAN DEFAULT FALSE,
    date TEXT DEFAULT '',
    history JSONB DEFAULT '[]',
    rev BIGINT NOT NULL DEFAULT 1,
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at BIGINT DEFAULT 0,
    CONSTRAINT uq_sync_notes_note_id UNIQUE (note_id)
);

-- Indices
CREATE INDEX IF NOT EXISTS idx_sync_notes_user_rev ON user_sync_notes(user_id, rev);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_notes_global_note_id ON user_sync_notes(note_id);
```

### Giải Thích Các Trường:
- `note_id`: Mã định danh ghi chú (Canonical 8-char hoặc `n_...`).
- `color`: Cấu hình màu nền / theme card ghi chú (`{ "bg": "#fff", "border": "#eee" }` hoặc mã HEX).
- `is_saved`: Trạng thái bookmark / pin vào danh mục Saved.
- `rev`: Số hiệu phiên bản đơn điệu tăng (Monotonic Revision).
- `is_deleted` & `deleted_at`: Soft-delete ngăn chặn thiết bị offline hồi sinh ghi chú cũ.

---

## 3. Giao Thức Đồng Bộ Dữ Liệu (Sync Protocol API)

### 3.1. Unified Sync Batch (`POST /api/sync/note`)

#### Request Body Payload (`NoteKeepSyncRequest`):
```json
{
  "sync_batch_id": "batch_note_1728219000_xyz",
  "since_rev": 5,
  "items": [
    {
      "id": "N19s7A2x",
      "title": "Ý tưởng dự án mới",
      "content": "Chi tiết ý tưởng và các bước triển khai...",
      "color": { "name": "Pastel Yellow", "bg": "#fef9c3", "text": "#713f12" },
      "isSaved": true,
      "date": "2026-10-06",
      "base_rev": 5,
      "rev": 6,
      "is_deleted": false,
      "updated": 1728219000000
    }
  ]
}
```

#### Response Body:
```json
{
  "status": "OK",
  "sync_batch_id": "batch_note_1728219000_xyz",
  "latest_rev": 6,
  "items": [
    {
      "id": "N19s7A2x",
      "title": "Ý tưởng dự án mới",
      "content": "Chi tiết ý tưởng...",
      "color": { "name": "Pastel Yellow", "bg": "#fef9c3", "text": "#713f12" },
      "is_saved": true,
      "date": "2026-10-06",
      "rev": 6,
      "is_deleted": false,
      "updated_at": 1728219000000
    }
  ],
  "server_time": 1728219001000
}
```

---

## 4. Hướng Dẫn Kiểm Thử Bằng cURL (Developer Cheat Sheet)

```bash
# 1. Đồng bộ ghi chú
curl -X POST https://spac2.com/api/sync/note \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "since_rev": 0,
    "items": [{
      "id": "n_sample_01",
      "title": "Ghi chú mẫu",
      "content": "Nội dung ghi chú nhanh...",
      "rev": 1
    }]
  }'
```
