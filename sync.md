# Spac2 Sync & Resource Architecture Contract (Hash Route Standard)

Tài liệu này đặc tả toàn diện kiến trúc đồng bộ dữ liệu (Sync Engine), cơ chế định danh tài nguyên (Resource URLs & Hash Routes), ma trận phân quyền (Authorization Matrix), phòng chống phục sinh dữ liệu đã xóa (Anti-Resurrection Guard), và quản lý tương thích ngược ID cũ (Legacy ID Aliasing) trên toàn bộ hệ sinh thái Spac2 (`doc`, `task`, `mindmap`, `table`, `note`, `calendar`, `slides`).

---

## 1. 5 Điều Răn Kiến Trúc (5 Core Commandments)

1. **URL Định Danh Toàn Cục (Resource Identity)**: Mọi tài nguyên thuộc hệ sinh thái Spac2 sở hữu một Resource ID duy nhất (độ dài 8 ký tự alphanumeric, ví dụ: `977ViYdH`).
2. **SPA Hash Route Standard**: URL chia sẻ tài nguyên sử dụng định dạng Hash Route `https://spac2.com/app/{app}/#{APP}/{resource_id}` (ví dụ: `https://spac2.com/app/doc/#DOC/977ViYdH`, `https://spac2.com/app/note/#NOTE/977ViYdH`) giúp tương thích 100% với Static Hosting/Cloudflare Pages không bao giờ sinh lỗi 404 máy chủ.
3. **Private Mặc Định (Private by Default)**: Tài nguyên mới tạo luôn có `visibility = 'private'`, chỉ duy nhất chủ sở hữu (`user_id`) truy cập được.
4. **Phân Quyền Tại Origin Backend (Origin Authorization)**: Trạng thái quyền hạn (`visibility`, `ACL`, `Owner`) được kiểm tra trực tiếp tại máy chủ. Bất kỳ request nào không hợp lệ đều nhận phản hồi `404 Not Found` (Masking - chống dò quét resource).
5. **Chia Sẻ Không Thay Đổi URL (Immutable URLs)**: Bật/tắt chế độ chia sẻ chỉ thay đổi cờ trạng thái `visibility` (`private` <-> `link_read`), không bao giờ sinh URL phụ hoặc làm thay đổi đường dẫn tài nguyên.
6. **Tuyệt Đối Không Index Tài Nguyên Cá Nhân (Strict No-Index)**: Toàn bộ API tài nguyên người dùng luôn mang header `X-Robots-Tag: noindex, nofollow, noarchive, nosnippet` và không bao giờ xuất hiện trong Google Search hoặc Sitemap.

---

## 2. Phân Tách Bề Mặt URL (Dual-Surface Architecture)

```
                            https://spac2.com/app/doc/
                                      │
                 ┌────────────────────┴────────────────────┐
                 ▼                                         ▼
      [App Landing Page]                         [User Resource Hash Link]
      URL: /app/doc/                             URL: /app/doc/#DOC/977ViYdH
      HTTP Request: GET /app/doc/                HTTP Request: GET /app/doc/ (Hash stays on client)
      Robots: index, follow                      API Resource: GET /api/sync/resource/doc/977ViYdH
      Cache: Public CDN Cache                    Headers: X-Robots-Tag: noindex, private, no-store
      Mục đích: Giới thiệu app, SEO Google       Mục đích: Trực tiếp mở tài liệu làm việc
```

---

## 3. Ma Trận Phân Quyền & Che Giấu Thông Tin (Authorization Matrix)

| Trạng thái người gọi | Visibility | Phương thức | HTTP Status | Quyền thực tế |
| :--- | :--- | :--- | :--- | :--- |
| Khách chưa đăng nhập (Anonymous) | `private` | GET / POST / PATCH | **`404 Not Found`** | Bị từ chối (Ẩn danh tài nguyên) |
| Khách chưa đăng nhập (Anonymous) | `link_read` | GET (Read) | **`200 OK`** | Chỉ xem (Read-only) |
| Khách chưa đăng nhập (Anonymous) | `link_read` | POST / PATCH (Write) | **`401 Unauthorized`**| Bắt buộc đăng nhập để ghi |
| Người dùng khác (Authenticated) | `private` (Không ACL) | GET / Write | **`404 Not Found`** | Bị từ chối (Ẩn danh tài nguyên) |
| Người dùng khác có ACL `read` | Bất kỳ | GET (Read) | **`200 OK`** | Xem theo phân quyền |
| Người dùng khác có ACL `read` | Bất kỳ | POST / PATCH (Write) | **`403 Forbidden`** | Không có quyền ghi |
| Người dùng khác có ACL `write`| Bất kỳ | GET / Write | **`200 OK`** | Toàn quyền chỉnh sửa |
| Chủ sở hữu (Owner) | Bất kỳ | GET / Write / Share | **`200 OK`** | Toàn quyền tài nguyên |
| Bất kỳ ai | Đã xóa (`deleted_at`) | Bất kỳ | **`404 Not Found`** | Tài nguyên đã bị hủy |

---

## 4. Chống Phục Sinh Dữ Liệu Đã Xóa (Anti-Resurrection Guard)

Tất cả các câu lệnh Upsert (trên PostgreSQL & SQLite) bắt buộc phải có mệnh đề kiểm tra trạng thái xóa:

```sql
INSERT INTO user_sync_docs (user_id, doc_id, title, content_delta, content_html, plain_text, updated_at_ms, client_updated_at, is_deleted, deleted_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (doc_id) DO UPDATE SET
    title = EXCLUDED.title,
    content_delta = EXCLUDED.content_delta,
    content_html = EXCLUDED.content_html,
    plain_text = EXCLUDED.plain_text,
    updated_at_ms = EXCLUDED.updated_at_ms,
    client_updated_at = EXCLUDED.client_updated_at,
    is_deleted = EXCLUDED.is_deleted,
    deleted_at = EXCLUDED.deleted_at
WHERE user_sync_docs.user_id = EXCLUDED.user_id
  AND user_sync_docs.is_deleted = FALSE;
```

---

## 5. Các Endpoint API Thống Nhất

- **`GET /api/sync/resource/{app_code}/{resource_id}`** (Headers: `noindex`, `no-cache`)
- **`PATCH /api/sync/resource/{app_code}/{resource_id}/visibility`** (Chỉ Owner/Admin có quyền)
- **`POST /api/sync/{app_code}/pull`** (Revision-based delta sync)
- **`POST /api/sync/{app_code}/push`** (Idempotent mutation push)
