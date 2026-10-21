"""视频信息与基础 IO 辅助（无 Qt 依赖）。

Windows 下不同机器可用的解码后端不同（部分精简系统缺 H.264 解码器），
open_capture 依次尝试多个后端并优先使用随包分发的 FFmpeg 插件；
read_frame 提供带顺序回退的可靠按帧号读取。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import cv2


class VideoOpenError(Exception):
    """视频文件无法打开或读取。"""


def fourcc(code: str) -> int:
    """'mp4v' -> VideoWriter fourcc 整数（兼容不同 OpenCV 版本）。"""
    fn = getattr(cv2, "VideoWriter_fourcc", None)
    if fn is not None:
        return fn(*code)
    return int.from_bytes(code.encode("ascii"), "little")


# 尝试顺序：FFmpeg（随包分发、编解码最全）→ 默认 → MSMF → DShow
_BACKS = (
    (cv2.CAP_FFMPEG, "FFMPEG"),
    (cv2.CAP_ANY, "默认"),
    (cv2.CAP_MSMF, "MSMF"),
    (cv2.CAP_DSHOW, "DShow"),
)


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    frame_count: int  # 可能为 0（流/损坏的元数据），仅作估计值使用

    @property
    def duration_sec(self) -> float:
        return self.frame_count / self.fps if self.fps > 0 else 0.0

    def summary(self) -> str:
        mins, sec = divmod(self.duration_sec, 60)
        return (
            f"分辨率 {self.width}×{self.height}   帧率 {self.fps:.2f} fps   "
            f"帧数 {self.frame_count if self.frame_count > 0 else '未知'}   "
            f"时长 {int(mins):02d}:{int(sec):02d}"
        )


def open_capture(path: str, verify: bool = False) -> Tuple[cv2.VideoCapture, str]:
    """打开视频，按优先级尝试多个后端，返回 (cap, 后端名)。

    verify=True 时额外读取一帧确认真的能解码（用于 GUI 载入，尽早暴露
    「能打开元信息但读不出画面」的问题），成功后回位到第 0 帧。
    全部失败抛 VideoOpenError。
    """
    tried = ""
    for be, name in _BACKS:
        cap = cv2.VideoCapture(path, be)
        if cap.isOpened():
            if not verify:
                return cap, name
            ok, _frame = cap.read()
            if ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                return cap, name
            tried = tried or name
            cap.release()
            continue
        cap.release()
    raise VideoOpenError(
        f"无法打开视频文件：{path}\n"
        f"可能是不支持的编码格式、文件已损坏，或系统缺少对应解码器。"
        + (f"（后端 {tried} 能打开但无法解码画面）" if tried else ""))


def probe_video(path: str) -> VideoInfo:
    """打开视频读取元信息后关闭。失败抛 VideoOpenError。"""
    cap, _backend = open_capture(path)
    try:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        if not fps or fps != fps or fps <= 0:  # NaN/0 防护
            fps = 25.0
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if frame_count < 0:
            frame_count = 0
        if width <= 0 or height <= 0:
            raise VideoOpenError(f"视频元信息异常（{width}×{height}）：{path}")
        return VideoInfo(path=path, width=width, height=height, fps=fps, frame_count=frame_count)
    finally:
        cap.release()


def read_frame(cap: cv2.VideoCapture, idx: int, path: str = ""):
    """可靠地读取第 idx 帧：先按帧号定位，失败时回退为重新打开顺序抓取。

    返回 (ok, frame)。顺序回退在长视频上较慢，仅在 seek 失效时触发。
    """
    if idx < 0:
        return False, None
    try:
        if cap.set(cv2.CAP_PROP_POS_FRAMES, idx):
            ok, frame = cap.read()
            if ok:
                return True, frame
    except Exception:  # noqa: BLE001 —— 某些后端 set 会抛异常，继续走回退
        pass
    if not path:
        return False, None
    cap2, _ = open_capture(path)
    try:
        for _ in range(idx):
            if not cap2.grab():
                return False, None
        return cap2.read()
    finally:
        cap2.release()


def format_ts(frame_idx: int, fps: float) -> str:
    """帧号 -> 'HH:MM:SS.mmm' 原始时间戳。"""
    if fps <= 0:
        fps = 25.0
    total_ms = int(round(frame_idx * 1000.0 / fps))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
