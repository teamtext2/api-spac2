# Spac2 Calendar Synchronization & Architecture Specification (`/api/sync/calendar`)

Tài liệu này đặc tả chi tiết kiến trúc đồng bộ lịch biểu (Calendar Events, Timeslots, Recurrence, Location, Tags) dành cho **Spac2 Calendar** (`/app/calendar/`).

---

## 1. Tổng Quan Kiến Trúc (Architecture Overview)

- **Domain**: Calendar, Schedule, Daily Planner & Recurring Events.
- **Client Codebase**: `/app/calendar/` (`index.html`, `js/main.js`, `js/sync.js`, `css/styles.css`).
- **Backend Router**: `/api/sync/calendar.py` (`FastAPI`).
- **Database Table**: `user_sync_calendar_events` (PostgreSQL).
- **Mô hình đồng bộ**: Monotonic Revision Sync, Field Alias Normalization (`t` -> `title`, `c` -> `color`, `start` -> `startTime`).

---

## 2. Mô Hình Cơ Sở Dữ Liệu (Database Schema)

```sql
CREATE TABLE IF NOT EXISTS user_sync_calendar_events (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    event_id VARCHAR(100) NOT NULL,
    date TEXT DEFAULT '',
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    start_time TEXT DEFAULT '',
    end_time TEXT DEFAULT '',
    color JSONB DEFAULT '{"bg": "#2978FF", "text": "#FFFFFF", "name": "Blue"}',
    is_all_day BOOLEAN DEFAULT FALSE,
    location TEXT DEFAULT '',
    recurrence TEXT DEFAULT '',
    rev BIGINT NOT NULL DEFAULT 1,
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at BIGINT DEFAULT 0,
    CONSTRAINT uq_sync_calendar_event_id UNIQUE (user_id, event_id)
);

-- Indices
CREATE INDEX IF NOT EXISTS idx_sync_calendar_user_rev ON user_sync_calendar_events(user_id, rev);
CREATE INDEX IF NOT EXISTS idx_sync_calendar_user_date ON user_sync_calendar_events(user_id, date);
```

---

## 3. Giao Thức Đồng Bộ Dữ Liệu (Sync Protocol API)

### 3.1. Unified Sync Batch (`POST /api/sync/calendar`)

#### Request Body Payload (`CalendarKeepSyncRequest`):
```json
{
  "sync_batch_id": "batch_cal_1728219000_ev",
  "since_rev": 4,
  "items": [
    {
      "id": "cal_ev_01",
      "date": "2026-10-10",
      "title": "Họp Ban Giám Đốc",
      "description": "Thảo luận mục tiêu Q4",
      "startTime": "09:00",
      "endTime": "10:30",
      "color": { "bg": "#EF4444", "text": "#FFFFFF", "name": "Red" },
      "isAllDay": false,
      "location": "Phòng Họp 1 / Google Meet",
      "recurrence": "weekly",
      "is_deleted": false
    }
  ]
}
```

#### Response Body:
```json
{
  "status": "success",
  "sync_batch_id": "batch_cal_1728219000_ev",
  "current_rev": 5,
  "items": [
    {
      "id": "cal_ev_01",
      "date": "2026-10-10",
      "title": "Họp Ban Giám Đốc",
      "description": "Thảo luận mục tiêu Q4",
      "startTime": "09:00",
      "endTime": "10:30",
      "color": { "bg": "#EF4444", "text": "#FFFFFF", "name": "Red" },
      "isAllDay": false,
      "location": "Phòng Họp 1 / Google Meet",
      "recurrence": "weekly",
      "rev": 5,
      "is_deleted": false
    }
  ],
  "server_time": 1728219001000
}
```

---

## 4. Các Tính Năng & Tương Thích Ngược (Compatibility & Aliasing)

1. **Short Aliases Support**: Hỗ trợ đầy đủ các phím viết tắt từ client cũ (`t` cho `title`, `desc` cho `description`, `start` / `end` cho `startTime` / `endTime`, `c` cho `color`).
2. **Anti-Duplication Signature**: Tự động lọc trùng lặp khi nhiều request gửi sự kiện cùng ngày, giờ và tiêu đề: `f"{date}|||{start_time}|||{title.lower()}|||{desc.lower()}"`.
