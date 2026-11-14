# Báo Cáo Tổng Hợp Thay Đổi & Phân Tích Tối Ưu Luồng Video (Camera Hub)

*Ngày lập báo cáo: 14/11/2026*  
*Commit gốc trước đợt thay đổi: `59308c7` (`main`)*

---

## I. Tóm tắt các thay đổi đã triển khai trong repo

Các chỉnh sửa đang nằm trong commit lần này gồm hai hạng mục chính: **(1) Tối ưu kích hoạt ghi hình từ AI/Motion trên Firmware (`Phương án 1`)** và **(2) Sửa lỗi phát lại clip H.265 + ổn định luồng Live WebSocket trên WebUI (`v6`)**.

### 1. Phương án 1 — Tối ưu Firmware AI & Gộp luồng sự kiện (0% CPU decode thêm)
* **`eye4_rtsp_proxy.py`**:
  * Mở rộng `_send_auto_provision()` (`L2283`) để nhận và đồng bộ cấu hình `auto_track`, `human_sensitive`, `motion_sensitivity`: bật OSD phần cứng, cấu hình `set_alarm.cgi` (`motion_armed=1`, `motion_sensitivity`), đồng bộ `trans_cmd_string.cgi?cmd=2127` (AI bám người) và `cmd=2126` (độ nhạy nhận diện người), đánh dấu `_auto_provision_done = True` khi hoàn tất.
  * Thêm `_emit_alarm()` (`L2686`) và cập nhật `_finish_cgi_msg()` để phân tách nguồn cảnh báo phần cứng từ camera: `"AI_TRACK"` (`cmd=2127`, `track_status > 0`) và `"HW_MOTION"` (`get_status.cgi`, `alarm_status > 0`).
* **`camera_hub.py`**:
  * Thêm cờ cấu hình `auto_firmware_ai` (mặc định `True`, xem `L813`, `L841`, `L1423`, `L2984-2986`), `_effective_camera_ai_targets()` (`L864`: áp dụng `track_enable=1`, `human_sensitive=3`, `motion_sensitivity=10` nếu camera chưa có cấu hình ghi đè riêng) và `_sync_camera_ai_once()` (`L1189`) để kiểm tra và đồng bộ lại qua CGI khi camera kết nối ổn định.
* **`motion_recorder.py`**:
  * Phân loại sự kiện từ camera trong `on_hardware_alarm()` (`L510`) thành `HW_ALARM_AI` vs `HW_ALARM_MD`, đồng thời nâng cấp/gộp trigger đang mở trong `trigger_motion()` (`L531`) theo bảng ưu tiên `_TRIGGER_PRIORITY` (`L493`: `MANUAL=5 > HW_ALARM_AI=4 > HW_ALARM_MD/HW_ALARM=3 > MANUAL_TEST=2 > PFRAME_SPIKE=1`).
  * Điều chỉnh `_start_live_decoder()` (`L329`) với `-probesize 49152`, `-analyzeduration 50000`, thực hiện handoff đồng bộ từ ring buffer (`_get_prime_bitstream()`) sang `_live_proc` trước khi nhận frame mới để tránh rớt P-frame lúc spawn FFmpeg; bổ sung `close()` (`L482`).
* **`static/index.html` & `static/js/hub-settings.js`**:
  * Bổ sung tùy chọn `set-auto-fw-ai` (*Tự động tối ưu AI & CĐ Firmware (0% CPU)*) trong cấu hình Hub và tách bộ lọc sự kiện `HW_ALARM_AI` (*AI Bám người (Camera)*) / `HW_ALARM_MD` (*Chuyển động (Camera)*).

### 2. Sửa lỗi phát lại clip H.265 & ổn định WebSocket Live trên WebUI (`v6`)
* **`camera_hub.py`**:
  * Nâng `STATIC_ASSET_VER = "v6"` (`L36`, static route `L3053`) và thêm route `/favicon.ico` trả `204`.
  * Bổ sung pipeline chuyển mã on-demand sang H.264 cho các trình duyệt không decode trực tiếp được MP4 H.265: `_resolve_recording_mp4()` (`L2860`), `_prune_h264_cache()`, `_ensure_h264_mp4()` (`L2895`, dùng lock theo từng clip và cache trong `recordings/.h264_cache`) và `handle_event_video()` (`L2942`, route `/api/event-video` hỗ trợ HTTP Range `206`, chặn path traversal).
* **`motion_recorder.py`**:
  * Cập nhật `sync_codec_params()` (`L164`), `get_live_snapshot()` (`L228`), `_scan_events()` (`L915`: bỏ qua thư mục ẩn `.*` như `.h264_cache` khi quét sự kiện) và `delete_event()` (`L1031`: xóa kèm file cache `.h264_cache` tương ứng khi xóa clip).
* **Frontend (`static/index.html`, `static/sonic.html`, `static/js/ws-stream.js`, `static/js/ptz-modal.js`, `static/js/events.js`)**:
  * Trỏ toàn bộ tài nguyên tĩnh sang `/static/v6/`.
  * `ws-stream.js`: trì hoãn `URL.revokeObjectURL` (`1500ms`) sau khi nạp ảnh mới, thêm `ensureWatchdog()` (`L25`) và xử lý `ws.onerror` trong `connectStreamWs()` (`L86`) để tự phục hồi khi WebSocket bị treo hoặc đứt kết nối.
  * `ptz-modal.js`: thêm fallback `error` sang `snapshot.jpg` trong `openFocusModal()` (`L416`).
  * `events.js`: kiểm tra `canBrowserDecodeHevc()` và tự động chuyển phát sang `/api/event-video?rel=...` trong `playEvent()` (`L257`) khi `<video>` gặp lỗi decode HEVC, kèm dọn dẹp sạch trong `closePlayModal()` (`L289`).

> **Trạng thái kiểm chứng WebUI (`v6`) trước khi đo tối ưu luồng:** Đã chạy `py_compile`, `node --check static/js/*.js`, kiểm tra các route HTTPS công khai (`/`, `/sonic`, `/static/v6/js/app.js`, `/favicon.ico` trả `204`, traversal trả `404`, `/api/event-video?...` trả `206 video/mp4` codec `h264/avc1/yuv420p/1280x720`) và kiểm tra headless browser (`WSS_CHECK` nhận `139` & `92` frames với `bad_jpeg=0`, `GRID_OK`, `FOCUS_OK`, `EVENTS_H264_FALLBACK_OK=True`, `http_errors=[]`, `console_errors=[]`).

---

## II. Hiện trạng đo đạc thực tế luồng video (2 camera)

Môi trường đo trực tiếp trên Hub với 2 camera đang kết nối (`Gau gau` — firmware `48.82.230.19` và `Gau gau 2` — firmware `48.82.230.12`):

1. **Đường truyền P2P & tham số encoder**:
   * Máy chủ Hub (`192.168.77.243`, `4` vCPU) nằm khác subnet với 2 camera (`192.168.31.90` và `192.168.31.91`) đứng sau hệ thống Multi-WAN NAT (nhiều WAN IP public khác nhau).
   * Cả hai camera đều không đục lỗ P2P trực tiếp được và fallback sang **`WAN_RELAY`** tại mốc `2.8s`:
     * `Gau gau`: relay `66.90.98.42:46154` (RTT `~24.6ms`, `cmd_srtt` `85–96ms`).
     * `Gau gau 2`: relay `50.7.250.130:46682` (RTT `~27.5ms`, `cmd_srtt` `69–75ms`).
   * `get_record.cgi` trả về `enc_bitrate=1024`, `enc_framerate=25`, `enc_ratemode=0` (VBR), `enc_keyframe=25`, nhưng khoảng cách keyframe (GOP) thực tế trong file MP4 ghi được là **`100 frames` (`4.0s`)**.
2. **Độ ổn định RTSP đầu ra & chất lượng MP4 lưu trữ**:
   * Đo RTSP nội bộ trong `10.5s` sau GOP:
     * `GauGau` (`:9556`): `28` cụm xả burst `<10ms`, `1` khoảng trống (gap) `>90ms` (`max = 93.8ms`).
     * `GauGau2` (`:9557`): `70` cụm xả burst `<10ms`, `5` khoảng trống `>90ms` (`[182.7, 92.8, 109.3, 106.2, 99.9] ms`).
   * FPS thực nhận biến động mạnh: `/api/status` ghi nhận `Gau gau 2` có lúc tụt xuống **`15.4 FPS`**; các clip MP4 ghi lại chỉ đạt **`19.3 FPS`** (`185 frames / 9.6s`) và **`20.5 FPS`** (với các gap giữa frame lên tới `81–94ms`).
3. **WebSocket MJPEG & mức tiêu thụ tài nguyên trên Hub**:
   * `ws://127.0.0.1:8080/ws/streams` (localhost): có `5` gap `>100ms` (`max = 226.1ms`).
   * `wss://.../ws/streams` (qua tunnel Internet/Cloudflare): tụt xuống **`21.4–21.9 FPS`**, xuất hiện **`17–20` gap `>80ms`** (`max = 233.4ms`) trong `8s`.
   * Băng thông WS MJPEG đo được cho chỉ 2 camera: `Gau gau` `8.43 Mbps` (`~43 KB/frame`) + `Gau gau 2` `10.68 Mbps` (`~55 KB/frame`) = **`19.1 Mbps` (`2.33 MB/s`)**.
   * CPU (`nproc=4`): 2 tiến trình `ffmpeg` live decoder chiếm `49.1%` + `45.8%`, cộng thêm `python3` `17.5%` = **`112.4%` CPU** khi chạy thường trực 2 camera.

---

## III. 4 nguyên nhân gốc rễ gây giật hình, mất frame & tụt FPS MP4

1. **Mọi kết nối PPPP đều rơi về `WAN_RELAY` do khác subnet & Multi-WAN NAT (`eye4_rtsp_proxy.py`)**:
   * Hub (`192.168.77.243`) không cùng subnet LAN với camera (`192.168.31.90/91`), trong khi đường WAN đi qua Multi-WAN NAT nên UDP hole-punching thất bại. Sau `2.8s` trong `_wan_punch_loop()` (`L1715`), proxy buộc phải chốt qua `WAN_RELAY` (`_commit_best_relay()` `L1277`), làm tăng `cmd_srtt` lên `69–96ms` và khiến gói UDP dễ trễ/rơi theo cụm.
2. **Hàng đợi ghép gói `VideoReassembly` chờ gap tối đa `1.0s` (`eye4_rtsp_proxy.py`)**:
   * Trong `VideoReassembly` (`L2778-2874`), tham số `REORDER_WINDOW = 256` (`L2793`) và `REORDER_TIMEOUT_SEC = 1.0` (`L2794`). Khi mất 1 gói UDP trên relay, bộ đệm giữ toàn bộ gói đến sau tối đa tới `1.0s` (hoặc đủ `256` gói) mới gọi `_handle_gap()` (`L2850`), tạo ra chuỗi "khựng hình kéo dài -> xả burst `<10ms` hàng loạt" và làm hỏng chuỗi P-frame.
3. **Chu kỳ phát hiện stall dài + batch ACK + GOP thực tế `100 frames` (`eye4_rtsp_proxy.py`, `camera_hub.py`)**:
   * `_handle_drw_encrypted()` (`L1383-1498`) gom ACK theo nhịp xử lý, còn `_video_keepalive_loop()` (`L2221-2274`) đặt `stall_sec = 2.5 if is_wan else 1.5` (`L2236`). Kết hợp với việc `camera_hub.py` đặt `desired_kf = 50` (`L1158`) nhưng firmware thực tế vẫn xuất GOP `100 frames` (`4.0s`), mỗi lần mất gói trên relay sẽ khiến decoder lỗi/đứng hình lâu cho tới keyframe kế tiếp.
4. **Nghẽn WebSocket MJPEG (`19.1 Mbps`), tải CPU FFmpeg (`112.4%`), và mux MP4 theo FPS trung bình (`camera_hub.py`, `motion_recorder.py`)**:
   * `handle_ws_streams()` (`camera_hub.py:1430`) dùng vòng lặp `asyncio.sleep(0.008 if sent_any else 0.015)` (`L1503`) gửi toàn bộ JPEG full-res (`43–55 KB/frame`), tạo lưu lượng `19.1 Mbps` rất dễ dồn buffer khi đi qua Cloudflare/Internet (`gap >233ms`).
   * Mỗi camera chạy 1 tiến trình FFmpeg decode H.265 -> MJPEG full-res liên tục (`~46-49%` CPU/cam), nhanh chóng chạm trần khi tăng thêm camera hoặc chạy chung với H.264 on-demand transcoding.
   * `_mux_frames_to_mp4()` (`motion_recorder.py:780`) tính `actual_fps = max(10.0, min(30.0, len(frames) / duration_sec))` và ép `-r fps_str` (tốc độ khung hình đều dựa trên tổng số frame nhận được). Khi luồng vào từ relay bị mất/rớt khung cục bộ, clip MP4 xuất ra vừa bị hạ FPS trung bình (`19.3–20.5 FPS`), vừa không phản ánh đúng thời điểm giãn cách giữa các frame bị hụt.

---

## IV. Lộ trình tối ưu đề xuất (chưa triển khai trong commit này)

### Nhóm A — Tối ưu Mạng & Tầng PPPP (ưu tiên cao nhất)
1. **Loại bỏ `WAN_RELAY` bằng đường truyền trực tiếp**:
   * Đưa Hub vào cùng VLAN/subnet `192.168.31.x` với camera, hoặc mở routing nội bộ không NAT giữa `192.168.77.243` và `192.168.31.90/91` để dùng `LAN_P2P` (`RTT < 2ms`), hoặc gán 1 WAN IP cố định cho đường UDP P2P thay vì đi qua Multi-WAN cân bằng tải.
2. **Tinh chỉnh `VideoReassembly` & cơ chế phản hồi DRW (`eye4_rtsp_proxy.py`)**:
   * Giảm `REORDER_TIMEOUT_SEC` từ `1.0s` xuống mức nhỏ hơn nhiều (ví dụ `0.15s–0.25s`) và thu gọn `REORDER_WINDOW` để hạn chế tình trạng treo chờ gói mất rồi xả dồn burst.
   * Gửi DRW ACK nhanh hơn khi nhận đủ cụm dữ liệu và hạ ngưỡng `stall_sec` trong `_video_keepalive_loop()`.
3. **Đồng bộ lại GOP / Bitrate thực tế trên camera**:
   * Xác minh tham số `set_media.cgi` / `set_record.cgi` để ép khoảng cách I-frame thực tế về `25–50 frames` (`1–2s`) thay vì `100 frames`, giảm độ trễ hồi phục khung hình sau khi mất gói.

### Nhóm B — Tối ưu Pipeline WebUI & Mux MP4 trên Hub
1. **Giảm tải băng thông WebSocket `/ws/streams` (`camera_hub.py`, `motion_recorder.py`)**:
   * Chuyển ảnh MJPEG cho Grid live về độ phân giải/chất lượng hợp lý (ví dụ `640x360` thay vì `1280x720` `~43–55 KB/frame`), cắt giảm mạnh băng thông từ `19.1 Mbps` xuống còn vài Mbps để mượt khi xem qua Cloudflare/WAN.
   * Drop frame cũ nếu WebSocket client gửi không kịp thay vì để dồn latency; cân nhắc bật/giảm tần suất live MJPEG khi không có client Grid/Focus đang kết nối.
2. **Mux MP4 theo timestamp thực tế (`motion_recorder.py`)**:
   * Thay thế cách ép `-r actual_fps` cố định bằng mux dựa trên timestamp từng frame (VFR hoặc chèn timestamp chính xác qua PTS/FFmpeg concat/timebase) để video lưu lại giữ đúng nhịp thời gian thực khi luồng camera có dao động FPS.
