# Spac2 Mindmap Synchronization & Sharing Architecture Specification (`/api/sync/mindmap`)

Tài liệu này đặc tả chi tiết kiến trúc đồng bộ sơ đồ tư duy (Nodes, Edges,
Hierarchy, Canvas Transform), bộ lưu trữ đồ thị (Graph Storage), cơ chế phân
quyền chia sẻ dự án (Project Sharing & Visibility) và giao thức đồng bộ phiên
bản dành cho **Spac2 Mindmap** (`/app/mindmap/`).

---

## 1. Tổng Quan Kiến Trúc (Architecture Overview)

- **Domain**: Interactive Mind Mapping, Brainstorming & Infinite Canvas Graph.
- **Client Codebase**: `/app/mindmap/` (`index.html`, `js/main.js`,
  `js/sync.js`, `css/styles.css`).
- **Backend Router**: `/api/sync/mindmap.py` (`FastAPI`).
- **Resource Router**: `/api/sync/resource.py` (`FastAPI`).
- **Database Table**: `user_sync_mindmap_projects` (PostgreSQL).
- **Mô hình đồng bộ**: Monotonic Revision Sync, Graph Snapshot & Delta Patching,
  Optimistic Concurrency Control (OCC).
- **Cơ chế chia sẻ**: Hash Route Standard (`#MINDMAP/{project_id}`), Origin
  Authorization, Strict No-Index.

---

## 2. Mô Hình Cơ Sở Dữ Liệu (Database Schema)

```sql
CREATE TABLE IF NOT EXISTS user_sync_mindmap_projects (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    project_id VARCHAR(100) NOT NULL,
    name TEXT DEFAULT '',
    data JSONB DEFAULT '{"nodes": [], "edges": [], "transform": {"x": 0, "y": 0, "scale": 1}}',
    created_at_str TEXT DEFAULT '',
    rev BIGINT NOT NULL DEFAULT 1,
    visibility VARCHAR(20) NOT NULL DEFAULT 'private',
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at BIGINT DEFAULT 0,
    CONSTRAINT uq_sync_mindmap_project_id UNIQUE (project_id)
);

-- Indices
CREATE INDEX IF NOT EXISTS idx_sync_mindmap_user_rev ON user_sync_mindmap_projects(user_id, rev);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_mindmap_global_project_id ON user_sync_mindmap_projects(project_id);
```

### Cấu Trúc Dữ Liệu `data` (JSONB):

```json
{
  "transform": { "x": 100, "y": 200, "scale": 1.0 },
  "nodes": [
    {
      "id": "root",
      "text": "Kế Hoạch Khởi Nghiệp",
      "x": 0,
      "y": 0,
      "color": "#3b82f6",
      "collapsed": false,
      "isRoot": true
    },
    {
      "id": "node_1",
      "parentId": "root",
      "text": "Nghiên cứu thị trường",
      "x": 250,
      "y": -100,
      "color": "#10b981",
      "collapsed": false
    }
  ],
  "edges": [
    {
      "id": "edge_root_node_1",
      "from": "root",
      "to": "node_1",
      "color": "#94a3b8"
    }
  ]
}
```

---

## 3. Cơ Chế Chia Sẻ Dự Án (Project Sharing & Security)

### 3.1. Hash Route Standard (`#MINDMAP/{project_id}`)

- URL chia sẻ: `https://spac2.com/app/mindmap/#MINDMAP/{project_id}`
- **Zero 404s**: Hash không gửi lên web server / CDN, tương thích 100% Static
  Hosting.
- **Né Index (Strict No-Index)**: Web crawlers không index hash fragment. Mọi
  API endpoint trả dữ liệu người dùng đều có header:
  ```http
  X-Robots-Tag: noindex, nofollow, noarchive, nosnippet
  Cache-Control: private, no-store, no-cache, must-revalidate
  ```

### 3.2. Ma Trận Quyền Hạn (Authorization Matrix)

- `visibility = 'private'`: Chỉ chủ sở hữu (`user_id`) truy cập được. Người khác
  truy cập nhận `404 Not Found` (Masking).
- `visibility = 'link_read'`: Bất kỳ ai có link đều mở xem được ở chế độ
  **View-Only** (`role = 'viewer'`), có nút **Nhân bản (Clone to My Mindmaps)**.
- Chỉ chủ sở hữu (`role = 'owner'`) mới có quyền chỉnh sửa đồ thị hoặc bật/tắt
  quyền chia sẻ.

### 3.3. Các Endpoint Chia Sẻ

1. **Lấy thông tin tài nguyên**: `GET /api/sync/resource/mindmap/{project_id}`
   - Trả về `role` (`owner` hoặc `viewer`), `visibility`, và `data` đồ thị.
2. **Cập nhật trạng thái chia sẻ**:
   `PATCH /api/sync/resource/mindmap/{project_id}/visibility`
   - Payload: `{"visibility": "link_read" | "private"}`
   - Chỉ Owner có quyền thực hiện.

---

## 4. Giao Thức Đồng Bộ Dữ Liệu (Sync Protocol API)

### 4.1. Unified Sync Batch (`POST /api/sync/mindmap`)

#### Request Body Payload (`MindmapKeepSyncRequest`):

```json
{
  "sync_batch_id": "batch_mindmap_1728219000_map",
  "since_rev": 1,
  "items": [
    {
      "id": "mm_proj_001",
      "name": "Kế Hoạch Khởi Nghiệp",
      "data": {
        "transform": { "x": 0, "y": 0, "scale": 1 },
        "nodes": [ ... ],
        "edges": [ ... ]
      },
      "rev": 1,
      "base_rev": 1,
      "visibility": "private",
      "updatedAt": 1728219000000,
      "is_deleted": false
    }
  ]
}
```

#### Response Body:

```json
{
  "status": "success",
  "current_rev": 2,
  "deduplicated": false,
  "synced_count": 1,
  "results": [
    { "id": "mm_proj_001", "status": "ACK", "rev": 2 }
  ],
  "projects": [
    {
      "id": "mm_proj_001",
      "name": "Kế Hoạch Khởi Nghiệp",
      "data": { ... },
      "rev": 2,
      "visibility": "private",
      "is_deleted": false,
      "updatedAt": 1728219000000
    }
  ]
}
```

---

## 5. Các Điểm Cần Lưu Ý Cho Developer (Dev Notes)

1. **Transform Coordinates**: Tọa độ `(x, y)` của các node là tọa độ logic tương
   đối so với gốc canvas (World coordinates). Khi pan hoặc zoom, chỉ trường
   `transform: {x, y, scale}` thay đổi.
2. **Atomic Graph Structure**: Toàn bộ mảng `nodes` và `edges` được lưu cùng
   nhau trong 1 document snapshot để đảm bảo tính toàn vẹn của cây đồ thị (không
   bao giờ xảy ra tình trạng node con trỏ tới parent không tồn tại).
3. **Strict Optimistic Concurrency Control (OCC)**: Tránh ghi đè ngầm nếu
   `base_rev < server_rev`, trả về trạng thái `CONFLICT`
   (`OCC_VERSION_MISMATCH`).
4. **Soft Delete Tombstones & Auto Purge**: Xóa dự án lưu vết tombstone với
   `is_deleted = TRUE` và tự động dọn dẹp sau 30 ngày.
5. **WebSocket Invalidation**: Sau khi lưu thành công, hệ thống gửi signal
   `resource_changed` để các thiết bị khác đang mở cùng project cập nhật theo
   thời gian thực.
