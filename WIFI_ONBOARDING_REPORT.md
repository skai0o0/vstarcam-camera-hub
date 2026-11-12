# Báo Cáo Kỹ Thuật: Vấn Đề Nạp Wi-Fi & Onboarding Camera VStarcam C22S

*Ngày lập báo cáo: 12/11/2026*  
*Mục tiêu: Đánh giá thực nghiệm quy trình nạp Wi-Fi (Wi-Fi Provisioning) và liên kết P2P qua Internet cho dòng camera VStarcam C22S (Không có cổng mạng dây RJ45).*

---

## 1. Tổng Quan Vấn Đề (Executive Summary)

* **Thiết bị:** Camera VStarcam C22S (Firmware nhánh `48.82.230.19`, phần cứng sản xuất khoảng 2019, phiên bản bo mạch thuần Wi-Fi không trang bị cổng RJ45).
* **Môi trường mạng:** Router Wi-Fi 6 ZTE ZXHN E2633 (China Mobile), phát 2 băng tần riêng biệt: 2.4GHz (`CMCC-5Auu`) và 5GHz (`CMCC-5Auu-5G`).
* **Hiện tượng:**
  * Khâu giải mã mã QR tem đáy camera để lấy UID (`VSTJ-082266-VWCWH`) hoạt động hoàn hảo trên WebUI Camera Hub.
  * Tuy nhiên, khi chuyển sang bước nạp Wi-Fi ở chế độ khui hộp mới (bằng sóng âm Sonic Wave FSK dải tần `6.5 kHz – 10.1 kHz`), camera không thể kết nối vào router Wi-Fi.
  * Trên máy chủ Camera Hub, tiến trình P2P liên tục nhận phản hồi từ cụm máy chủ VStarcam Cloud (`47.74.208.128:32100` / `8.209.76.203:32100` / `47.254.77.97:32100`) với mã trạng thái `status=0xFE` (`WAITING_FOR_WIFI` / Camera Offline).

---

## 2. Các Phương Thức Đã Thực Nghiệm & Kết Quả

| STT | Phương thức thử nghiệm | Cơ chế thực hiện | Kết quả thực tế |
| :---: | :--- | :--- | :--- |
| **1** | **Nhánh 1: Chỉ Liên Kết Server (Link-Only)** | Bỏ qua Wi-Fi, chỉ gửi UID + Mật khẩu lên Server để kích hoạt đục lỗ NAT UDP P2P (`MSG_HELLO` + `MSG_P2P_REQ`). | **Kẹt ở Bước 2**: Do camera mới chưa có thông tin Wi-Fi nên không thể online lên Cloud; Server nhận mã `0xFE` liên tục. |
| **2** | **Mã QR Quang Học (Phone Screen to Lens)** | Sinh mã QR chứa Wi-Fi format Eye4 (`s=...&p=...`) đưa trước ống kính camera cách 10–20 cm. | **Thất bại hoàn toàn**: Thấu kính C22S là loại tiêu cự cố định (Fixed-focus ~4mm), cự ly lấy nét tối thiểu >45cm; màn hình điện thoại bị chói và mờ tịt đối với cảm biến CMOS. |
| **3** | **Sóng âm Chuẩn Full SSID (`encodeSSIDWiFi`)** | Mã hóa toàn bộ tên Wi-Fi `CMCC-5Auu` + Mật khẩu `efub6454` thành chuỗi FSK 19 tần số (thời lượng phát ~15.6 giây). | **Thất bại**: Chuỗi nốt nhạc quá dài; loa điện thoại phát qua Safari bị nén dải động, camera nghe không trọn vẹn gói tin. |
| **4** | **Sóng âm Siêu Tốc MAC Router (`encodeMacWiFi`)** | Cung cấp 4 số cuối MAC in trên tem router (`001e` từ MAC `F4:FC:49:6D:00:1E`) + Mật khẩu `efub6454` (thời lượng phát ~11.7 giây). | **Thất bại**: Camera vẫn không phản hồi câu thoại xác nhận và không gia nhập mạng Wi-Fi. |
| **5** | **Sóng âm Kết Hợp (Combo MAC + SSID)** | Xen kẽ 3 burst MAC + 2 burst Full SSID liên tục. | **Thất bại**: Camera vẫn giữ trạng thái chờ nạp mạng (chu kỳ 2 phút nhắc 1 lần). |
