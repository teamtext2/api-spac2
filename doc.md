# Spac2 Doc Synchronization & Architecture Specification (`/api/sync/doc`)

Tài liệu này đặc tả chi tiết kiến trúc đồng bộ dữ liệu, mô hình cơ sở dữ liệu, giao thức OCC (Optimistic Concurrency Control) dành cho ứng dụng **Spac2 Doc** (`/app/doc/`).

---

## 1. Tổng Quan Kiến Trúc (Architecture Overview)

- **Domain**: Document Processor & Rich-Text Word Processor.
- **Client Codebase**: `/app/doc/` (`index.html`, `js/main.js`, `js/sync.js`, `css/styles.css`).
- **Backend Router**: `/api/sync/doc.py` (`FastAPI`).
- **Database Table**: `user_sync_docs` (PostgreSQL).
- **URL Hash Route**: `https://spac2.com/app/doc/#DOC/{doc_id}` (ví dụ: `#DOC/977ViYdH`).
- **Mô hình đồng bộ**: 1-1 Monotonic Revision Sync (Single-Trip Push & Pull), Idempotency Token, OCC Conflict Resolution.

---

## 2. Mô Hình Cơ Sở Dữ Liệu (Database Schema)

```sql
CREATE TABLE IF NOT EXISTS user_sync_docs (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    doc_id VARCHAR(100) NOT NULL,
    title TEXT DEFAULT '',
    body TEXT DEFAULT '',
    preview_text TEXT DEFAULT '',
    word_count INT DEFAULT 0,
    pinned BOOLEAN DEFAULT FALSE,
    in_trash BOOLEAN DEFAULT FALSE,
    target INT DEFAULT 500,
    tabs JSONB DEFAULT '[]',
    active_tab_id VARCHAR(100) DEFAULT 'tab-default',
    history JSONB DEFAULT '[]',
    rev BIGINT NOT NULL DEFAULT 1,
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at BIGINT DEFAULT 0,
    CONSTRAINT uq_sync_docs_doc_id UNIQUE (doc_id)
);

-- Indices for high-throughput sync queries
CREATE INDEX IF NOT EXISTS idx_sync_docs_user_rev ON user_sync_docs(user_id, rev);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_docs_global_doc_id ON user_sync_docs(doc_id);
```

### Giải Thích Các Trường Chính:
- `doc_id`: Định danh duy nhất toàn cầu (Canonical Alphanumeric ID hoặc `d_...`).
- `body`: Nội dung HTML chuẩn của tab tài liệu hiện hành.
- `tabs`: Mảng JSON chứa danh sách các tab con (`[{ id, title, content, wordCount, target }]`).
- `rev`: Số hiệu phiên bản đơn điệu tăng (Monotonic Revision), bắt đầu từ 1. Mỗi lần cập nhật sẽ tăng `rev = max_user_rev + 1`.
- `is_deleted` & `deleted_at`: Đánh dấu xóa mềm (Soft-delete) chống phục sinh rác khi client offline kết nối lại.

---

## 3. Giao Thức Đồng Bộ Dữ Liệu (Sync Protocol API)

### 3.1. Unified Sync Batch (`POST /api/sync/doc`)

Client gửi các thay đổi cục bộ (Push) và nhận về các thay đổi mới nhất từ server (Pull) trong cùng 1 request nguyên tử (Single-trip atomic sync).

#### Header Yêu Cầu:
```http
Authorization: Bearer <JWT_TOKEN> (hoặc Session Cookie / X-User-Id)
Content-Type: application/json
```

#### Request Body Payload (`DocKeepSyncRequest`):
```json
{
  "sync_batch_id": "batch_1728219000_abc123",
  "since_rev": 12,
  "items": [
    {
      "id": "977ViYdH",
      "title": "Báo cáo Q4",
      "body": "<p>Nội dung tài liệu...</p>",
      "preview_text": "Nội dung tài liệu...",
      "word_count": 120,
      "pinned": false,
      "in_trash": false,
      "target": 1000,
      "tabs": [
        { "id": "tab-1", "title": "Tab 1", "content": "<p>...</p>", "wordCount": 120 }
      ],
      "active_tab_id": "tab-1",
      "base_rev": 12,
      "rev": 13,
      "is_deleted": false,
      "updated_at": 1728219000000
    }
  ]
}
```

#### Response Body:
```json
{
  "status": "OK",
  "sync_batch_id": "batch_1728219000_abc123",
  "latest_rev": 14,
  "items": [
    {
      "id": "977ViYdH",
      "title": "Báo cáo Q4",
      "body": "<p>Nội dung tài liệu...</p>",
      "tabs": [...],
      "rev": 14,
      "is_deleted": false,
      "updated_at": 1728219000000
    }
  ],
  "server_time": 1728219001000
}
```
