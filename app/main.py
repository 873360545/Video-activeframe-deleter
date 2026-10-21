"""应用入口。

正常启动：  python -m app.main   （或 python main.py / run_dev.bat）
自检模式：  python -m app.main --selftest
            无交互跑通 GUI 装配 + 完整分析管线 + 结果保存/放弃流程，
            并验证「分析完成后可再次开始分析」。用于自动化验证。

扩展诊断：  设置环境变量 MONITOR_SELFTEST_VIDEO=<视频路径> 时，GUI 自检通过后
            会再用核心分析管线跑一遍该视频（验证打包环境内的解码能力）；
            MONITOR_SELFTEST_ROIS="x,y,w,h;..." 可选指定分析区域。
"""
from __future__ import annotations

import os
import sys
import tempfile


def _run_selftest(app) -> None:
    import time as _time
    import traceback

    from PySide6.QtCore import QTimer

    from app.ui.main_window import MainWindow
    from tools.make_test_video import make_test_video

    app.setQuitOnLastWindowClosed(False)  # 窗口意外关闭时也能看到诊断日志

    video = os.path.join(tempfile.gettempdir(), "monitor_selftest_e2e.mp4")
    if not os.path.exists(video):
        make_test_video(video)

    win = MainWindow()
    win._quiet = True
    win.show()
    win.load_video(video)
    win.canvas.setRois([(0, 100, 640, 380)])  # 分析区域：盖住两段运动、把时间数字留在框外

    t0 = _time.monotonic()

    def _ts() -> str:
        return f"[{_time.monotonic() - t0:6.1f}s]"

    state: dict = {"round": 0, "res1": None, "res2": None, "err": "", "saved": [],
                   "log": []}

    def log(msg: str):
        state["log"].append(f"{_ts()} {msg}")

    # 回归检查：视频帧必须真实绘制在画布上（曾出过 paintEvent 漏画帧导致画布全黑的 bug）
    win._show_frame(150)  # 运动段帧，画面内有移动方块，像素必有差异
    for _ in range(10):
        app.processEvents()
    img = win.canvas.grab().toImage()
    samples = [img.pixelColor(int(img.width() * fx), int(img.height() * fy)).value()
               for fx, fy in ((0.3, 0.4), (0.5, 0.5), (0.7, 0.6), (0.5, 0.2), (0.5, 0.8))]
    log(f"canvas samples={samples}")
    if max(samples) == min(samples):
        for line in state["log"]:
            print(f"  LOG: {line}", flush=True)
        print(f"SELFTEST_FAILED: 画布未绘制视频帧（采样像素全相同 {samples}）", flush=True)
        os._exit(1)

    def guarded(fn):
        def wrap(*a):
            try:
                return fn(*a)
            except Exception:
                state["err"] = traceback.format_exc()
                log(f"EXC in {getattr(fn, '__name__', fn)}")
                app.quit()
        return wrap

    def start_analysis():
        log(f"start_analysis round={state['round']}")
        win._on_start()
        w = win._worker
        if w is None:
            state["err"] = "无法启动分析线程"
            app.quit()
            return
        if state["round"] == 1:
            state["await2"] = True
        log(f"worker started isRunning={w.isRunning()}")

    def poll_tick():
        """轮询 GUI 状态驱动 selftest（不依赖 worker 信号到普通闭包的投递）。"""
        phase = state.get("phase", "video")
        if phase == "video":
            if win._worker is not None:
                return  # 正在分析
            lr = win._last_result
            if state["round"] == 0 and win._result_active and lr is not None:
                state["res1"] = lr
                state["round"] = 1
                log(f"poll: round1 done kept={lr.kept_frames}")
                QTimer.singleShot(50, guarded(after_first))
            elif state.get("await2") and win._result_active and lr is not None:
                state["res2"] = lr
                state["await2"] = False
                log(f"poll: round2 done kept={lr.kept_frames}")
                # 回归（v1.4.1）：分析完不保存、直接换视频 → 不得遗留旧分析区域
                try:
                    win.load_video(video)
                except Exception:
                    state["err"] = "换视频重载失败: " + traceback.format_exc()
                    app.quit()
                    return
                if win._result_active or win.canvas.rois() or win.roi_list.count():
                    state["err"] = (f"换视频后状态未重置: result_active={win._result_active} "
                                    f"rois={win.canvas.rois()} list={win.roi_list.count()}")
                    app.quit()
                    return
                log("reload clean: result_mode=False rois=[] list=[]")
                state["phase"] = "batch"
                QTimer.singleShot(50, guarded(start_batch_phase))
        elif phase == "batch":
            bt = win.batch_tab
            if bt._queue or bt._active:
                return
            if state.get("batch_started") and bt._row_state and \
                    all(v == "done" for v in bt._row_state.values()):
                state["phase"] = "live"
                QTimer.singleShot(50, guarded(start_live_phase))
        elif phase == "live":
            lt = win.live_tab
            if lt._monitor is None:
                # 停止完成（_on_stopped 已跑）：不依赖 stoppedLive 队列投递，直接评估
                if not state.get("live_finished"):
                    state["live_finished"] = True
                    log("live: monitor gone, evaluating directly")
                    finish_live()
                return
            if not state.get("live_stop_started") and \
                    _time.monotonic() >= state["live_deadline"]:
                state["live_stop_started"] = True
                log("live: requesting stop")
                lt._on_stop()

    # ---- 批量分析阶段
    def start_batch_phase():
        import shutil
        state["batch_started"] = True
        log("batch: starting")
        src = os.path.join(tempfile.gettempdir(), "monitor_selftest_e2e.mp4")
        b = os.path.join(tempfile.gettempdir(), "monitor_selftest_e2e_b.mp4")
        shutil.copy2(src, b)
        out = tempfile.mkdtemp(prefix="selftest_batch_")
        state["batch_out"] = out
        state["batch_files"] = [src, b]
        bt = win.batch_tab
        bt._quiet = True
        bt._on_clear_files()
        bt.chk_out_dir.setChecked(True)
        bt.out_edit.setText(out)
        bt._add_paths([src, b])
        # 每视频独立分析区域（v1.5）：只给第一个视频设置，第二个保持全画面
        bt._apply_rois_to_row(0, [(0, 100, 640, 380)])
        if bt.table.item(0, 1).text() != "已编辑 1 个区域" or \
                bt.table.item(1, 1).text() != "全画面":
            state["err"] = "批量分析区域列显示不正确"
            app.quit()
            return
        log("batch: per-video rois injected")
        bt._on_start()
        log(f"batch: {bt.file_list.count()} files queued conc={bt.spin_conc.value()}")

    # ---- 实时监控阶段（真实屏幕捕获）
    def start_live_phase():
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QWidget

        log("live: starting")
        scr = QGuiApplication.primaryScreen()
        dpr = scr.devicePixelRatio() or 1.0
        probe = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        probe.setFixedSize(200, 150)
        probe.move(150, 150)
        probe.show()
        state["probe"] = probe
        state["probe_on"] = False

        def flip():
            state["probe_on"] = not state["probe_on"]
            probe.setStyleSheet("background:#ffffff;" if state["probe_on"]
                                else "background:#000000;")

        ft = QTimer()
        ft.setInterval(120)
        ft.timeout.connect(flip)
        ft.start()
        state["flip_timer"] = ft

        lt = win.live_tab
        lt._quiet = True
        lt.chk_merge.setChecked(True)   # v1.6：验证合并输出
        # 物理像素矩形：逻辑 (120,120,420,320) × dpr，完全盖住探针控件
        lt._rects = [(int(120 * dpr), int(120 * dpr),
                      int(420 * dpr), int(320 * dpr))]
        # v1.7：分析子区域=监控区域右下 1/4（黑白翻转中心在此，仍应触发事件）
        lt._analysis_rects = [(int(330 * dpr), int(280 * dpr),
                               int(210 * dpr), int(160 * dpr))]
        lt._update_region_label()
        win.tabs.setCurrentWidget(lt)
        lt._on_start()
        m = lt._monitor
        if m is None:
            state["err"] = "实时监控线程未能启动"
            app.quit()
            return
        m.stoppedLive.connect(guarded(finish_live))
        m.failed.connect(guarded(live_failed))
        state["live_deadline"] = _time.monotonic() + 3.0
        state["live_stop_started"] = False

    def live_failed(msg: str):
        state["err"] = f"实时监控失败: {msg}"
        log(state["err"])
        try:
            state["flip_timer"].stop()
            state["probe"].close()
        except Exception:
            pass
        app.quit()

    def finish_live():
        lt = win.live_tab
        events = list(lt._events)
        clips = [e.get("clip") for e in events if e.get("clip")]
        merged = []
        if lt._out_dir and os.path.isdir(lt._out_dir):
            merged = [f for f in os.listdir(lt._out_dir)
                      if f.endswith(".mp4") and "_合并_" in f]
        pix = lt._tiles[0].preview.pixmap() if lt._tiles else None
        state["live_events"] = len(events)
        state["live_clips"] = len(clips)
        state["live_merged"] = len(merged)
        state["live_preview"] = pix is not None
        log(f"live: events={len(events)} clips={len(clips)} merged={len(merged)} "
            f"preview={'Y' if pix else 'N'} out_dir={lt._out_dir}")
        try:
            state["flip_timer"].stop()
            state["probe"].close()
        except Exception:
            pass
        app.quit()

    def after_first():
        log("after_first")
        if win._worker is not None:
            state["err"] = "分析结束后 _worker 未清空（无法再次分析）"
            app.quit()
            return
        if not win._result_active:
            state["err"] = "分析完成后未进入结果预览模式"
            app.quit()
            return
        save_dir = tempfile.mkdtemp(prefix="selftest_save_")
        saved = win._save_result_to(os.path.join(save_dir, "self.mp4"))
        state["saved"] = saved
        win._on_discard_result()
        if win._result_active or (win._temp_dir and os.path.isdir(win._temp_dir)):
            state["err"] = "放弃结果后未退出结果模式或未清理临时目录"
            app.quit()
            return
        QTimer.singleShot(50, guarded(start_analysis))  # 第二轮：验证可重新分析

    def heartbeat():
        w = win._worker
        log(f"alive worker={'Y' if w else 'N'} "
            f"running={w.isRunning() if w else '-'} result_mode={win._result_active} "
            f"stats={win.stats_label.text()}")
        if state.get("phase") == "batch":
            bt = win.batch_tab
            log(f"batch rows={dict(bt._row_state)} q={len(bt._queue)} "
                f"act={len(bt._active)} counts={dict(bt._counts)}")
        elif state.get("phase") == "live":
            lt = win.live_tab
            m = lt._monitor
            tiles = [t.status.text() for t in lt._tiles] if lt._tiles else []
            log(f"live mon={'Y' if m else 'N'} events={len(lt._events)} "
                f"tiles={tiles}")

    def timeout():
        if not state["err"]:
            state["err"] = "selftest 超时"
        log("timeout")
        app.quit()

    poll = QTimer()
    poll.setInterval(150)
    poll.timeout.connect(guarded(poll_tick))
    poll.start()

    hb = QTimer()
    hb.setInterval(20000)
    hb.timeout.connect(heartbeat)
    hb.start()

    QTimer.singleShot(300, guarded(start_analysis))
    _timeout_s = int(os.environ.get("MONITOR_SELFTEST_TIMEOUT", "150"))
    QTimer.singleShot(_timeout_s * 1000, timeout)
    app.exec()

    hb.stop()
    log(f"exec returned visible={win.isVisible()} result_mode={win._result_active}")
    for line in state["log"]:
        print(f"  LOG: {line}", flush=True)
    if state["err"]:
        print(f"SELFTEST_FAILED: {state['err']}", flush=True)
        os._exit(1)

    r1, r2 = state["res1"], state["res2"]
    saved = state["saved"]
    ok = (r1.ok and r2.ok and r1.total_frames == 300
          and 100 <= r1.kept_frames <= 112 and r1.kept_frames == r2.kept_frames
          and len(saved) >= 1 and all(os.path.exists(p) for p in saved))
    print(f"SELFTEST_{'OK' if ok else 'FAILED'}: "
          f"round1 total={r1.total_frames} kept={r1.kept_frames} | "
          f"round2 kept={r2.kept_frames} | saved={len(saved)} 文件 | msg={r2.message}")
    for p in saved:
        print(f"  SAVED: {p}", flush=True)
    if not ok or state["err"]:
        if state["err"] and not ok:
            print(f"SELFTEST_FAILED: {state['err']}", flush=True)
        os._exit(1)

    # ---- 批量分析阶段结果
    batch_ok = False
    batch_outs = []
    for f in state.get("batch_files", []):
        stem = os.path.splitext(os.path.basename(f))[0] + "_剔除后.mp4"
        batch_outs.append(os.path.join(state.get("batch_out", ""), stem))
    batch_outs = [p for p in batch_outs if p]
    batch_ok = bool(batch_outs) and all(
        os.path.isfile(p) and os.path.getsize(p) > 0 for p in batch_outs)
    bt = win.batch_tab
    batch_ok = batch_ok and bt._counts["ok"] == len(batch_outs)
    print(f"BATCH_{'OK' if batch_ok else 'FAILED'}: outputs={len(batch_outs)} "
          f"counts={bt._counts}", flush=True)
    for p in batch_outs:
        print(f"  BATCH_OUT: {p}", flush=True)

    # ---- 实时监控阶段结果
    live_ok = (state.get("live_events", 0) >= 1
               and state.get("live_clips", 0) >= 1
               and state.get("live_merged", 0) >= 1
               and state.get("live_preview", False))
    print(f"LIVE_{'OK' if live_ok else 'FAILED'}: "
          f"events={state.get('live_events', 0)} "
          f"clips={state.get('live_clips', 0)} "
          f"merged={state.get('live_merged', 0)} "
          f"preview={'Y' if state.get('live_preview') else 'N'}", flush=True)
    if not batch_ok:
        os._exit(1)
    if not live_ok:
        os._exit(1)

    # ---- 外部视频诊断（如设置了 MONITOR_SELFTEST_VIDEO）----
    ext = os.environ.get("MONITOR_SELFTEST_VIDEO", "")
    if ext and os.path.isfile(ext):
        from app.core.analyzer import AnalysisConfig, run_analysis

        rois = []
        for part in os.environ.get("MONITOR_SELFTEST_ROIS", "").split(";"):
            part = part.strip()
            if part:
                x, y, w, h = (int(v) for v in part.split(","))
                rois.append((x, y, w, h))
        out_dir = tempfile.mkdtemp(prefix="extdiag_")
        cfg = AnalysisConfig(video_path=ext, output_video_path=os.path.join(out_dir, "out.mp4"),
                             export_csv=False, burn_timestamp=True, rois=rois)
        res = run_analysis(cfg)
        pct = res.kept_frames * 100 // max(1, res.total_frames)
        print(f"EXTDIAG_{'OK' if res.ok else 'FAILED'}: video={os.path.basename(ext)} "
              f"total={res.total_frames} kept={res.kept_frames} ({pct}%) msg={res.message}",
              flush=True)
        if not res.ok:
            os._exit(1)
    os._exit(0)


def main() -> None:
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from app.version import APP_NAME

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setFont(QFont("Microsoft YaHei UI", 9))

    if "--selftest" in sys.argv[1:]:
        _run_selftest(app)
        return

    from app.ui.main_window import MainWindow

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
