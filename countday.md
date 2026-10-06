# Spac2 Countday Synchronization & Architecture Specification (`/api/sync/countday`)

Tài liệu này đặc tả chi tiết kiến trúc đồng bộ ngày đếm ngược, ngày kỷ niệm, ngày sinh nhật (Countdown / Anniversaries / Milestones) dành cho **Spac2 Countday** (`/app/countday/`).

---

## 1. Tổng Quan Kiến Trúc (Architecture Overview)

- **Domain**: Day Counter, Countdown to Special Dates, Anniversary Tracker.
- **Client Codebase**: `/app/countday/` (`index.html`, `js/main.js`, `js/sync.js`, `css/styles.css`).
- **Backend Router**: `/api/sync/countday.py` (`FastAPI`).
- **Database Table**: `user_sync_countday_events` (PostgreSQL).
- **Mô hình đồng bộ**: Single-Trip Monotonic Revision Sync, Order Index Sorting.

---

## 2. Mô Hình Cơ Sở Dữ Liệu (Database Schema)

```sql
CREATE TABLE IF NOT EXISTS user_sync_countday_events (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    event_id VARCHAR(100) NOT NULL,
    title TEXT DEFAULT '',
    target_date TEXT DEFAULT '',
    order_index INT DEFAULT 0,
    rev BIGINT NOT NULL DEFAULT 1,
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at BIGINT DEFAULT 0,
    CONSTRAINT uq_sync_countday_event_id UNIQUE (user_id, event_id)
);

-- Indices
CREATE INDEX IF NOT EXISTS idx_sync_countday_user_rev ON user_sync_countday_events(user_id, rev);
CREATE INDEX IF NOT EXISTS idx_sync_countday_user_order ON user_sync_countday_events(user_id, order_index);
```

---

## 3. Giao Thức Đồng Bộ Dữ Liệu (Sync Protocol API)

### 3.1. Unified Sync Batch (`POST /api/sync/countday`)

#### Request Body Payload (`CountdayKeepSyncRequest`):
```json
{
  "sync_batch_id": "batch_cd_1728219000_day",
  "since_rev": 2,
  "items": [
    {
      "id": "cd_tet_2027",
      "title": "Tết Nguyên Đán 2027",
      "date": "2027-02-06",
      "order_index": 0,
      "is_deleted": false
    }
  ]
}
```

#### Response Body:
```json
{
  "status": "success",
  "sync_batch_id": "batch_cd_1728219000_day",
  "current_rev": 3,
  "items": [
    {
      "id": "cd_tet_2027",
      "title": "Tết Nguyên Đán 2027",
      "date": "2027-02-06",
      "target_date": "2027-02-06",
      "order_index": 0,
      "rev": 3,
      "is_deleted": false
    }
  ],
  "server_time": 1728219001000
}
```

---

## 4. Các Điểm Cần Lưu Ý Cho Developer (Dev Notes)

1. **Date Format**: Chuỗi `target_date` sử dụng định dạng ISO chuẩn `YYYY-MM-DD` hoặc `YYYY-MM-DDTHH:mm:ss`.
2. **Reordering**: Trường `order_index` được dùng để lưu thứ tự kéo thả (Drag & Drop) sắp xếp các thẻ đếm ngày trên client.
