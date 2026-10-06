# Spac2 Task Synchronization & Architecture Specification (`/api/sync/task`)

Tài liệu này đặc tả chi tiết kiến trúc đồng bộ dữ liệu hai cấp (Projects & Tasks / Subtasks), bộ lọc thời gian, trạng thái hoàn thành và cơ chế đồng bộ danh mục công việc dành cho **Spac2 Task** (`/app/task/`).

---

## 1. Tổng Quan Kiến Trúc (Architecture Overview)

- **Domain**: Task Management, Project Groups & Todo Lists.
- **Client Codebase**: `/app/task/` (`index.html`, `js/main.js`, `js/sync.js`, `css/styles.css`).
- **Backend Router**: `/api/sync/task.py` (`FastAPI`).
- **Database Tables**:
  1. `user_sync_task_projects` (Danh mục / Project danh sách).
  2. `user_sync_tasks` (Nhiệm vụ cụ thể, Todo, Subtasks).
- **Mô hình đồng bộ**: Two-entity Monotonic Revision Sync (Projects & Tasks trong cùng 1 Batch Delta).

---

## 2. Mô Hình Cơ Sở Dữ Liệu (Database Schema)

```sql
-- 1. Table Projects
CREATE TABLE IF NOT EXISTS user_sync_task_projects (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    project_id VARCHAR(100) NOT NULL,
    name TEXT DEFAULT '',
    color JSONB DEFAULT '{"bg": "#3B82F6", "text": "#FFFFFF", "name": "Blue"}',
    created_at_str TEXT DEFAULT '',
    rev BIGINT NOT NULL DEFAULT 1,
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at BIGINT DEFAULT 0,
    CONSTRAINT uq_sync_projects_proj_id UNIQUE (user_id, project_id)
);

-- 2. Table Tasks
CREATE TABLE IF NOT EXISTS user_sync_tasks (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    task_id VARCHAR(100) NOT NULL,
    project_id VARCHAR(100) DEFAULT '',
    title TEXT DEFAULT '',
    note TEXT DEFAULT '',
    priority VARCHAR(20) DEFAULT 'normal',
    due_date TEXT DEFAULT '',
    completed BOOLEAN DEFAULT FALSE,
    completed_at TEXT DEFAULT NULL,
    subtasks JSONB DEFAULT '[]',
    date TEXT DEFAULT '',
    rev BIGINT NOT NULL DEFAULT 1,
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at BIGINT DEFAULT 0,
    CONSTRAINT uq_sync_tasks_task_id UNIQUE (user_id, task_id)
);

-- Indices
CREATE INDEX IF NOT EXISTS idx_sync_projects_user_rev ON user_sync_task_projects(user_id, rev);
CREATE INDEX IF NOT EXISTS idx_sync_tasks_user_rev ON user_sync_tasks(user_id, rev);
```

---

## 3. Giao Thức Đồng Bộ Dữ Liệu (Sync Protocol API)

### 3.1. Unified Sync Batch (`POST /api/sync/task`)

Đồng bộ đồng thời cả danh sách Project và các Task con trong 1 lần gọi.

#### Request Body Payload (`TaskKeepSyncRequest`):
```json
{
  "sync_batch_id": "batch_task_1728219000_123",
  "since_rev": 10,
  "projects": [
    {
      "id": "proj_work",
      "name": "Công Việc Q4",
      "color": { "bg": "#3B82F6", "text": "#FFFFFF", "name": "Blue" },
      "createdAt": "2026-10-06T12:00:00Z",
      "is_deleted": false
    }
  ],
  "tasks": [
    {
      "id": "task_abc_01",
      "projectId": "proj_work",
      "title": "Hoàn thiện tài liệu Sync API",
      "note": "Viết file md cho các app",
      "priority": "high",
      "dueDate": "2026-10-07",
      "completed": false,
      "subtasks": [
        { "id": "sub_1", "title": "Viết doc.md", "completed": true },
        { "id": "sub_2", "title": "Viết task.md", "completed": false }
      ],
      "createdAt": "2026-10-06T12:10:00Z",
      "is_deleted": false
    }
  ]
}
```

#### Response Body:
```json
{
  "status": "success",
  "sync_batch_id": "batch_task_1728219000_123",
  "latest_rev": 12,
  "projects": [ ... ],
  "tasks": [ ... ],
  "server_time": 1728219001000
}
```

---

## 4. Các Tính Năng Đặc Thù & Lưu Ý Khi Dev (Key Behaviors)

1. **Cascade Project Deletion**: Khi một project bị đánh dấu `is_deleted = true`, các task thuộc project đó cũng sẽ được ẩn tương ứng trên giao diện.
2. **Subtask Structure**: `subtasks` được lưu trực tiếp dưới dạng JSONB array trong dòng của task, giúp truy vấn nhanh chóng không cần join bảng phụ.
3. **Anti-Duplication Guard**: Backend tự động kiểm tra `(project_id, title, created_at)` để khử trùng lặp khi mạng bị giật gửi trùng mutation.
4. **Offline First**: Frontend lưu toàn bộ danh sách `projects` và `tasks` trong `localStorage`, gắn cờ `isDirty` và tự động đồng bộ khi có kết nối mạng (`navigator.onLine`).
