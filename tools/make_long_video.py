# -*- coding: utf-8 -*-
"""生成长时间 1080p 测试视频：稀疏运动（用于性能测试）。"""
import os
import sys
import tempfile
import time

import cv2
import numpy as np


def main():
    fps = 25.0
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    n = int(fps * seconds)
    w, h = 1920, 1080
    out = os.path.join(tempfile.gettempdir(), "long_1080p.mp4")
    vw = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not vw.isOpened():
        raise SystemExit("VideoWriter open failed")
    # 静止背景 + 噪声纹理，时间数字，每分钟前 5 秒有移动方块
    rng = np.random.default_rng(7)
    bg2d = rng.integers(30, 60, (h, w), dtype=np.uint8)
    bg2d = cv2.GaussianBlur(bg2d, (31, 31), 0)
    bg = cv2.cvtColor(bg2d, cv2.COLOR_GRAY2BGR)
    t0 = time.perf_counter()
    for i in range(n):
        frame = bg.copy()
        ts = i / fps
        hh, mm, ss = int(ts // 3600), int(ts // 60) % 60, int(ts) % 60
        cv2.putText(frame, f"{hh:02d}:{mm:02d}:{ss:02d}", (40, 90),
                    cv2.FONT_HERSHEY_SIMPLEX, 2.2, (255, 255, 255), 6, cv2.LINE_AA)
        if (ts % 60) < 5:  # 每分钟前 5 秒有运动
            x = int((ts % 60) * 300) % (w - 300)
            cv2.rectangle(frame, (x, 500), (x + 260, 720), (60, 200, 90), -1)
        vw.write(frame)
        if i % 1500 == 0:
            print(f"gen {i}/{n}", flush=True)
    vw.release()
    print(f"LONG_VIDEO_OK {out} {n} frames in {time.perf_counter()-t0:.0f}s "
          f"size={os.path.getsize(out)//1048576}MB", flush=True)


if __name__ == "__main__":
    main()
