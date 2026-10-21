# -*- coding: utf-8 -*-
"""批量分析页：一次放入多个视频，按并发数同时分析，逐个独立出结果。

- 每个视频独立输出：剔除后视频 + 可选 CSV 报告（写到各自输出位置）；
- 并发数=1 时视频逐个分析（每个视频内部仍可多线程分片）；
  并发数>1 时多个视频同时分析（此时每个视频内部单线程，避免争抢 CPU）；
- 双击视频行可为**该视频单独框选分析区域**（每个视频区域各自独立）；
  未编辑的视频按全画面分析。
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDoubleSpinBox,
                               QFileDialog, QGridLayout, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QListWidget, QMessageBox,
                               QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from app.core.analyzer import AnalysisConfig, AnalyzerWorker
from app.ui.roi_editor import RoiEditorDialog
from app.version import APP_NAME

VIDEO_FILTER = ("视频文件 (*.mp4 *.avi *.mkv *.mov *.ts *.flv *.wmv *.webm);;"
                "所有文件 (*.*)")


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{stem}_{i}{ext}"):
        i += 1
    return f"{stem}_{i}{ext}"


class BatchTab(QWidget):
    """批量分析标签页。"""

    _quiet = False  # selftest 模式静默

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: List[tuple] = []          # [(row, path, cfg)]
        self._active: Dict[AnalyzerWorker, int] = {}  # worker -> row
        self._row_state: Dict[int, str] = {}   # row -> pending/running/done
        self._out_map: Dict[int, str] = {}     # row -> 输出视频路径
        self._counts = {"ok": 0, "fail": 0, "cancel": 0}
        self._rois_map: Dict[str, list] = {}   # 视频路径 -> 该视频的分析区域
        self._build_ui()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        root = QVBoxLayout(self)

        files_row = QHBoxLayout()
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        files_row.addWidget(self.file_list, 1)
        btns = QVBoxLayout()
        self.btn_add = QPushButton("添加视频…")
        self.btn_add.clicked.connect(self._on_add_files)
        self.btn_add_dir = QPushButton("添加文件夹…")
        self.btn_add_dir.clicked.connect(self._on_add_folder)
        self.btn_rm = QPushButton("移除选中")
        self.btn_rm.clicked.connect(self._on_remove)
        self.btn_clear_files = QPushButton("清空")
        self.btn_clear_files.clicked.connect(self._on_clear_files)
        for b in (self.btn_add, self.btn_add_dir, self.btn_rm, self.btn_clear_files):
            btns.addWidget(b)
        btns.addStretch(1)
        files_row.addLayout(btns)
        self.file_list.setMinimumHeight(120)
        root.addLayout(files_row, 2)

        g = QGridLayout()
        g.addWidget(QLabel("像素敏感度"), 0, 0)
        self.spin_pixel = QSpinBox()
        self.spin_pixel.setRange(0, 255)
        self.spin_pixel.setValue(25)
        g.addWidget(self.spin_pixel, 0, 1)
        g.addWidget(QLabel("有效变化阈值"), 0, 2)
        self.spin_motion = QDoubleSpinBox()
        self.spin_motion.setRange(0.01, 100.0)
        self.spin_motion.setDecimals(2)
        self.spin_motion.setValue(0.5)
        self.spin_motion.setSuffix(" %")
        g.addWidget(self.spin_motion, 0, 3)
        g.addWidget(QLabel("运动前保留帧数"), 0, 4)
        self.spin_pre = QSpinBox()
        self.spin_pre.setRange(0, 120)
        self.spin_pre.setValue(5)
        g.addWidget(self.spin_pre, 0, 5)
        g.addWidget(QLabel("运动后保留帧数"), 0, 6)
        self.spin_post = QSpinBox()
        self.spin_post.setRange(0, 120)
        self.spin_post.setValue(10)
        g.addWidget(self.spin_post, 0, 7)
        g.addWidget(QLabel("并行线程数/视频"), 1, 0)
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(0, 32)
        self.spin_threads.setValue(0)
        self.spin_threads.setToolTip("0=自动；1=单线程；>1=视频内多线程分片。\n"
                                     "并发数>1 时此设置自动改为 1（避免争抢 CPU）。")
        g.addWidget(self.spin_threads, 1, 1)
        g.addWidget(QLabel("同时分析视频数"), 1, 2)
        self.spin_conc = QSpinBox()
        self.spin_conc.setRange(1, 4)
        self.spin_conc.setValue(1)
        self.spin_conc.setToolTip("同时分析的视频个数。1=逐个分析（推荐，\n"
                                  "每个视频内部可多线程加速）；>1=多个视频同时分析。")
        g.addWidget(self.spin_conc, 1, 3)
        self.chk_csv = QCheckBox("生成 CSV 报告")
        self.chk_csv.setChecked(True)
        g.addWidget(self.chk_csv, 1, 4)
        self.chk_burn = QCheckBox("烧录原始时间戳水印")
        self.chk_burn.setChecked(True)
        g.addWidget(self.chk_burn, 1, 5)
        self.chk_out_dir = QCheckBox("输出到指定目录（不勾=保存到各视频旁）")
        self.chk_out_dir.toggled.connect(self._on_out_dir_toggled)
        g.addWidget(self.chk_out_dir, 2, 0, 1, 2)
        self.out_edit = QLineEdit()
        self.out_edit.setReadOnly(True)
        self.out_edit.setEnabled(False)
        g.addWidget(self.out_edit, 2, 2, 1, 5)
        self.btn_pick_out = QPushButton("…")
        self.btn_pick_out.setEnabled(False)
        self.btn_pick_out.setFixedWidth(34)
        self.btn_pick_out.clicked.connect(self._on_pick_out_dir)
        g.addWidget(self.btn_pick_out, 2, 7)
        for c in range(8):
            g.setColumnStretch(c, 1 if c in (1, 3, 5, 7) else 0)
        root.addLayout(g)

        run = QHBoxLayout()
        self.btn_start = QPushButton("开始批量分析")
        self.btn_start.setStyleSheet("font-weight:bold; padding:8px;")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_cancel = QPushButton("取消全部")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel_all)
        self.summary_label = QLabel("待机")
        self.btn_open_dir = QPushButton("打开输出文件夹")
        self.btn_open_dir.setEnabled(False)
        self.btn_open_dir.clicked.connect(self._on_open_dir)
        run.addWidget(self.btn_start)
        run.addWidget(self.btn_cancel)
        run.addWidget(self.summary_label, 1)
        run.addWidget(self.btn_open_dir)
        root.addLayout(run)

        tip = QLabel("双击视频行可为该视频单独框选分析区域（每个视频区域各自独立，未编辑=分析整个画面）")
        tip.setStyleSheet("color:#9a9a9a;")
        root.addWidget(tip)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["视频文件", "分析区域", "状态", "进度", "结果"])
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.setMinimumHeight(180)
        root.addWidget(self.table, 3)

    # ---------------------------------------------------------------- 文件
    def _on_add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "添加视频", "", VIDEO_FILTER)
        self._add_paths(files)

    def _on_add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "添加文件夹中的全部视频")
        if not d:
            return
        exts = (".mp4", ".avi", ".mkv", ".mov", ".ts", ".flv", ".wmv", ".webm")
        files = []
        try:
            for name in sorted(os.listdir(d)):
                if name.lower().endswith(exts):
                    files.append(os.path.join(d, name))
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"无法读取文件夹：{e}")
            return
        self._add_paths(files)

    def _add_paths(self, files: List[str]):
        existing = {self.file_list.item(i).text()
                    for i in range(self.file_list.count())}
        added = 0
        for f in files:
            if f and f not in existing and os.path.isfile(f):
                self.file_list.addItem(f)
                existing.add(f)
                added += 1
        self._rebuild_rows()

    def _on_remove(self):
        for item in sorted(self.file_list.selectedItems(),
                           key=lambda it: self.file_list.row(it), reverse=True):
            self.file_list.takeItem(self.file_list.row(item))
        self._rebuild_rows()

    def _on_clear_files(self):
        self.file_list.clear()
        self._rebuild_rows()

    def _rebuild_rows(self):
        self.table.setRowCount(self.file_list.count())
        self._row_state.clear()
        self._out_map.clear()
        # 只保留当前列表中视频的区域设置
        current = {self.file_list.item(i).text()
                   for i in range(self.file_list.count())}
        for p in list(self._rois_map):
            if p not in current:
                del self._rois_map[p]
        for i in range(self.file_list.count()):
            path = self.file_list.item(i).text()
            self.table.setItem(i, 0, QTableWidgetItem(path))
            self.table.setItem(i, 1, QTableWidgetItem(self._rois_cell(path)))
            self.table.setItem(i, 2, QTableWidgetItem("等待"))
            self.table.setItem(i, 3, QTableWidgetItem("—"))
            self.table.setItem(i, 4, QTableWidgetItem("—"))

    # ---------------------------------------------------------------- 区域
    @staticmethod
    def _rois_cell(path: str, rois_map: Optional[Dict[str, list]] = None) -> str:
        rois = (rois_map or {}).get(path)
        if rois:
            return f"已编辑 {len(rois)} 个区域"
        return "全画面"

    def _on_cell_double_clicked(self, row: int, _col: int):
        self._edit_rois(row)

    def _edit_rois(self, row: int):
        if row < 0 or row >= self.file_list.count():
            return
        if self._queue or self._active:
            QMessageBox.information(self, APP_NAME,
                                    "批量分析进行中，无法编辑分析区域。\n"
                                    "请等待完成或取消后再试。")
            return
        path = self.file_list.item(row).text()
        dlg = RoiEditorDialog(path, self._rois_map.get(path, []), self)
        if dlg.exec() and dlg.rois is not None:
            self._apply_rois_to_row(row, dlg.rois)

    def _apply_rois_to_row(self, row: int, rois: list):
        """保存某行的分析区域并刷新显示（编辑对话框确认 / 自检注入共用）。"""
        if row < 0 or row >= self.file_list.count():
            return
        path = self.file_list.item(row).text()
        if rois:
            self._rois_map[path] = [tuple(int(v) for v in r) for r in rois]
        else:
            self._rois_map.pop(path, None)
        self.table.setItem(row, 1, QTableWidgetItem(self._rois_cell(
            path, self._rois_map)))

    # ---------------------------------------------------------------- 输出目录
    def _on_out_dir_toggled(self, on: bool):
        self.out_edit.setEnabled(on)
        self.btn_pick_out.setEnabled(on)

    def _on_pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _target_dir(self, video_path: str) -> str:
        if self.chk_out_dir.isChecked() and self.out_edit.text():
            return self.out_edit.text()
        return os.path.dirname(video_path)

    # ---------------------------------------------------------------- 运行
    def _on_start(self):
        if self._queue or self._active:
            return
        n = self.file_list.count()
        if n == 0:
            QMessageBox.warning(self, APP_NAME, "请先添加要分析的视频。")
            return
        if self.chk_out_dir.isChecked() and not self.out_edit.text():
            QMessageBox.warning(self, APP_NAME, "请选择输出目录，或取消勾选"
                                "「输出到指定目录」。")
            return
        force_single_thread = self.spin_conc.value() > 1
        self._counts = {"ok": 0, "fail": 0, "cancel": 0}
        self._out_map.clear()
        self._row_state = {i: "pending" for i in range(n)}
        self._queue = []
        for i in range(n):
            path = self.file_list.item(i).text()
            tdir = self._target_dir(path)
            try:
                os.makedirs(tdir, exist_ok=True)
            except OSError:
                self._set_row(i, "✘ 无法创建输出目录", "—", "—")
                self._row_state[i] = "done"
                self._counts["fail"] += 1
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            out_mp4 = _unique_path(os.path.join(tdir, stem + "_剔除后.mp4"))
            csv_path = os.path.join(tdir, os.path.basename(out_mp4)[:-4] + "_报告.csv") \
                if self.chk_csv.isChecked() else ""
            cfg = AnalysisConfig(
                video_path=path,
                output_video_path=out_mp4,
                export_images=False,
                images_dir="",
                export_csv=bool(csv_path),
                csv_path=csv_path,
                burn_timestamp=self.chk_burn.isChecked(),
                show_preview=False,
                pixel_threshold=self.spin_pixel.value(),
                motion_percent=self.spin_motion.value(),
                pre_roll=self.spin_pre.value(),
                post_roll=self.spin_post.value(),
                rois=[tuple(int(v) for v in r)
                      for r in self._rois_map.get(path, [])],
                threads=1 if force_single_thread else self.spin_threads.value(),
                fast_mode=False,
            )
            self._out_map[i] = out_mp4
            self._queue.append((i, path, cfg))
        self.btn_open_dir.setEnabled(bool(self._out_map))
        self._set_running(True)
        self._pump()

    def _pump(self):
        conc = self.spin_conc.value()
        while self._queue and len(self._active) < conc:
            row, _path, cfg = self._queue.pop(0)
            self._row_state[row] = "running"
            w = AnalyzerWorker(cfg)
            # 注意：必须用绑定方法槽（lambda/闭包槽在本环境下收不到 worker 信号）
            w.progressChanged.connect(self._on_w_progress)
            w.statsChanged.connect(self._on_w_stats)
            w.finishedOk.connect(self._on_w_ok)
            w.canceled.connect(self._on_w_cancel)
            w.failed.connect(self._on_w_fail)
            w.finished.connect(w.deleteLater)
            self._active[w] = row
            self._set_row(row, "分析中…", "0", "—")
            w.start()
        self._update_summary()
        if not self._queue and not self._active:
            self._on_all_done()

    def _row_of_sender(self) -> Optional[int]:
        w = self._sender_worker()
        return self._active.get(w) if w is not None else None

    def _on_w_progress(self, done: int, total: int):
        row = self._row_of_sender()
        if row is not None:
            self._set_row(row, None, f"{done}/{total}", None)

    def _on_w_stats(self, kept: int, processed: int):
        row = self._row_of_sender()
        if row is not None:
            self._set_row(row, None, None, f"已保留 {kept} 帧")

    def _on_w_ok(self, res):
        row = self._row_of_sender()
        if row is None:
            return
        self._finish_job(self._sender_worker(), row)
        self._counts["ok"] += 1
        self._row_state[row] = "done"
        self._set_row(row, "✔ 完成", "—",
                      f"保留 {res.kept_frames}/{res.total_frames} 帧"
                      + (f" → {res.output_video_path}" if res.output_video_path else ""))
        self._maybe_next()

    def _on_w_cancel(self, res):
        row = self._row_of_sender()
        if row is None:
            return
        self._finish_job(self._sender_worker(), row)
        self._counts["cancel"] += 1
        self._row_state[row] = "done"
        self._set_row(row, "已取消", "—", "—")
        self._maybe_next()

    def _on_w_fail(self, msg: str):
        row = self._row_of_sender()
        if row is None:
            return
        self._finish_job(self._sender_worker(), row)
        self._counts["fail"] += 1
        self._row_state[row] = "done"
        item = self.table.item(row, 2)
        if item is not None:
            item.setToolTip(msg)
        self._set_row(row, "✘ 失败", "—", msg.splitlines()[0][:80] if msg else "")
        self._maybe_next()

    def _maybe_next(self):
        """一个任务结束后立即补位启动队列中的下一个。"""
        if self._queue:
            self._pump()
        elif not self._active:
            self._on_all_done()
        else:
            self._update_summary()

    def _set_row(self, row: int, status: Optional[str], progress: Optional[str],
                 result: Optional[str]):
        if row >= self.table.rowCount():
            return
        if status is not None:
            self.table.setItem(row, 2, QTableWidgetItem(status))
        if progress is not None:
            self.table.setItem(row, 3, QTableWidgetItem(progress))
        if result is not None:
            self.table.setItem(row, 4, QTableWidgetItem(result))

    def _sender_worker(self) -> Optional[AnalyzerWorker]:
        # 信号发射者即 worker（用 sender() 还原）
        s = self.sender()
        return s if isinstance(s, AnalyzerWorker) else None

    def _finish_job(self, w: Optional[AnalyzerWorker], row: int):
        if w is not None:
            self._active.pop(w, None)

    def _on_cancel_all(self):
        for w in list(self._active):
            w.cancel()
        for row, st in self._row_state.items():
            if st == "pending":
                self._row_state[row] = "done"
                self._counts["cancel"] += 1
                self._set_row(row, "已跳过", "—", "—")
        self._queue.clear()

    def _update_summary(self):
        total = self.file_list.count()
        done = sum(1 for s in self._row_state.values() if s == "done")
        running = len(self._active)
        self.summary_label.setText(
            f"共 {total} 个视频 · 进行中 {running} · 完成 {done}"
            f"（成功 {self._counts['ok']} / 失败 {self._counts['fail']}"
            f" / 取消 {self._counts['cancel']}）")

    def _on_all_done(self):
        self._set_running(False)
        ok, fail, cancel = self._counts["ok"], self._counts["fail"], self._counts["cancel"]
        self.summary_label.setText(
            f"批量完成：成功 {ok}，失败 {fail}，取消 {cancel}")
        self.btn_open_dir.setEnabled(bool(self._out_map))
        if not self._quiet and (fail or cancel):
            QMessageBox.warning(self, APP_NAME,
                                f"批量分析结束：成功 {ok}，失败 {fail}，取消 {cancel}。\n"
                                "失败原因可悬停在对应行的状态上查看。")

    def _set_running(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        for b in (self.btn_add, self.btn_add_dir, self.btn_rm, self.btn_clear_files,
                  self.spin_pixel, self.spin_motion, self.spin_pre, self.spin_post,
                  self.spin_threads, self.spin_conc, self.chk_csv, self.chk_burn,
                  self.chk_out_dir):
            b.setEnabled(not running)

    def _on_open_dir(self):
        if self.chk_out_dir.isChecked() and self.out_edit.text():
            d = self.out_edit.text()
        else:
            rows = sorted(self._out_map)
            d = os.path.dirname(self._out_map[rows[0]]) if rows else ""
        if d and os.path.isdir(d):
            QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    # ---------------------------------------------------------------- 生命周期
    def shutdown(self):
        """主窗口关闭时调用：取消全部任务并等待线程退出。"""
        if self._queue or self._active:
            self._on_cancel_all()
            workers = list(self._active)
            for w in workers:
                w.wait(5000)
            self._active.clear()
            self._queue.clear()
