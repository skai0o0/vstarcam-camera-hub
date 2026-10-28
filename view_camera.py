#!/usr/bin/env python3
"""
Simple live viewer for the Eye4 RTSP Proxy stream using OpenCV.
Press 'q' or ESC in the video window to exit, or 's' to save a screenshot.
"""
import os
import time
import cv2

RTSP_URL = "rtsp://127.0.0.1:9555/"


def main():
    # Force TCP transport for RTSP (required by eye4_rtsp_proxy)
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    print(f"Connecting to {RTSP_URL} ...")
    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print("Failed to connect. Make sure eye4_rtsp_proxy.py is running!")
        return

    print("Connected! Press 'q' or ESC to quit, 's' to save a snapshot.")
    window_name = "VStarcam C22S (192.168.31.90) - RTSP Live"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Stream interrupted, reconnecting...")
            cap.release()
            time.sleep(1)
            cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
            continue

        cv2.imshow(window_name, frame)
        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            break
        elif key == ord("s"):
            filename = f"snapshot_{int(time.time())}.jpg"
            cv2.imwrite(filename, frame)
            print(f"Saved {filename}")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
