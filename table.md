# Spac2 Table Synchronization & Architecture Specification (`/api/sync/table`)

Tài liệu này đặc tả chi tiết kiến trúc đồng bộ dữ liệu bảng tính đa trang (Multi-sheet Workbook), cơ chế Cell-level Delta Patching, giải thuật hòa nhập 3 cấp (3-Level Merging), và kiểm soát xung đột dữ liệu cho **Spac2 Table** (`/app/table/`).

---

## 1. Tổng Quan Kiến Trúc (Architecture Overview)

- **Domain**: Spreadsheet, Multi-sheet Tables, Data Calculations & Formula Grids.
- **Client Codebase**: `/app/table/` (`index.html`, `js/main.js`, `js/sync.js`, `css/styles.css`).
- **Backend Router**: `/api/sync/table.py` (`FastAPI`).
- **Database Table**: `user_sync_table_projects` (PostgreSQL).
- **Mô hình đồng bộ**: 
  - **Full Document Sync**: Đồng bộ toàn bộ workbook (`sheets`, `colWidths`, `styles`, `merges`).
  - **Cell-Level Differential Merging**: Tự động hòa nhập các ô chỉnh sửa khác nhau giữa 2 thiết bị mà không ghi đè mất dữ liệu.

---

## 2. Mô Hình Cơ Sở Dữ Liệu (Database Schema)

```sql
CREATE TABLE IF NOT EXISTS user_sync_table_projects (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    project_id VARCHAR(100) NOT NULL,
    name TEXT DEFAULT '',
    data JSONB DEFAULT '{"sheets": [{"name": "Sheet 1", "data": [[""]], "colWidths": [], "rowHeights": [], "styles": {}, "merges": []}], "activeSheetIndex": 0}',
    last_edited BIGINT DEFAULT 0,
    rev BIGINT NOT NULL DEFAULT 1,
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at BIGINT DEFAULT 0,
    CONSTRAINT uq_sync_table_proj_id UNIQUE (user_id, project_id)
);

-- Indices
CREATE INDEX IF NOT EXISTS idx_sync_table_user_rev ON user_sync_table_projects(user_id, rev);
```

### Cấu Trúc Dữ Liệu `data` (JSONB):
```json
{
  "activeSheetIndex": 0,
  "sheets": [
    {
      "id": "sheet_1",
      "name": "Doanh Thu",
      "data": [
        ["Tháng", "Doanh Thu", "Chi Phí"],
        ["T1", 1000, 400],
        ["T2", 1500, 500]
      ],
      "colWidths": [120, 150, 150],
      "rowHeights": [32, 28, 28],
      "styles": {
        "0-0": { "bold": true, "bg": "#f1f5f9" },
        "0-1": { "bold": true, "bg": "#f1f5f9" }
      },
      "merges": []
    }
  ]
}
```

---

## 3. Giao Thức Đồng Bộ Dữ Liệu (Sync Protocol API)

### 3.1. Unified Sync Batch (`POST /api/sync/table`)

#### Request Body Payload (`TableKeepSyncRequest`):
```json
{
  "sync_batch_id": "batch_table_1728219000_tbl",
  "since_rev": 3,
  "items": [
    {
      "id": "tbl_fin_2026",
      "name": "Báo cáo tài chính",
      "data": { ... },
      "patches": [
        { "sheet": 0, "r": 1, "c": 1, "v": 1200, "ts": 1728219000000 }
      ],
      "lastEdited": 1728219000000,
      "is_deleted": false
    }
  ]
}
```

#### Response Body:
```json
{
  "status": "success",
  "sync_batch_id": "batch_table_1728219000_tbl",
  "current_rev": 4,
  "projects": [
    {
      "id": "tbl_fin_2026",
      "name": "Báo cáo tài chính",
      "data": { ... },
      "lastEdited": 1728219000000,
      "rev": 4,
      "is_deleted": false
    }
  ],
  "server_time": 1728219001000
}
```

---

## 4. Giải Thuật Hòa Nhập 3 Cấp (Differential 3-Level Merging)

Khi hai thiết bị cùng chỉnh sửa một bảng tính khi ngoại tuyến và đồng bộ lại:
1. **Cấp 1 - Metadata Level**: Giữ tên bảng tính và thời điểm cập nhật mới nhất (`lastEdited`).
2. **Cấp 2 - Sheet Level**: Ghép nối các sheet theo `sheet.id` hoặc `sheet.name`. Sheet mới tạo ở client sẽ được bổ sung vào workbook.
3. **Cấp 3 - Cell Level Grid**: 
   - Duyệt qua ma trận ô `(r, c)`.
   - Nếu ô trên client có giá trị mới thay đổi so với bản snapshot gốc, áp dụng giá trị đó.
   - Giữ nguyên các ô mà thiết bị khác vừa cập nhật nếu không bị đụng hàng tại đúng tọa độ `(r, c)`.
