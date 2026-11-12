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

---

## 3. Phân Tích Kỹ Thuật & Suy Luận Hiện Tại

### Giả thuyết 1: BSSID (MAC thực tế của sóng Wi-Fi 2.4GHz) bị lệch so với Base MAC trên tem Router
* **Đặc tính phần cứng router ZTE:**
  * Địa chỉ MAC in trên tem mặt sau router (`F4:FC:49:6D:00:1E`) là **Base MAC / WAN MAC** của thiết bị.
  * Trên hầu hết các router viễn thông của ZTE / China Mobile / Huawei, mỗi interface mạng được cấp một địa chỉ MAC riêng biệt (thường tăng dần theo offset):
    * `WAN / LAN Port:` `F4:FC:49:6D:00:1E`
    * `Wi-Fi 2.4GHz (BSSID 1):` Thường là `F4:FC:49:6D:00:1F` (offset +1, suffix `001f`) hoặc `F4:FC:49:6D:00:20` (offset +2).
    * `Wi-Fi 5GHz (BSSID 2):` Thường là `F4:FC:49:6D:00:21` hoặc tương tự.
* **Hậu quả:** Khi chúng ta phát sóng âm mang 4 số cuối `001e`, camera quét tìm các Access Point xung quanh có BSSID kết thúc bằng `001e` nhưng sóng 2.4GHz thực tế lại mang đuôi `001f`. Camera không tìm thấy router tương ứng nên lập tức bỏ qua gói tin!

---

### Giả thuyết 2: Xung đột chuẩn Wi-Fi 6 (802.11ax) & Bảo mật WPA3 trên Router ZTE ZXHN E2633
* **Khác biệt thế hệ:**
  * **Router ZTE ZXHN E2633:** Router Wi-Fi 6 thế hệ mới (sản xuất 08/2025), mặc định kích hoạt chuẩn **802.11ax**, công nghệ OFDMA, và chuẩn mã hóa **WPA3-SAE / WPA2 Mixed**. Ngoài ra còn kích hoạt tính năng **PMF (Protected Management Frames - 802.11w)**.
  * **Camera VStarcam C22S:** Thiết kế từ năm 2019, sử dụng chip Wi-Fi thế hệ cũ chỉ hỗ trợ chuẩn **802.11b/g/n (Wi-Fi 4)** ở băng tần 2.4GHz và chuẩn mã hóa cơ bản **WPA2-PSK (AES)** không có PMF.
* **Hậu quả:** Ngay cả khi camera đã nghe và giải mã đúng mật khẩu qua sóng âm, khi nó bắt đầu quá trình bắt tay 4 bước (4-Way Handshake) với router ZTE:
  * Nếu router yêu cầu PMF (Protected Management Frames) hoặc phát Beacon frame chuẩn 802.11ax, card Wi-Fi của C22S sẽ bị từ chối kết nối (Association Reject / Deauthentication).
  * Camera thất bại ở tầng Wi-Fi Layer 2 nên không thể lấy IP DHCP và không thể báo danh lên P2P Cloud.

---

### Giả thuyết 3: Bộ lọc âm thanh Safari iOS và méo tần số FSK
* **Cơ chế âm thanh trên iPhone (iOS 18.7):**
  * Trong App Eye4 native, âm thanh được gửi trực tiếp tới phần cứng âm thanh cấp thấp (`AudioUnit`) mà không qua bộ lọc.
  * Khi phát file WAV qua trình duyệt Safari trên iPhone, hệ thống iOS tự động áp dụng các thuật toán:
    * **Dynamic Range Compression (DRC):** Nén âm lượng các dải âm cao.
    * **Acoustic Echo Cancellation / Noise Suppression:** Nhầm các nốt FSK (`6.5 kHz – 10.1 kHz`) thành tiếng rít (feedback/howling) và tự động triệt tiêu hoặc làm nhòe pha âm thanh.
* **Hậu quả:** Micro của C22S nhận được âm thanh đã bị biến dạng biên độ hoặc mất nốt, thuật toán FFT trong daemon `smartlink` của camera tính ra sai lệch CRC-16 và hủy gói tin.

---

### Giả thuyết 4: Trạng thái Reset của Camera chưa sẵn sàng nhận cấu hình
* Dòng C22S có bộ daemon `onekeywifi` kiểm tra nghiêm ngặt phân vùng lưu trữ cấu hình:
  * Nếu thao tác giữ nút Reset chưa đủ 15–20 giây, cấu hình Wi-Fi rác cũ chưa được xóa trắng hoàn toàn.
  * Khi boot lên, camera vẫn âm thầm cố kết nối lại cấu hình cũ thay vì mở micro ở chế độ bắt sóng âm (dù loa có thể phát câu thoại nhắc nhở định kỳ 2 phút).

---

## 4. Kế Hoạch & Các Hướng Xử Lý Khả Thi Tiếp Theo

### Phương án A: Xác định chính xác BSSID 2.4GHz thực tế của Router ZTE
1. Sử dụng một máy tính Windows (chạy lệnh `netsh wlan show networks mode=bssid`) hoặc cài app **WiFi Analyzer** trên điện thoại Android để quét mạng `CMCC-5Auu`.
2. Đọc chính xác 4 ký tự cuối của **BSSID 2.4GHz thực tế** (thay vì nhìn tem ngoài của router).
3. Thử lại chế độ Siêu tốc với BSSID chính xác này.

### Phương án B: Tinh chỉnh cấu hình Router ZTE ZXHN E2633 để tương thích ngược
1. Truy cập trang quản trị router: `http://192.168.10.1` (Tài khoản: `user` | Mật khẩu: `ty%X7U5N`).
2. Vào phần cấu hình Wi-Fi 2.4GHz:
   * **Bảo mật (Security):** Chuyển về **WPA2-PSK (AES)** thuần (tắt WPA3-SAE).
   * **PMF (Protected Management Frames):** Chuyển về **Disabled** hoặc **Optional**.
   * **Chế độ không dây (Mode):** Chọn **802.11b/g/n mixed** (Tắt 802.11ax trên băng tần 2.4G).

### Phương án C: Dùng App Eye4 mượn đường Wi-Fi lần đầu, sau đó liên kết Hub vĩnh viễn
1. Mở app Eye4 trên điện thoại, dùng luồng nạp native của Eye4 để cấp Wi-Fi `CMCC-5Auu` cho camera (App Eye4 native có quyền đọc BSSID trực tiếp từ iOS/Android và phát qua driver âm thanh gốc nên tỷ lệ ăn rất cao).
2. Ngay khi camera vào Wi-Fi thành công (Camera phát tiếng *"One key WiFi connection success"*):
   * Mở WebUI Camera Hub của chúng ta (hoặc trang `/sonic`).
   * Chọn tab **`[🔗 Camera Đã Có Wi-Fi (Chỉ liên kết Server)]`**.
   * Nhập UID `VSTJ-082266-VWCWH` và bấm **"🔗 Liên Kết Server Ngay"**.
3. **Từ thời điểm này:** Camera Hub nắm quyền quản lý luồng Live RTSP / P2P vĩnh viễn qua Internet mà không cần mở lại app Eye4 nữa!
