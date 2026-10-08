# Spac2 Sync & Resource Architecture Contract

Tài liệu này đặc tả toàn diện kiến trúc đồng bộ dữ liệu (Sync Engine), cơ chế
định danh tài nguyên (Resource URLs & Hash Routes), phòng chống phục sinh dữ liệu đã xóa (Anti-Resurrection
Guard), và quản lý tương thích ngược ID cũ (Legacy ID Aliasing) trên toàn bộ hệ
sinh thái Spac2 (`doc`, `task`, `mindmap`, `table`, `note`, `calendar`,
`slides`).

---

## 1. Các Nguyên Tắc Kiến Trúc Cốt Lõi (Core Principles)

1. **URL Định Danh Toàn Cục (Resource Identity)**: Mọi tài nguyên thuộc hệ sinh
   thái Spac2 sở hữu một Resource ID duy nhất (độ dài 8 ký tự alphanumeric, ví
   dụ: `977ViYdH`).
2. **SPA Hash Route Standard**: URL mở tài nguyên sử dụng định dạng Hash
   Route `https://spac2.com/app/{app}/#{APP}/{resource_id}` (ví dụ:
   `https://spac2.com/app/doc/#DOC/977ViYdH`,
   `https://spac2.com/app/note/#NOTE/977ViYdH`) giúp tương thích 100% với Static
   Hosting/Cloudflare Pages không bao giờ sinh lỗi 404 máy chủ.
3. **Private & Bảo Mật Tuyệt Đối (Private by Default)**: Tài nguyên của người dùng chỉ duy nhất chủ sở hữu (`user_id`) truy cập được.
4. **Phân Quyền Tại Origin Backend (Origin Authorization)**: Quyền hạn được kiểm tra trực tiếp tại máy chủ theo token xác thực. Bất
   kỳ request nào không hợp lệ đều nhận phản hồi `404 Not Found` (Masking - chống dò quét resource).
5. **Tuyệt Đối Không Index Tài Nguyên Cá Nhân (Strict No-Index)**: Toàn bộ API
   tài nguyên người dùng luôn mang header
   `X-Robots-Tag: noindex, nofollow, noarchive, nosnippet` và không bao giờ xuất
   hiện trong Google Search hoặc Sitemap.

---

## 2. Chống Phục Sinh Dữ Liệu & Kiểm Soát Xung Đột (OCC & Anti-Resurrection Guard)

1. **Anti-Resurrection Guard**: Mọi câu lệnh Upsert tài liệu cá nhân bắt buộc
   kiểm tra `WHERE is_deleted = FALSE` để ngăn chặn thiết bị offline lâu ngày
   phục sinh rác.
2. **Strict Optimistic Concurrency Control (OCC) & Explicit Status Protocol**:
   - Khi client gửi mutation mang `base_rev`:
   - Nếu `base_rev < server_rev` (xung đột đồng thời giữa nhiều thiết bị):
     - Server **tuyệt đối KHÔNG âm thầm ghi đè dữ liệu** (Không overwrite).
     - Server trả về trạng thái rõ ràng: `status: "CONFLICT"`,
       `error: "OCC_VERSION_MISMATCH"`, `server_rev: server_rev`.
     - Bản cập nhật mới nhất trên server được truyền tự nhiên qua danh sách
       delta pull `items` của cùng response.
   - **Vòng đời xử lý Mutation tại Client (Conflict Lifecycle & Loop
     Prevention)**:
     - **`ACK`**: Xóa mutation tương ứng khỏi Outbox.
     - **`REJECT`**: Đánh dấu mutation là `blocked`, lưu lại cục bộ nhưng ngừng
       retry tự động vô hạn.
     - **`CONFLICT`**: Đưa mutation ra khỏi hàng đợi retry đang hoạt động để
       chống vòng lặp vô hạn (`CONFLICT -> retry -> CONFLICT...`). Lưu trữ cả
       bản nháp cục bộ và bản snapshot server vào `history`, rebase `rev` cục bộ
       lên `server_rev` để các chỉnh sửa tiếp theo của người dùng được tạo với
       `base_rev` mới hợp lệ.

---

## 3. Các Endpoint API Thống Nhất (Single Unified Sync Pipeline)

- **`POST /api/sync/{app_code}`** (**Single Unified Sync Pipeline duy nhất** - Single-trip atomic push + pull, Monotonic Rev, OCC
  Conflict Archiving, Idempotency SHA-256)

---

## 4. Danh Mục Tài Liệu Chi Tiết Từng Ứng Dụng (App-Specific Sync Guides)

Để xem chi tiết mô hình dữ liệu (Database Schema), payload JSON, luồng giao diện (UI/UX) và các lưu ý đặc thù của từng ứng dụng, tham khảo các tài liệu sau:

| Ứng Dụng | File Tài Liệu | Mô Tả & Tính Năng Đặc Thù |
| :--- | :--- | :--- |
| **Spac2 Doc** | [`doc.md`](file:///d:/website/spac2/api/doc.md) | Tài liệu Rich-Text nhiều tab, bộ đếm từ, quản lý mục tiêu số từ |
| **Spac2 Note** | [`note.md`](file:///d:/website/spac2/api/note.md) | Ghi chú nhanh dạng thẻ pastel, bookmark, checklist |
| **Spac2 Task** | [`task.md`](file:///d:/website/spac2/api/task.md) | Quản lý dự án (Projects), công việc (Tasks), Todo con (Subtasks), lọc độ ưu tiên & ngày hết hạn |
| **Spac2 Table** | [`table.md`](file:///d:/website/spac2/api/table.md) | Bảng tính đa trang (Multi-sheet), công thức tính, hòa nhập cấp ô (Cell-level 3-Level Merging) |
| **Spac2 Mindmap** | [`mindmap.md`](file:///d:/website/spac2/api/mindmap.md) | Sơ đồ tư duy, cấu trúc cây đồ thị (Nodes & Edges), tọa độ Canvas Transform |
| **Spac2 Calendar** | [`calendar.md`](file:///d:/website/spac2/api/calendar.md) | Lịch biểu, khung giờ, sự kiện định kỳ (Recurrence), địa điểm, chuẩn hóa alias |
| **Spac2 Countday** | [`countday.md`](file:///d:/website/spac2/api/countday.md) | Đếm ngược ngày quan trọng, sự kiện kỷ niệm, sắp xếp thứ tự kéo thả (Order Index) |
