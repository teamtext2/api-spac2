# Spac2 Mindmap Synchronization & Architecture Specification (`/api/sync/mindmap`)

Tài liệu này đặc tả chi tiết kiến trúc đồng bộ sơ đồ tư duy (Nodes, Edges, Hierarchy, Canvas Transform), bộ lưu trữ đồ thị (Graph Storage) và giao thức đồng bộ phiên bản dành cho **Spac2 Mindmap** (`/app/mindmap/`).

---

## 1. Tổng Quan Kiến Trúc (Architecture Overview)

- **Domain**: Interactive Mind Mapping, Brainstorming & Infinite Canvas Graph.
- **Client Codebase**: `/app/mindmap/` (`index.html`, `js/main.js`, `js/sync.js`, `css/styles.css`).
- **Backend Router**: `/api/sync/mindmap.py` (`FastAPI`).
- **Database Table**: `user_sync_mindmap_projects` (PostgreSQL).
- **Mô hình đồng bộ**: Monotonic Revision Sync, Graph Snapshot & Delta Patching.

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
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at BIGINT DEFAULT 0,
    CONSTRAINT uq_sync_mindmap_proj_id UNIQUE (user_id, project_id)
);

-- Indices
CREATE INDEX IF NOT EXISTS idx_sync_mindmap_user_rev ON user_sync_mindmap_projects(user_id, rev);
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

## 3. Giao Thức Đồng Bộ Dữ Liệu (Sync Protocol API)

### 3.1. Unified Sync Batch (`POST /api/sync/mindmap`)

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
  "sync_batch_id": "batch_mindmap_1728219000_map",
  "current_rev": 2,
  "projects": [
    {
      "id": "mm_proj_001",
      "name": "Kế Hoạch Khởi Nghiệp",
      "data": { ... },
      "rev": 2,
      "is_deleted": false,
      "updatedAt": 1728219000000
    }
  ],
  "server_time": 1728219001000
}
```

---

## 4. Các Điểm Cần Lưu Ý Cho Developer (Dev Notes)

1. **Transform Coordinates**: Tọa độ `(x, y)` của các node là tọa độ logic tương đối so với gốc canvas (World coordinates). Khi pan hoặc zoom, chỉ trường `transform: {x, y, scale}` thay đổi.
2. **Atomic Graph Structure**: Toàn bộ mảng `nodes` và `edges` được lưu cùng nhau trong 1 document snapshot để đảm bảo tính toàn vẹn của cây đồ thị (không bao giờ xảy ra tình trạng node con trỏ tới parent không tồn tại).
3. **Graph Rendering Optimization**: Phía client sử dụng Canvas / SVG rendering với dirty-rect checking để chỉ render lại các node khi người dùng kéo thả hoặc chỉnh sửa chữ.
