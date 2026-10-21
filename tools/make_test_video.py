"""生成合成监控测试视频（供单元测试 / selftest / 演示使用）。

画面内容：
- 静止背景（深灰底 + 网格线 + 静止色块）——不应触发保留；
- 左上角"时间数字"OSD：黑白交替的底色方块 + 每秒跳动的秒数——
  必须用剔除区域盖住，否则每秒产生一次"伪变化"；
- 第 120-179 帧：橙色方块移动（有效运动段 A）；
- 第 240-254 帧：第二段移动（有效运动段 B）。
"""
from __future__ import annotations

import cv2
import numpy as np

from app.core.video_io import fourcc

WIDTH, HEIGHT, FPS, FRAMES = 640, 480, 25, 300
MOTION_A = (120, 180)   # [start, end)
MOTION_B = (240, 255)
SQUARE_COLOR = (60, 80, 255)     # BGR 橙色
BG_COLOR = (40, 44, 48)
GRID_COLOR = (70, 74, 78)
STATIC_BLOCK = (90, 120, 60)


def make_test_video(path: str, width: int = WIDTH, height: int = HEIGHT,
                    fps: int = FPS, frames: int = FRAMES) -> str:
    writer = cv2.VideoWriter(path, fourcc("mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"无法创建测试视频写入器：{path}")
    try:
        for i in range(frames):
            img = np.full((height, width, 3), BG_COLOR, np.uint8)
            for gx in range(0, width, 40):
                cv2.line(img, (gx, 0), (gx, height), GRID_COLOR, 1)
            for gy in range(0, height, 40):
                cv2.line(img, (0, gy), (width, gy), GRID_COLOR, 1)
            cv2.rectangle(img, (400, 300), (560, 420), STATIC_BLOCK, -1)

            # 时间数字 OSD：底色每秒黑白反转 + 秒数变化（两处同时变，差异显著）
            sec = i // fps
            box_a = (14, 14)
            box_b = (230, 58)
            if sec % 2 == 0:
                cv2.rectangle(img, box_a, box_b, (30, 30, 30), -1)
                cv2.putText(img, f"12:34:{sec:02d}", (20, 46),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (240, 240, 240), 2, cv2.LINE_AA)
            else:
                cv2.rectangle(img, box_a, box_b, (240, 240, 240), -1)
                cv2.putText(img, f"12:34:{sec:02d}", (20, 46),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (30, 30, 30), 2, cv2.LINE_AA)

            # 运动段 A
            if MOTION_A[0] <= i < MOTION_A[1]:
                t = i - MOTION_A[0]
                x, y = 80 + t * 8, 200 + t * 2
                cv2.rectangle(img, (x, y), (x + 80, y + 80), SQUARE_COLOR, -1)
            # 运动段 B
            if MOTION_B[0] <= i < MOTION_B[1]:
                t = i - MOTION_B[0]
                x, y = 60 + t * 10, 80 + t * 3
                cv2.rectangle(img, (x, y), (x + 80, y + 80), SQUARE_COLOR, -1)

            writer.write(img)
    finally:
        writer.release()
    return path


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "test_video.mp4"
    print(make_test_video(out))
