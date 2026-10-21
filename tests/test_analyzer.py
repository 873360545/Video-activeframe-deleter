"""核心算法单元测试 + 端到端分析测试。"""
import csv as csvmod
import os
import sys
import unittest

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core.analyzer import (AnalysisConfig, SegmentDecider, auto_threads,
                               build_region_mask, choose_analysis_scale,
                               frame_change_ratio, kept_runs_from_mask,
                               run_analysis, scale_rois, _seek_accurate)
from tools.make_test_video import make_test_video

TMP = os.path.join(ROOT, "tests", "tmp")


class TestMaskAndRatio(unittest.TestCase):
    def test_build_region_mask(self):
        m = build_region_mask(100, 80, [(10, 10, 30, 20)])
        self.assertEqual(m.shape, (80, 100))
        self.assertEqual(m[15, 20], 255)   # 矩形内 = 参与分析
        self.assertEqual(m[0, 0], 0)       # 矩形外 = 忽略
        self.assertEqual(m[70, 90], 0)

    def test_build_region_mask_clamps(self):
        m = build_region_mask(100, 80, [(-5, -5, 20, 20), (90, 70, 50, 50)])
        self.assertEqual(m[0, 0], 255)
        self.assertEqual(m[79, 99], 255)

    def test_frame_change_ratio_full(self):
        prev = np.zeros((100, 100), np.uint8)
        cur = prev.copy()
        cur[0:10, 0:10] = 200  # 100 像素变化
        changed, ratio, _ = frame_change_ratio(prev, cur, None, 25)
        self.assertEqual(changed, 100)
        self.assertAlmostEqual(ratio, 1.0)

    def test_frame_change_ratio_outside_region(self):
        """变化发生在分析区域之外时不计入。"""
        prev = np.zeros((100, 100), np.uint8)
        cur = prev.copy()
        cur[0:10, 0:10] = 200
        amask = np.zeros((100, 100), np.uint8)
        amask[40:60, 40:60] = 255          # 分析区域在别处
        changed, ratio, _ = frame_change_ratio(prev, cur, amask, 25)
        self.assertEqual(changed, 0)
        self.assertEqual(ratio, 0.0)

    def test_frame_change_ratio_inside_region(self):
        """只有分析区域内的像素参与统计：区域内变化按区域面积算占比。"""
        prev = np.zeros((100, 100), np.uint8)
        cur = prev.copy()
        cur[40:50, 40:50] = 200            # 区域内 100 像素变化
        amask = np.zeros((100, 100), np.uint8)
        amask[40:60, 40:60] = 255          # 分析区域 20x20=400 像素
        changed, ratio, _ = frame_change_ratio(prev, cur, amask, 25)
        self.assertEqual(changed, 100)
        self.assertAlmostEqual(ratio, 25.0)

    def test_frame_change_ratio_below_pixel_threshold(self):
        prev = np.zeros((100, 100), np.uint8)
        cur = prev.copy()
        cur[0, 0] = 20
        changed, _, _ = frame_change_ratio(prev, cur, None, 25)
        self.assertEqual(changed, 0)


class TestSegmentDecider(unittest.TestCase):
    def test_basic_segment(self):
        d = SegmentDecider(0.5, pre_roll=2, post_roll=3)
        v = [d.step(r) for r in [0.0, 0.0, 0.9, 0.9, 0.0, 0.0, 0.0, 0.0, 0.0]]
        self.assertEqual(v, ["drop", "drop", "start", "continue",
                             "post", "post", "post", "drop", "drop"])

    def test_post_extends_on_retrigger(self):
        d = SegmentDecider(0.5, 0, 2)
        v = [d.step(r) for r in [0.6, 0.0, 0.0, 0.7, 0.0, 0.0, 0.0]]
        self.assertEqual(v, ["start", "post", "post", "continue",
                             "post", "post", "drop"])

    def test_zero_rolls(self):
        d = SegmentDecider(0.5, 0, 0)
        v = [d.step(r) for r in [0.6, 0.0, 0.6, 0.0]]
        self.assertEqual(v, ["start", "drop", "start", "drop"])


class TestKeptRunsAndHelpers(unittest.TestCase):
    def test_kept_runs(self):
        m = np.array([0, 1, 1, 0, 0, 1, 1, 1, 0, 1], dtype=bool)
        self.assertEqual(kept_runs_from_mask(m), [(1, 2), (5, 7), (9, 9)])

    def test_kept_runs_empty_and_full(self):
        self.assertEqual(kept_runs_from_mask(np.zeros(5, bool)), [])
        self.assertEqual(kept_runs_from_mask(np.ones(5, bool)), [(0, 4)])

    def test_auto_threads(self):
        self.assertEqual(auto_threads(300), 1)
        self.assertGreaterEqual(auto_threads(3000), 2)
        self.assertLessEqual(auto_threads(900000), 4)

    def test_analysis_scale(self):
        self.assertEqual(choose_analysis_scale(640, 480, True), 1.0)
        self.assertEqual(choose_analysis_scale(1920, 1080, False), 1.0)
        self.assertAlmostEqual(choose_analysis_scale(1920, 1080, True), 1280 / 1920)
        self.assertEqual(scale_rois([(10, 20, 30, 40)], 1.0), [(10, 20, 30, 40)])
        x, y, w, h = scale_rois([(10, 20, 30, 40)], 0.5)[0]
        self.assertAlmostEqual((x, y, w, h), (5.0, 10.0, 15.0, 20.0))

    def test_seek_accurate_e2e(self):
        os.makedirs(TMP, exist_ok=True)
        video = os.path.join(TMP, "e2e.mp4")
        if not os.path.exists(video):
            make_test_video(video)
        self.assertTrue(_seek_accurate(video, 100))


class TestFastModeEquivalence(unittest.TestCase):
    """高速模式（降采样分析）在明显运动场景下必须给出与全分辨率一致的保留结果。"""

    @classmethod
    def setUpClass(cls):
        os.makedirs(TMP, exist_ok=True)
        path = os.path.join(TMP, "hd.mp4")
        if not os.path.exists(path):
            fps, n, w, h = 25.0, 90, 1920, 1080
            vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
            for i in range(n):
                frame = np.full((h, w, 3), 40, np.uint8)
                if 30 <= i < 55:  # 中段出现大块移动方块
                    x = (i - 30) * 30
                    cv2.rectangle(frame, (x, 400), (x + 300, 700), (200, 90, 60), -1)
                vw.write(frame)
            vw.release()
        cls.video = path

    def _run(self, fast, name):
        cfg = AnalysisConfig(
            video_path=self.video,
            output_video_path=os.path.join(TMP, name + ".mp4"),
            export_csv=False, burn_timestamp=False,
            pixel_threshold=25, motion_percent=0.5,
            pre_roll=2, post_roll=3, rois=[], threads=1, fast_mode=fast)
        return run_analysis(cfg)

    def test_fast_matches_full(self):
        r_full = self._run(False, "hd_full")
        r_fast = self._run(True, "hd_fast")
        self.assertTrue(r_full.ok)
        self.assertTrue(r_fast.ok)
        self.assertEqual(r_full.total_frames, 90)
        self.assertEqual(r_fast.total_frames, 90)
        self.assertEqual(r_fast.kept_frames, r_full.kept_frames)
        self.assertGreater(r_fast.kept_frames, 20)


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(TMP, exist_ok=True)
        cls.video = os.path.join(TMP, "e2e.mp4")
        make_test_video(cls.video)

    def _cfg(self, name, rois, **kw):
        defaults = dict(
            video_path=self.video,
            output_video_path=os.path.join(TMP, name + ".mp4"),
            export_csv=True, csv_path=os.path.join(TMP, name + ".csv"),
            export_images=False,
            pixel_threshold=25, motion_percent=0.35,
            pre_roll=5, post_roll=10,
            rois=rois, burn_timestamp=False)
        defaults.update(kw)
        return AnalysisConfig(**defaults)

    def test_with_and_without_roi(self):
        # 分析区域：盖住两段运动、把时间数字留在框外
        roi = [(0, 100, 640, 380)]
        res_roi = run_analysis(self._cfg("out_roi", roi))
        res_all = run_analysis(self._cfg("out_all", []))

        # 有分析区域：只保留两个运动段（60+16 帧运动 + 前后缓冲），时间数字被忽略
        self.assertTrue(res_roi.ok)
        self.assertEqual(res_roi.total_frames, 300)
        self.assertGreaterEqual(res_roi.kept_frames, 100)
        self.assertLessEqual(res_roi.kept_frames, 112)
        self.assertTrue(os.path.exists(res_roi.output_video_path))

        # 无分析区域：每秒一次的时间数字变化额外触发保留，保留数应显著更多
        self.assertGreater(res_all.kept_frames, res_roi.kept_frames + 5)

        # 输出视频帧数 == 保留帧数
        cap = cv2.VideoCapture(res_roi.output_video_path)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        self.assertEqual(n, res_roi.kept_frames)

        # CSV：300 行数据，保留行数与结果一致
        with open(res_roi.csv_path, encoding="utf-8-sig") as f:
            rows = list(csvmod.reader(f))
        self.assertEqual(len(rows) - 1, 300)
        kept_rows = sum(1 for r in rows[1:] if r[3] == "是")
        self.assertEqual(kept_rows, res_roi.kept_frames)

    def test_parallel_matches_sequential(self):
        """多线程分片模式必须与单线程顺序模式得到完全一致的保留结果。"""
        r_seq = run_analysis(self._cfg("out_par_seq", [(0, 100, 640, 380)], threads=1))
        r_par = run_analysis(self._cfg("out_par_par", [(0, 100, 640, 380)], threads=4))
        self.assertTrue(r_seq.ok)
        self.assertTrue(r_par.ok)
        self.assertEqual(r_par.total_frames, r_seq.total_frames)
        self.assertEqual(r_par.kept_frames, r_seq.kept_frames)
        # 输出内容一致性：两种模式写出的帧必须一一对应（防 seek 错帧）
        cap_a = cv2.VideoCapture(r_seq.output_video_path)
        cap_b = cv2.VideoCapture(r_par.output_video_path)
        idx = 0
        while True:
            ok_a, fa = cap_a.read()
            ok_b, fb = cap_b.read()
            self.assertEqual(ok_a, ok_b, f"输出帧数不一致 @ {idx}")
            if not ok_a:
                break
            diff = float(np.mean(cv2.absdiff(fa, fb)))
            self.assertLess(diff, 5.0, f"输出第 {idx} 帧内容不匹配 (mean diff={diff:.2f})")
            idx += 1
        cap_a.release()
        cap_b.release()

    def test_cancel(self):
        counter = {"n": 0}

        def cancel():
            counter["n"] += 1
            return counter["n"] > 40

        res = run_analysis(self._cfg("out_cancel", [(0, 100, 640, 380)]),
                           is_canceled=cancel)
        self.assertTrue(res.canceled)
        self.assertLess(res.total_frames, 300)

    def test_full_frame_roi_equals_none(self):
        """全画面分析区域与不设区域应等价（校验语义反转正确）。"""
        r_full = run_analysis(self._cfg("out_fullroi", [(0, 0, 640, 480)]))
        r_none = run_analysis(self._cfg("out_noroi", []))
        self.assertTrue(r_full.ok)
        self.assertEqual(r_full.kept_frames, r_none.kept_frames)

    def test_zero_area_region_rejected(self):
        """分析区域完全在画面之外时必须报错而非静默返回。"""
        cfg = self._cfg("out_zero", [(700, 700, 10, 10)])
        with self.assertRaises(ValueError):
            run_analysis(cfg)


if __name__ == "__main__":
    unittest.main()
