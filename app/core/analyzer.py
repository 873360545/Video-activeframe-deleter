"""核心分析引擎。

纯逻辑部分（build_region_mask / frame_change_ratio / SegmentDecider /
run_analysis）不依赖 Qt，可独立单元测试；AnalyzerWorker 仅把 run_analysis
包装进 QThread 并通过信号回报进度。

分析区域语义：rois 为空 = 分析整个画面；非空 = 只分析选中矩形内像素
（时间数字等不变区域只需框在矩形外即可忽略）。

多线程分片模式：threads>1 时把视频按帧号切成 N 段，
第一阶段各线程用独立 VideoCapture 并行计算每帧变化率，
第二阶段全局合并决策（与单线程完全等价），
第三阶段单线程顺序写出保留帧。解码是主要耗时，多核可显著加速。
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import cv2
import numpy as np

from app.core.report import write_csv
from app.core.video_io import VideoOpenError, format_ts, fourcc, open_capture, probe_video, read_frame


# ---------------------------------------------------------------- 配置/结果
@dataclass
class AnalysisConfig:
    video_path: str
    output_video_path: str = ""          # 空 = 源文件旁默认命名
    export_images: bool = False          # 保留帧另存图片
    images_dir: str = ""
    export_csv: bool = True              # 每帧报告
    csv_path: str = ""
    burn_timestamp: bool = True          # 输出帧上烧录原始时间戳水印
    show_preview: bool = False           # GUI 实时预览（当前帧+变化标红+分析区域）
    pixel_threshold: int = 25            # 像素级敏感度 0-255
    motion_percent: float = 0.5          # 帧级变化百分比阈值
    pre_roll: int = 5                    # 运动开始前保留帧数
    post_roll: int = 10                  # 运动结束后保留帧数
    rois: list = field(default_factory=list)  # 分析区域 [(x, y, w, h), ...]，空=全帧（帧坐标）
    threads: int = 0                     # 并行线程数：0=自动，1=单线程顺序模式
    fast_mode: bool = False              # 高速模式：大分辨率视频降采样分析（默认关，可能漏检细小目标）


@dataclass
class AnalysisResult:
    ok: bool = False
    canceled: bool = False
    total_frames: int = 0                # 实际读取处理的帧数
    kept_frames: int = 0
    dropped_frames: int = 0
    output_video_path: str = ""
    csv_path: str = ""
    images_dir: str = ""
    message: str = ""


# ---------------------------------------------------------------- 纯逻辑
def build_region_mask(width: int, height: int, rois) -> np.ndarray:
    """分析区域矩形 -> uint8 掩码（255=参与分析，0=忽略）。坐标自动夹取。"""
    mask = np.zeros((height, width), dtype=np.uint8)
    for (x, y, w, h) in rois:
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(width, int(x + w)), min(height, int(y + h))
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = 255
    return mask


def frame_change_ratio(prev_gray: np.ndarray, gray: np.ndarray,
                       region_mask: Optional[np.ndarray],
                       pixel_threshold: int):
    """帧间变化统计。

    返回 (变化像素数, 变化率%, 二值变化掩码)。
    变化率% = 分析区域内变化像素 / 分析区域总像素
    （region_mask: 255=参与分析；None = 全帧参与）。
    """
    diff = cv2.absdiff(gray, prev_gray)
    if region_mask is not None:
        diff[region_mask == 0] = 0
    _, binary = cv2.threshold(diff, int(pixel_threshold), 255, cv2.THRESH_BINARY)
    changed = int(cv2.countNonZero(binary))
    if region_mask is not None:
        total = int(np.count_nonzero(region_mask))
    else:
        total = int(diff.size)
    ratio = (changed / total * 100.0) if total > 0 else 0.0
    return changed, ratio, binary


class SegmentDecider:
    """片段状态机：根据变化率判定 保留方式，处理前后缓冲。

    step() 返回：
      'start'    新运动段开始（调用方先冲刷前缓冲，再写当前帧）
      'continue' 运动段延续（写当前帧）
      'post'     后缓冲延续（写当前帧）
      'drop'     丢弃（调用方应将帧放入前缓冲环）
    """

    def __init__(self, motion_percent: float, pre_roll: int, post_roll: int):
        self.motion_percent = float(motion_percent)
        self.pre_roll = max(0, int(pre_roll))
        self.post_roll = max(0, int(post_roll))
        self.active = False
        self.post_remaining = 0

    def is_motion(self, ratio: float) -> bool:
        return ratio >= self.motion_percent

    def step(self, ratio: float) -> str:
        if self.is_motion(ratio):
            if self.active:
                self.post_remaining = self.post_roll
                return "continue"
            self.active = True
            self.post_remaining = self.post_roll
            return "start"
        if self.active and self.post_remaining > 0:
            self.post_remaining -= 1
            return "post"
        self.active = False
        self.post_remaining = 0
        return "drop"


def kept_mask_from_ratios(ratios: List[float], motion_percent: float,
                          pre_roll: int, post_roll: int) -> np.ndarray:
    """由每帧变化率计算保留掩码（与 SegmentDecider+前缓冲冲刷完全等价）。

    等价性：每个运动帧 i 保留窗口 [i-pre_roll, i+post_roll]，
    所有窗口的并集即最终保留集合（前后缓冲跨段自动合并）。
    """
    n = len(ratios)
    kept = np.zeros(n, dtype=bool)
    pre = max(0, int(pre_roll))
    post = max(0, int(post_roll))
    thr = float(motion_percent)
    for i, r in enumerate(ratios):
        if r >= thr:
            kept[max(0, i - pre):min(n, i + post + 1)] = True
    return kept


def kept_runs_from_mask(kept_mask: np.ndarray) -> List[tuple]:
    """保留掩码 -> 连续保留区间列表 [(start, end_inclusive), ...]。"""
    runs: List[tuple] = []
    n = len(kept_mask)
    i = 0
    while i < n:
        if kept_mask[i]:
            j = i
            while j + 1 < n and kept_mask[j + 1]:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    return runs


def _make_preview(frame: np.ndarray, binary: Optional[np.ndarray],
                  rois=()) -> np.ndarray:
    """实时预览图：当前视频帧（缩小）+ 变化像素标红 + 分析区域绿框。"""
    h, w = frame.shape[:2]
    scale = 420.0 / max(w, 1)
    pw = 420
    ph = max(1, int(h * scale))
    vis = cv2.resize(frame, (pw, ph), interpolation=cv2.INTER_AREA)
    if binary is not None:
        m = cv2.resize(binary, (pw, ph), interpolation=cv2.INTER_NEAREST)
        m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=1)
        vis[m > 0] = (0, 0, 255)      # 变化像素标红
    for (x, y, rw, rh) in (rois or ()):
        p1 = (int(round(x * scale)), int(round(y * scale)))
        p2 = (int(round((x + rw) * scale)), int(round((y + rh) * scale)))
        cv2.rectangle(vis, p1, p2, (90, 230, 90), 2)   # 分析区域绿框
    return vis


class _Throttle:
    """GUI 回调节流：progress / stats / preview 合并按时间发射，
    避免高帧率视频（如六万帧以上）刷爆 UI 事件队列导致界面卡死。"""

    def __init__(self, interval: float = 0.2):
        self.interval = float(interval)
        self.last = 0.0

    def due(self) -> bool:
        now = time.time()
        if now - self.last >= self.interval:
            self.last = now
            return True
        return False


def _burn_timestamp(frame: np.ndarray, frame_idx: int, fps: float) -> None:
    text = f"{format_ts(frame_idx, fps)}  F{frame_idx}"
    h = frame.shape[0]
    scale = max(0.5, h / 640.0)
    org = (12, h - 14)
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale,
                (0, 0, 0), max(2, int(scale * 3)), cv2.LINE_AA)
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale,
                (255, 255, 255), max(1, int(scale * 1.5)), cv2.LINE_AA)


def _open_writer(path: str, fps: float, width: int, height: int):
    """按扩展名优先选择编码器，失败自动回退；返回 (实际路径, writer|None)。"""
    ext = os.path.splitext(path)[1].lower()
    candidates = [(fourcc("mp4v"), ".mp4"), (fourcc("XVID"), ".avi")]
    if ext == ".avi":
        candidates.reverse()
    base = os.path.splitext(path)[0]
    for cc, cext in candidates:
        target = path if ext == cext else base + cext
        parent = os.path.dirname(os.path.abspath(target))
        os.makedirs(parent, exist_ok=True)
        writer = cv2.VideoWriter(target, cc, fps, (width, height))
        if writer.isOpened():
            return target, writer
        writer.release()
    return "", None


def _seek_accurate(path: str, idx: int) -> bool:
    """检验 CAP_PROP_POS_FRAMES seek 是否精确到帧。

    用顺序读取（read_frame 自带回退，结果可靠）取得基准帧，
    再用裸 seek+read 取对比帧，逐字节比较。解码是确定性的，应完全一致。
    """
    try:
        cap1, _ = open_capture(path)
        ok1, f1 = read_frame(cap1, idx, path)
        cap1.release()
        cap2, _ = open_capture(path)
        ok2 = cap2.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok3, f2 = cap2.read() if ok2 else (False, None)
        cap2.release()
    except Exception:  # noqa: BLE001 —— 任何异常都按"seek 不可信"处理
        return False
    if not (ok1 and f1 is not None and ok3 and f2 is not None):
        return False
    return f1.shape == f2.shape and bool(np.array_equal(f1, f2))


def _to_gray(frame: np.ndarray) -> np.ndarray:
    return cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0)


def choose_analysis_scale(width: int, height: int, fast_mode: bool) -> float:
    """高速模式：分析分辨率上限 1280px 宽（等比缩小），小视频不缩放。"""
    if not fast_mode or width <= 1280:
        return 1.0
    return 1280.0 / width


def scale_rois(rois, scale: float):
    """分析区域坐标按分析缩放比例换算。"""
    if abs(scale - 1.0) < 1e-6:
        return list(rois)
    return [(x * scale, y * scale, w * scale, h * scale) for (x, y, w, h) in rois]


def _analyze_gray(frame: np.ndarray, scale: float) -> np.ndarray:
    """解码帧 -> 分析用灰度图（按需降采样）。"""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if scale != 1.0:
        h, w = gray.shape
        gray = cv2.resize(gray, (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                          interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(gray, (5, 5), 0)


def auto_threads(frame_count: int) -> int:
    """自动线程策略：短视频（<600 帧）单线程最快（分片 seek 与建流开销不划算）；
    长视频按 600 帧/线程 递增，上限 4（ffmpeg 解码内部已自带多线程，
    外部分片过多反而因争用变慢——本机实测）。"""
    cpu = os.cpu_count() or 2
    return max(1, min(4, min(cpu, frame_count // 600 + 1)))


# ---------------------------------------------------------------- 并行分片
def _run_parallel(cfg: AnalysisConfig, info, threads: int,
                  canceled: Callable[[], bool],
                  on_progress, on_preview, on_stats) -> AnalysisResult:
    """多线程分片分析：并行算变化率 -> 全局决策 -> 顺序写出。"""
    res = AnalysisResult()
    width, height, fps = info.width, info.height, info.fps
    thr = int(cfg.pixel_threshold)
    scale = choose_analysis_scale(width, height, cfg.fast_mode)
    aw = max(1, int(round(width * scale)))
    ah = max(1, int(round(height * scale)))

    amask = build_region_mask(aw, ah, scale_rois(cfg.rois, scale)) if cfg.rois else None
    analysis_total = int(np.count_nonzero(amask)) if amask is not None else aw * ah
    if analysis_total <= 0:
        raise ValueError("分析区域为空（面积为 0）。\n请重新框选分析区域。")

    expected = max(0, int(info.frame_count))
    n_chunks = max(1, min(threads, expected // 8))
    bounds = [expected * k // n_chunks for k in range(n_chunks + 1)]

    results: List[Optional[List[float]]] = [None] * n_chunks
    lock = threading.Lock()
    state = {"done": 0, "canceled": False, "tick": _Throttle(0.2)}
    err_holder: List[str] = []

    def chunk_worker(k: int, start: int, end: int) -> None:
        try:
            cap, _ = open_capture(cfg.video_path)
        except Exception as exc:  # noqa: BLE001
            err_holder.append(str(exc))
            results[k] = []
            return
        ratios: List[float] = []
        base = None
        try:
            if start > 0:
                ok, bf = read_frame(cap, start - 1, cfg.video_path)
                if ok and bf is not None:
                    if bf.shape[0] != height or bf.shape[1] != width:
                        raise RuntimeError("视频中途分辨率变化，无法分片分析。")
                    base = _analyze_gray(bf, scale)
                if not cap.set(cv2.CAP_PROP_POS_FRAMES, start):
                    # seek 失效时重新打开、顺序快进到 start
                    cap.release()
                    cap, _ = open_capture(cfg.video_path)
                    fast_forwarded = False
                    for _ in range(start):
                        if not cap.grab():
                            fast_forwarded = True
                            break
                    if fast_forwarded:
                        results[k] = ratios
                        return
            idx = start
            while idx < end:
                if canceled():
                    state["canceled"] = True
                    break
                ok, frame = cap.read()
                if not ok:
                    break
                if frame.shape[0] != height or frame.shape[1] != width:
                    raise RuntimeError(
                        f"视频中途分辨率变化（帧 {idx}: {frame.shape[1]}×{frame.shape[0]}，"
                        f"应为 {width}×{height}），已终止分析。")
                gray = _analyze_gray(frame, scale)
                if base is None:
                    ratio, binary = 0.0, None
                else:
                    _, ratio, binary = frame_change_ratio(base, gray, amask, thr)
                base = gray
                ratios.append(ratio)
                idx += 1
                with lock:
                    state["done"] += 1
                    done_now = state["done"]
                    emit = state["tick"].due()
                if emit:  # 阶段一同样回报进度/统计/预览：长视频分析时界面实时刷新
                    if on_progress:
                        on_progress(done_now, expected * 2, ratio)
                    if on_stats:
                        on_stats(0, done_now)  # 阶段一尚未做保留决策，保留数未知
                    if on_preview and cfg.show_preview:
                        on_preview(_make_preview(frame, binary, cfg.rois))
        except Exception as exc:  # noqa: BLE001 —— 线程内异常上抛给主流程
            err_holder.append(f"{type(exc).__name__}: {exc}")
            state["canceled"] = True
        finally:
            cap.release()
            results[k] = ratios

    threads_list = [threading.Thread(target=chunk_worker, args=(k, bounds[k], bounds[k + 1]),
                                     daemon=True)
                    for k in range(n_chunks)]
    for t in threads_list:
        t.start()
    for t in threads_list:
        t.join()

    if err_holder:
        raise RuntimeError(err_holder[0])

    ratios_all: List[float] = []
    for r in results:
        ratios_all.extend(r or [])
    processed = len(ratios_all)

    if state["canceled"]:
        res.canceled = True
        res.total_frames = processed
        res.message = f"已取消。处理到第 {processed} 帧。"
        return res

    # ---- 全局合并决策（与单线程 SegmentDecider + 前缓冲冲刷等价）
    kept_mask = kept_mask_from_ratios(ratios_all, cfg.motion_percent,
                                      cfg.pre_roll, cfg.post_roll)
    csv_rows = [(i, format_ts(i, fps), round(ratios_all[i], 4), 1 if kept_mask[i] else 0)
                for i in range(processed)]

    # ---- 写出阶段
    out_path = cfg.output_video_path.strip() or os.path.splitext(cfg.video_path)[0] + "_剔除后.mp4"
    out_path, writer = _open_writer(out_path, fps, width, height)
    if writer is None:
        raise RuntimeError("无法创建输出视频写入器（编解码器不可用）。")
    res.output_video_path = out_path
    res.images_dir = cfg.images_dir if cfg.export_images else ""
    if cfg.export_images and cfg.images_dir:
        os.makedirs(cfg.images_dir, exist_ok=True)

    # ---- 写出阶段：按保留段 seek 跳跃解码，丢弃区间整段跳过（不再全片重解码）
    runs = kept_runs_from_mask(kept_mask[:processed])
    seek_trusted = _seek_accurate(cfg.video_path, min(50, processed - 1)) if processed > 1 else False
    cap, _ = open_capture(cfg.video_path)
    kept = 0
    done = state["done"]
    tick_c = _Throttle(0.2)
    try:
        cursor = 0  # 流当前位置（下一条 read 将返回的帧号）
        for (s, e) in runs:
            if canceled():
                res.canceled = True
                break
            if s > cursor:
                done += s - cursor  # 跳过的丢弃区间视为瞬时完成
            if seek_trusted:
                if not cap.set(cv2.CAP_PROP_POS_FRAMES, s):
                    seek_trusted = False  # seek 失效 → 本视频退回顺序快进
                else:
                    cursor = s
            if not seek_trusted:
                while cursor < s:
                    if not cap.grab():
                        break
                    cursor += 1
            for i in range(s, e + 1):
                ok, frame = cap.read()
                if not ok:
                    break
                cursor = i + 1
                if frame.shape[0] != height or frame.shape[1] != width:
                    raise RuntimeError(
                        f"视频中途分辨率变化（帧 {i}），已终止写出。")
                if cfg.burn_timestamp:
                    _burn_timestamp(frame, i, fps)
                writer.write(frame)
                if cfg.export_images and cfg.images_dir:
                    cv2.imwrite(os.path.join(cfg.images_dir, f"frame_{i:06d}.jpg"), frame)
                kept += 1
                done += 1
                if tick_c.due():  # 写出阶段同样实时刷新界面
                    if on_progress:
                        on_progress(done, expected * 2, ratios_all[i])
                    if on_stats:
                        on_stats(kept, done)
                    if on_preview and cfg.show_preview:
                        on_preview(_make_preview(frame, None, cfg.rois))
            if res.canceled:
                break
    finally:
        cap.release()
        writer.release()

    res.total_frames = processed
    res.kept_frames = kept
    res.dropped_frames = processed - kept
    if cfg.export_csv and cfg.csv_path:
        write_csv(cfg.csv_path, csv_rows)
        res.csv_path = cfg.csv_path
    res.ok = not res.canceled and processed > 0
    drop_pct = (res.dropped_frames / res.total_frames * 100.0) if res.total_frames else 0.0
    if res.canceled:
        res.message = f"已取消。处理到第 {done} 帧，保留 {kept} 帧（未保存输出）。"
    elif processed == 0:
        res.ok = False
        res.message = "没有读取到任何视频帧，请检查视频文件。"
    else:
        res.message = (f"分析完成：共 {res.total_frames} 帧，保留 {res.kept_frames} 帧，"
                       f"剔除 {res.dropped_frames} 帧（{drop_pct:.1f}%）。")
    return res


# ---------------------------------------------------------------- 主流程
def run_analysis(cfg: AnalysisConfig,
                 on_progress: Optional[Callable[[int, int, float], None]] = None,
                 on_preview: Optional[Callable[[np.ndarray], None]] = None,
                 on_stats: Optional[Callable[[int, int], None]] = None,
                 is_canceled: Optional[Callable[[], bool]] = None) -> AnalysisResult:
    """执行完整分析。异常向上抛出；取消时返回 canceled=True 的部分结果。

    threads 解析：cfg.threads>0 直接使用；0（自动）= auto_threads(帧数)
    ——短视频单线程最快（分片 seek 与建流开销不划算），长视频按 600 帧/线程
    递增（上限 4）。线程数>1 且总帧数≥64 时启用多线程分片。
    写出阶段按保留段 seek 跳跃解码，只解码要保留的部分。
    """
    canceled = is_canceled or (lambda: False)
    info = probe_video(cfg.video_path)

    n_threads = int(cfg.threads) if cfg.threads and cfg.threads > 0 else auto_threads(info.frame_count)
    if n_threads > 1 and info.frame_count >= 64:
        return _run_parallel(cfg, info, n_threads, canceled, on_progress, on_preview, on_stats)

    return _run_sequential(cfg, info, canceled, on_progress, on_preview, on_stats)


def _run_sequential(cfg: AnalysisConfig, info,
                    canceled: Callable[[], bool],
                    on_progress, on_preview, on_stats) -> AnalysisResult:
    """单线程顺序模式（流式处理，内存占用最小）。"""
    res = AnalysisResult()
    width, height, fps = info.width, info.height, info.fps
    scale = choose_analysis_scale(width, height, cfg.fast_mode)

    amask = build_region_mask(max(1, int(round(width * scale))),
                              max(1, int(round(height * scale))),
                              scale_rois(cfg.rois, scale)) if cfg.rois else None
    analysis_total = int(np.count_nonzero(amask)) if amask is not None else width * height
    if analysis_total <= 0:
        raise ValueError("分析区域为空（面积为 0）。\n请重新框选分析区域。")

    out_path = cfg.output_video_path.strip() or os.path.splitext(cfg.video_path)[0] + "_剔除后.mp4"
    out_path, writer = _open_writer(out_path, fps, width, height)
    if writer is None:
        raise RuntimeError("无法创建输出视频写入器（编解码器不可用）。")

    if cfg.export_images and cfg.images_dir:
        os.makedirs(cfg.images_dir, exist_ok=True)

    cap, _ = open_capture(cfg.video_path)

    ring: deque = deque(maxlen=max(0, int(cfg.pre_roll)))  # (idx, frame) 前缓冲
    decider = SegmentDecider(cfg.motion_percent, cfg.pre_roll, cfg.post_roll)
    csv_rows = []
    prev_gray = None
    kept = 0
    processed = 0
    expected = max(0, info.frame_count)
    tick = _Throttle(0.2)
    res.output_video_path = out_path
    res.images_dir = cfg.images_dir if cfg.export_images else ""

    try:
        while True:
            if canceled():
                res.canceled = True
                break
            ok, frame = cap.read()
            if not ok:
                break  # EOF 或不可读帧：结束
            if frame.shape[0] != height or frame.shape[1] != width:
                raise RuntimeError(
                    f"视频中途分辨率变化（帧 {processed}: {frame.shape[1]}×{frame.shape[0]}，"
                    f"应为 {width}×{height}），已终止分析。")
            frame_idx = processed
            gray = _analyze_gray(frame, scale)
            if prev_gray is None:
                ratio = 0.0
                binary = None
            else:
                _, ratio, binary = frame_change_ratio(prev_gray, gray, amask, cfg.pixel_threshold)
            prev_gray = gray

            verdict = decider.step(ratio)
            keep = verdict != "drop"

            def emit(idx: int, fr: np.ndarray) -> None:
                nonlocal kept
                if cfg.burn_timestamp:
                    _burn_timestamp(fr, idx, fps)
                if writer is not None:
                    writer.write(fr)
                if cfg.export_images and cfg.images_dir:
                    cv2.imwrite(os.path.join(cfg.images_dir, f"frame_{idx:06d}.jpg"), fr)
                kept += 1

            if keep:
                if verdict == "start":
                    for bi, bf in ring:
                        emit(bi, bf)
                        if 0 <= bi < len(csv_rows):  # 回写前缓冲帧的保留标记，保持 CSV 与输出一致
                            r0 = csv_rows[bi]
                            csv_rows[bi] = (r0[0], r0[1], r0[2], 1)
                    ring.clear()
                emit(frame_idx, frame)
            elif cfg.pre_roll > 0:
                ring.append((frame_idx, frame.copy()))

            csv_rows.append((frame_idx, format_ts(frame_idx, fps),
                             round(ratio, 4), 1 if keep else 0))
            processed += 1

            if tick.due():  # 进度/统计/预览统一按时间节流，界面始终实时刷新
                if on_progress:
                    on_progress(processed, expected, ratio)
                if on_stats:
                    on_stats(kept, processed)
                if on_preview and cfg.show_preview:
                    on_preview(_make_preview(frame, binary, cfg.rois))

        res.total_frames = processed
        res.kept_frames = kept
        res.dropped_frames = processed - kept
        if cfg.export_csv and cfg.csv_path:
            write_csv(cfg.csv_path, csv_rows)
            res.csv_path = cfg.csv_path
        res.ok = not res.canceled and processed > 0
        drop_pct = (res.dropped_frames / res.total_frames * 100.0) if res.total_frames else 0.0
        if res.canceled:
            res.message = f"已取消。处理到第 {processed} 帧，保留 {kept} 帧（未保存输出）。"
        elif processed == 0:
            res.ok = False
            res.message = "没有读取到任何视频帧，请检查视频文件。"
        else:
            res.message = (f"分析完成：共 {res.total_frames} 帧，保留 {res.kept_frames} 帧，"
                           f"剔除 {res.dropped_frames} 帧（{drop_pct:.1f}%）。")
    finally:
        cap.release()
        if writer is not None:
            writer.release()

    return res


# ---------------------------------------------------------------- Qt 线程包装
try:  # 延迟导入 Qt：纯逻辑单元测试环境即使缺 Qt 也不受影响
    from PySide6.QtCore import QThread, Signal

    class AnalyzerWorker(QThread):
        progressChanged = Signal(int, int, float)   # processed, total, 当前变化率
        previewReady = Signal(object)               # np.ndarray BGR 预览图
        statsChanged = Signal(int, int)             # kept, processed
        finishedOk = Signal(object)                 # AnalysisResult
        canceled = Signal(object)                   # AnalysisResult（部分结果）
        failed = Signal(str)

        def __init__(self, cfg: AnalysisConfig, parent=None):
            super().__init__(parent)
            self._cfg = cfg
            self._cancel = False

        def cancel(self) -> None:
            self._cancel = True

        def run(self) -> None:
            try:
                result = run_analysis(
                    self._cfg,
                    on_progress=lambda p, t, r: self.progressChanged.emit(p, t, r),
                    on_preview=(lambda img: self.previewReady.emit(img)),
                    on_stats=lambda kept, proc: self.statsChanged.emit(kept, proc),
                    is_canceled=lambda: self._cancel,
                )
                if result.canceled:
                    self.canceled.emit(result)
                else:
                    self.finishedOk.emit(result)
            except Exception as exc:  # noqa: BLE001 —— 统一回报到 UI
                self.failed.emit(f"{type(exc).__name__}: {exc}")
except ImportError:  # pragma: no cover - 无 Qt 环境下仅提供纯逻辑
    AnalyzerWorker = None  # type: ignore[assignment]
