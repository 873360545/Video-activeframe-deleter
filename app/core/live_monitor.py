# -*- coding: utf-8 -*-
"""实时监控：屏幕区域捕获 + 逐区域独立运动分析 + 运动片段录制。

工作方式：
- ScreenRegionSelector 在屏幕上框选一个或多个矩形区域（全局物理像素坐标）；
- LiveMonitor 以固定帧率抓取每个区域，逐区域独立做帧间变化分析
  （复用视频分析引擎的像素敏感度/有效变化阈值/前后缓冲逻辑）；
- 每个区域独立出结果：独立的实时状态（变化率/运动状态）、独立的事件列表、
  独立的运动片段视频文件（运动开始前/结束后各保留若干秒）；
- 监控结束后可导出全部区域的事件报告 CSV；可选把每个区域的全部运动片段
  合并为一个视频（merge_event_clips）。
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import time
import warnings
from collections import deque
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal

from app.core.analyzer import SegmentDecider, frame_change_ratio
from app.core.report import write_csv

# ---------------------------------------------------------------- 纯逻辑（可单测）


def assemble_events(ratios: List[float], motion_percent: float,
                    pre_roll: int, post_roll: int) -> List[Tuple[int, int]]:
    """由变化率序列生成运动事件 [(起始帧, 结束帧)]（含前后缓冲，近邻自动合并）。

    与视频分析引擎的片段状态机语义一致：
    - 变化率 ≥ 阈值判为运动帧；
    - 运动开始前 pre_roll 帧、结束后 post_roll 帧一并计入事件；
    - 间隔小于 post_roll 的两段运动自动合并为一个事件。
    """
    dec = SegmentDecider(motion_percent, pre_roll, post_roll)
    buffer: deque = deque(maxlen=max(0, int(pre_roll)))
    kept: List[int] = []
    for i, r in enumerate(ratios):
        st = dec.step(r)
        if st == "start":
            kept.extend(buffer)
            buffer.clear()
            kept.append(i)
        elif st in ("continue", "post"):
            kept.append(i)
        else:
            buffer.append(i)
    events: List[Tuple[int, int]] = []
    s: Optional[int] = None
    prev: Optional[int] = None
    for i in kept:
        if s is None:
            s = i
        elif prev is not None and i != prev + 1:  # 缓冲不足导致的不连续也切分为新事件
            events.append((s, prev))
            s = i
        prev = i
    if s is not None and prev is not None:
        events.append((s, prev))
    return events


def sanitize_name(name: str) -> str:
    """区域名 -> 安全文件名成分。"""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name)).strip("._")
    return cleaned or "区域"


def region_local_mask(region_rect: Tuple[int, int, int, int],
                      analysis_rects: List[Tuple[int, int, int, int]]
                      ) -> Optional[np.ndarray]:
    """监控区域内的分析区域掩码（区域局部坐标，uint8，255=参与分析）。

    analysis_rects 为屏幕全局物理矩形；与分析区域无交集时返回 None
    （= 整个监控区域都参与分析）。
    """
    if not analysis_rects:
        return None
    x, y, w, h = region_rect
    mask = np.zeros((h, w), np.uint8)
    for (ax, ay, aw, ah) in analysis_rects:
        x0, y0 = max(ax - x, 0), max(ay - y, 0)
        x1, y1 = min(ax + aw - x, w), min(ay + ah - y, h)
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = 255
    return mask if mask.any() else None


def merge_event_clips(paths: List[str], out_path: str, fps: float
                      ) -> Tuple[int, int]:
    """把同一区域的多个运动片段按时间顺序合并为一个 mp4 视频（逐帧拷贝）。

    返回 (写入总帧数, 贡献了帧的片段数)。无可用片段/无可读帧时抛 ValueError。
    尺寸异常的帧自动缩放到首个片段的尺寸（同一区域尺寸应一致，此为兜底）。
    """
    paths = [p for p in paths if p and os.path.isfile(p)]
    if not paths:
        raise ValueError("没有可合并的片段文件")
    vw = None
    w2 = h2 = 0
    total = 0
    used = 0
    for p in paths:
        cap = cv2.VideoCapture(p)
        n_this = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if vw is None:
                    h, w = frame.shape[:2]
                    w2, h2 = max(2, w - (w % 2)), max(2, h - (h % 2))
                    vw = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"),
                                         float(fps), (w2, h2))
                    if not vw.isOpened():
                        raise RuntimeError("无法创建合并输出视频（编码器不可用）")
                if frame.shape[0] != h2 or frame.shape[1] != w2:
                    frame = cv2.resize(frame, (w2, h2),
                                       interpolation=cv2.INTER_AREA)
                else:
                    frame = frame[:h2, :w2]
                vw.write(frame)
                n_this += 1
        finally:
            cap.release()
        if n_this:
            used += 1
            total += n_this
    if vw is None or total == 0:
        if vw is not None:
            vw.release()
        try:
            if os.path.isfile(out_path):
                os.remove(out_path)
        except OSError:
            pass
        raise ValueError("片段中没有任何可读帧")
    vw.release()
    return total, used


class RegionConfig:
    """单个监控区域的配置。rect 为屏幕物理像素坐标 (x, y, w, h)。

    analysis_rects 为该区域内的分析子区域（同样为屏幕全局物理坐标）；
    为空时整个监控区域都参与变化分析。
    """

    __slots__ = ("name", "rect", "pixel_threshold", "motion_percent",
                 "pre_frames", "post_frames", "record", "analysis_rects")

    def __init__(self, name: str, rect: Tuple[int, int, int, int],
                 pixel_threshold: int = 25, motion_percent: float = 0.5,
                 pre_frames: int = 15, post_frames: int = 30, record: bool = True,
                 analysis_rects: Optional[List[Tuple[int, int, int, int]]] = None):
        self.name = name
        self.rect = (int(rect[0]), int(rect[1]), max(2, int(rect[2])), max(2, int(rect[3])))
        self.pixel_threshold = int(pixel_threshold)
        self.motion_percent = float(motion_percent)
        self.pre_frames = max(0, int(pre_frames))
        self.post_frames = max(0, int(post_frames))
        self.record = bool(record)
        self.analysis_rects = [tuple(int(v) for v in r)
                               for r in (analysis_rects or [])]


def make_region_tag(index: int) -> str:
    return f"区域{index}"


# ---------------------------------------------------------------- 捕获辅助


def _clip_for_rect(rect: Tuple[int, int, int, int], monitors) -> dict:
    """把全局物理像素矩形映射为 mss 抓取参数（取包含该矩形的显示器）。"""
    x, y, w, h = rect
    for mon in monitors[1:]:
        mx, my = mon["left"], mon["top"]
        mw, mh = mon["width"], mon["height"]
        if mx <= x and my <= y and x + w <= mx + mw and y + h <= my + mh:
            return {"left": x - mx, "top": y - my, "width": w, "height": h}
    m0 = monitors[0] if monitors else {"left": 0, "top": 0}
    return {"left": x - m0["left"], "top": y - m0["top"], "width": w, "height": h}


def _new_capture():
    import mss
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return mss.mss()
        except Exception:
            return mss.MSS()  # 兼容未来版本


def _grab_bgr(sct, clip: dict) -> Optional[np.ndarray]:
    raw = sct.grab(clip)
    arr = np.asarray(raw)
    if arr.size == 0:
        return None
    return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)


# ---------------------------------------------------------------- 实时监控线程


class LiveMonitor(QThread):
    """多区域实时屏幕监控线程。

    信号：
      regionStats(dict)      聚合状态（节流 ~4Hz）：{rid: {ratio, motion, events, recording}}
      previewReady(int, object)  (rid, BGR ndarray，已缩至 ≤480px 宽，~每帧)
      eventAdded(dict)       事件记录 {region, name, start, end, duration, max_ratio, clip}
      failed(str)            致命错误（如捕获失败）
      stoppedLive()          正常停止
    """

    regionStats = Signal(object)   # dict: {rid: {ratio, motion, events, recording}}
    previewReady = Signal(int, object)
    eventAdded = Signal(object)    # dict: {region, name, start, end, ...}
    failed = Signal(str)
    stoppedLive = Signal()

    def __init__(self, regions: List[RegionConfig], fps: float = 15.0,
                 output_dir: str = "", parent=None):
        super().__init__(parent)
        self._regions = list(regions)
        self._fps = max(1.0, float(fps))
        self._interval = 1.0 / self._fps
        self._out_dir = output_dir
        self._stop_evt = False

    # ---- 对外控制
    def stop(self):
        self._stop_evt = True

    def output_dir(self) -> str:
        return self._out_dir

    # ---- 线程体
    def run(self):  # noqa: C901
        try:
            self._run_loop()
        except Exception as exc:  # 捕获/写盘等致命错误
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _run_loop(self):
        if self._out_dir:
            os.makedirs(self._out_dir, exist_ok=True)
        sct = _new_capture()
        clips = [_clip_for_rect(rc.rect, sct.monitors) for rc in self._regions]

        states = []
        for k, rc in enumerate(self._regions):
            amask = region_local_mask(rc.rect, rc.analysis_rects)
            if amask is not None and amask.shape != (rc.rect[3], rc.rect[2]):
                amask = None  # 尺寸异常兜底：退回整区域分析
            states.append({
                "rid": k, "cfg": rc, "clip": clips[k],
                "amask": amask,
                "prev": None, "dec": SegmentDecider(rc.motion_percent,
                                                    rc.pre_frames, rc.post_frames),
                "buf": deque(maxlen=rc.pre_frames),      # 前缓冲 (bgr)
                "pend": [],                               # 事件中已收集的帧 (bgr)
                "ev_start": None, "ev_max": 0.0,
                "events": [], "recording": False,
            })

        last_stats = 0.0
        idx = 0
        t_prev = time.monotonic()
        while not self._stop_evt:
            now = time.monotonic()
            wait = self._interval - (now - t_prev)
            if wait > 0:
                time.sleep(min(wait, 0.5))
                if self._stop_evt:
                    break
            t_prev = time.monotonic()

            stats_out = {}
            for st in states:
                rc: RegionConfig = st["cfg"]
                try:
                    bgr = _grab_bgr(sct, st["clip"])
                except Exception as exc:
                    self.failed.emit(f"屏幕捕获失败（{rc.name}）：{exc}")
                    return
                if bgr is None:
                    continue
                gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                gray = cv2.GaussianBlur(gray, (5, 5), 0)

                ratio = 0.0
                if st["prev"] is not None and st["prev"].shape == gray.shape:
                    _n, ratio, _bin = frame_change_ratio(st["prev"], gray,
                                                         st["amask"],
                                                         rc.pixel_threshold)
                st["prev"] = gray

                # 片段状态机 + 帧收集（与视频引擎语义一致）
                act = st["dec"].step(ratio)
                if act == "start":
                    st["pend"].extend(st["buf"])
                    st["buf"].clear()
                    st["pend"].append(bgr)
                    st["ev_start"] = _dt.datetime.now()
                    st["ev_max"] = ratio
                    st["recording"] = rc.record
                elif act in ("continue", "post"):
                    if st["pend"]:
                        st["pend"].append(bgr)
                    st["ev_max"] = max(st["ev_max"], ratio)
                else:
                    if st["pend"]:
                        self._flush_event(st, fps=self._fps)
                    st["buf"].append(bgr)
                    st["recording"] = False

                stats_out[st["rid"]] = {
                    "ratio": ratio,
                    "motion": bool(st["dec"].active),
                    "events": len(st["events"]),
                    "recording": st["recording"],
                }
                if idx % 1 == 0:
                    pv = bgr
                    if pv.shape[1] > 480:
                        nh = max(1, int(pv.shape[0] * 480 / pv.shape[1]))
                        pv = cv2.resize(pv, (480, nh), interpolation=cv2.INTER_AREA)
                    self.previewReady.emit(st["rid"], pv)

            if now - last_stats >= 0.25:
                self.regionStats.emit(stats_out)
                last_stats = now
            idx += 1

        # 停止：冲刷未结束的事件
        for st in states:
            if st["pend"]:
                self._flush_event(st, fps=self._fps)
        self.stoppedLive.emit()

    # ---- 事件落盘
    def _flush_event(self, st: dict, fps: float):
        rc: RegionConfig = st["cfg"]
        frames = st["pend"]
        st["pend"] = []
        st["recording"] = False
        if not frames:
            return
        ev_start = st["ev_start"] or _dt.datetime.now()
        dur = len(frames) / fps
        ev_end = ev_start + _dt.timedelta(seconds=dur)
        clip_path = ""
        if rc.record and self._out_dir:
            h, w = frames[0].shape[:2]
            w2, h2 = w - (w % 2), h - (h % 2)  # mp4v 需要偶数尺寸
            stamp = ev_start.strftime("%Y%m%d_%H%M%S")
            clip_path = os.path.join(
                self._out_dir, f"{sanitize_name(rc.name)}_{stamp}.mp4")
            vw = cv2.VideoWriter(clip_path, cv2.VideoWriter_fourcc(*"mp4v"),
                                 fps, (w2, h2))
            if vw.isOpened():
                for f in frames:
                    vw.write(f[:h2, :w2])
                vw.release()
            else:
                clip_path = ""  # 编码不可用：只记事件
        rec = {
            "region": st["rid"] + 1,
            "name": rc.name,
            "start": ev_start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": ev_end.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": dur,
            "max_ratio": float(st["ev_max"]),
            "clip": clip_path,
            "frames": len(frames),
        }
        st["events"].append(rec)
        self.eventAdded.emit(rec)

    # ---- 事件报告
    @staticmethod
    def write_event_csv(path: str, all_events: List[dict]) -> int:
        """所有区域事件 -> CSV（utf-8-sig）。返回事件条数。"""
        rows = []
        for rec in all_events:
            rows.append((rec.get("region", ""), rec.get("name", ""),
                         rec.get("start", ""), rec.get("end", ""),
                         f"{rec.get('duration', 0):.2f}",
                         f"{rec.get('max_ratio', 0):.2f}",
                         rec.get("frames", 0), rec.get("clip", "")))
        write_csv(path, rows,
                  header=("区域", "名称", "开始时间", "结束时间", "时长(秒)",
                          "最大变化率(%)", "帧数", "片段文件"),
                  convert=False)
        return len(rows)
