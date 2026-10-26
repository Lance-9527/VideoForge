# -*- coding: utf-8 -*-
"""白膜 / 480p 成片一致性验收器 的测试。

运行（在 VideoForge 仓库根目录）：
    python -X utf8 backend/tests/test_shot_consistency.py
或：
    python -X utf8 -m unittest discover -s backend/tests -p "test_shot_consistency.py" -v

覆盖三类：
  1) 纯合成用例（不需要素材）：几何判定 / 相位相关 / 主体分割 / 阈值覆盖 / 输出契约 /
     无网络无随机静态检查；这些用例在素材缺失时仍然全绿，保证本文件不会因为素材挪位而空转。
  2) 真实素材用例：正确白膜 vs 第 2 轮成片、旧（悬浮）白膜 vs 同一条成片 —— 把实测数字
     冻结进断言，证明指标有区分度，而不是摆设。
  3) 交叉验证用例：第 1 轮成片是由**旧**白膜生成的，所以 OLD↔GEN1 应显著优于 CORR↔GEN1；
     这一步用来证明「IoU / edge_ncc 确实在识别白膜与成片的对应关系」。

实测基准（2026-10-25 本机素材，见 BASELINE）来源：
  backend/core/shot_consistency.py 的 evaluate_* 输出（round 4/6）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.shot_contract import THRESHOLDS, build_contract, orbit_path  # noqa: E402
from core import shot_consistency as sc  # noqa: E402

FF_OK = bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))

#: 素材根目录（可用环境变量覆盖；默认本机验证目录）
WORK = Path(os.environ.get(
    "VFORGE_WORK_DIR",
    r"C:\Users\Administrator\Documents\Codex\2026-10-24"
    r"\videoforge-d-videoforge-dsh-dist-1-2\work",
))
ASSETS = {
    "corr_spec": WORK / "review-previz-corrected-20261025" / "spec.json",
    "corr_mp4": WORK / "review-previz-corrected-20261025" / "final-render" / "镜01.mp4",
    "corr_samples": WORK / "review-previz-corrected-20261025" / "final-render" / "镜01"
                    / "camera-samples.json",
    "live_root": WORK / "live-validation-20261025" / "outputs"
                 / "fb69f757-388e-45bf-bc9d-8b1746fb6583",
}
ASSETS["old_spec"] = ASSETS["live_root"] / "previz.bak-20261025-0931" / "spec.json"
ASSETS["old_mp4"] = ASSETS["live_root"] / "previz.bak-20261025-0931" / "镜01.mp4"
ASSETS["gen2_mp4"] = (ASSETS["live_root"] / "shots" / "api"
                      / "aaaf7bbd-1ca2-40af-b422-e8f3e2f37132_1792892745_00.mp4")
ASSETS["gen1_mp4"] = (ASSETS["live_root"] / "shots" / "api"
                      / "aaaf7bbd-1ca2-40af-b422-e8f3e2f37132_1792862952_00.mp4")
ASSETS["delivery_mp4"] = (ASSETS["live_root"] / "shots" / "api"
                          / "aaaf7bbd-1ca2-40af-b422-e8f3e2f37132_1792892745_00"
                            ".delivery-96f-864x486.mp4")

MEDIA_OK = FF_OK and all(Path(p).is_file() for k, p in ASSETS.items() if k != "live_root")

#: 两组对照的实测基准（evaluate_generated 输出的 metrics）
BASELINE = {
    # 正确白膜（z=0.75 坐在 h=0.75 桌上；90° 等半径环绕；85mm 定焦） vs 第 2 轮 480p
    "correct_vs_gen2": {
        "ssim": 0.832921,
        "silhouette_iou_mean": 0.580571,
        "edge_ncc": 0.502388,
        "subject_height_whitebox": 0.558333,
        "subject_height_generated": 0.555556,
        "subject_height_delta": 0.002778,
        "motion_coherence_whitebox": 0.993786,
        "motion_coherence_generated": 0.088712,
        "zoom_whitebox": False,
        "zoom_generated": False,
        "failures": ["silhouette_iou", "motion_not_reproduced"],
    },
    # 错误白膜（z=0.9 悬空；push_in 路径；65mm） vs 同一条第 2 轮 480p
    "wrong_vs_gen2": {
        "ssim": 0.821558,
        "silhouette_iou_mean": 0.238321,
        "edge_ncc": 0.080605,
        "subject_height_whitebox": 0.472222,
        "subject_height_generated": 0.555556,
        "subject_height_delta": 0.083333,
        "motion_coherence_whitebox": 0.678113,
        "motion_coherence_generated": 0.088712,
        "zoom_whitebox": True,
        "zoom_generated": False,
        "failures": ["silhouette_iou", "subject_height_ratio", "zoom_not_reproduced",
                     "motion_not_reproduced", "edge_ncc"],
    },
    # 第 1 轮 480p 是**旧**白膜生成的 → 与旧白膜是同源对（应高分）
    "old_vs_gen1": {
        "ssim": 0.873166,
        "silhouette_iou_mean": 0.548865,
        "edge_ncc": 0.555288,
        "subject_height_delta": 0.002778,
        "zoom_whitebox": True,
        "zoom_generated": True,
        "failures": ["silhouette_iou"],  # 只因 0.65 的 IoU 阈值（见阈建议）
    },
    # 交叉错配：正确白膜 vs 第 1 轮成片（不是它生成的）
    "correct_vs_gen1": {
        "ssim": 0.838503,
        "silhouette_iou_mean": 0.288541,
        "edge_ncc": 0.046241,
        "subject_height_delta": 0.088889,
        "failures": ["silhouette_iou", "subject_height_ratio", "suspected_zoom",
                     "edge_ncc"],
    },
}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
#: 契约在 2026-10-25 交付时的阈值快照。
#: 测试刻意冻结这一份（而不是每次现读 shot_contract.THRESHOLDS），这样：
#:   · 验收基线数字不会因为契约调阈值而失效；
#:   · 「0.65 不现实」的用例仍然在测**当时交付的那个 0.65**；
#:   · 另有一条用例专门检查契约模块仍然暴露这 7 个键（接口没被改坏）。
DELIVERED_THRESHOLDS = {
    "radius_drift_ratio": 0.02,
    "lens_drift_mm": 0.5,
    "subject_ratio_drift": 0.05,
    "ssim_min": 0.80,
    "silhouette_iou_min": 0.65,
    "motion_cosine_min": 0.75,
    "arc_angle_tolerance_deg": 12.0,
}


def _contract(spec: dict, text: str = "", samples=None) -> dict:
    """build_contract + 冻结交付期阈值，保证基线可复算。"""
    contract = build_contract(spec, spec, text, camera_samples=samples)
    contract["thresholds"] = dict(DELIVERED_THRESHOLDS)
    return contract


def _load(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _make_whitebox_spec(*, duration: float = 4.0, lens: float = 85.0,
                        arc: float = 90.0, box_z: float = 0.75,
                        table_h: float = 0.75, on_surface: bool = True,
                        frames: int = 96) -> dict:
    """合成一份 z-up 的 orbit spec（与真实白膜同一套坐标约定）。"""
    pos = [-2.0, -2.0, 1.35]
    look = [0.0, 0.0, 1.15]
    moves = orbit_path(pos, look, lens, frames, arc)
    table = {"type": "table", "id": "prop_1", "x": 0.0, "y": 0.0, "z": 0.0,
             "w": 1.2, "d": 0.7, "h": table_h, "anchor": "base"}
    box = {"type": "box", "id": "prop_2", "x": 0.0, "y": 0.0, "z": box_z,
           "w": 0.6, "d": 0.6, "h": 0.6, "anchor": "base"}
    if on_surface:
        box["on_surface"] = "prop_1"
    return {
        "duration": duration,
        "movement": "orbit",
        "set": {"kind": "room", "props": [table, box]},
        "camera": {"position": pos, "look_at": look, "lens": lens, "moves": moves},
    }


def _make_synthetic_clip(path: Path, *, frames: int = 12, subtitle: bool = True) -> Path:
    """用 numpy+ffmpeg 造一段 320x180 / 24fps 的确定性测试片（可选底部白色字幕条）。"""
    import numpy as np

    h, w = 180, 320
    canvas = np.full((frames, h, w, 3), 38, dtype=np.uint8)
    canvas[:, 40:120, 90:230] = 170  # 中间一块亮面（主体）
    if subtitle:
        for x in range(6, w - 6, 5):  # 高频窄条 = 文字笔画边缘
            canvas[:, 158:173, x:x + 2] = 235
    raw = canvas.tobytes()
    cmd = [sc._ffmpeg(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}", "-r", "24", "-i", "-", "-c:v", "libx264",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)]
    subprocess.run(cmd, input=raw, capture_output=True, check=True)
    return path


def _names(result: dict) -> list:
    return [f["name"] for f in result["failures"]]


# --------------------------------------------------------------------------- #
# 0. 纯合成：输出契约 & 基础工具（不需要素材）
# --------------------------------------------------------------------------- #
class TestSyntheticBasics(unittest.TestCase):
    def test_result_schema_is_stable_and_json_safe(self):
        spec = _make_whitebox_spec()
        contract = _contract(spec)
        result = sc.evaluate_whitebox(spec, contract)
        self.assertEqual(set(result), {"ok", "metrics", "failures", "limitations"})
        self.assertIsInstance(result["ok"], bool)
        self.assertIsInstance(result["metrics"], dict)
        self.assertIsInstance(result["failures"], list)
        self.assertIsInstance(result["limitations"], list)
        for failure in result["failures"]:
            self.assertEqual(set(failure), {"name", "want", "got", "note"})
        json.dumps(result, ensure_ascii=False)  # 必须可 JSON 序列化

    def test_synthetic_orbit_passes_whitebox(self):
        spec = _make_whitebox_spec()
        result = sc.evaluate_whitebox(spec, _contract(spec))
        self.assertTrue(result["ok"], _names(result))
        self.assertAlmostEqual(result["metrics"]["arc_degrees"], 90.0, delta=0.05)
        self.assertLess(result["metrics"]["radius_drift_ratio"], 1e-3)
        self.assertAlmostEqual(result["metrics"]["contact_gap_m"], 0.0, delta=1e-9)
        self.assertEqual(result["metrics"]["movement_geometric"], "orbit")

    def test_synthetic_bad_radius_lens_duration_are_caught(self):
        spec = _make_whitebox_spec()
        spec["camera"]["moves"][1][1] -= 0.5      # 半径被破坏
        spec["camera"]["moves"][2][7] = 88.0      # 中途变焦
        spec["duration"] = 4.5                    # 与末帧 96 不符
        result = sc.evaluate_whitebox(spec, _contract(spec))
        self.assertFalse(result["ok"])
        names = _names(result)
        for expected in ("radius_drift_ratio", "lens_drift_mm", "duration_frames"):
            self.assertIn(expected, names)
        self.assertGreater(result["metrics"]["lens_drift_mm"], DELIVERED_THRESHOLDS["lens_drift_mm"])

    def test_floating_subject_is_caught_geometrically(self):
        """z=0.9 的 0.6m 方块放在 0.75m 高的桌上 → 悬空 0.15m。"""
        spec = _make_whitebox_spec(box_z=0.9)
        result = sc.evaluate_whitebox(spec, _contract(spec))
        self.assertIn("subject_contact", _names(result))
        self.assertAlmostEqual(result["metrics"]["contact_gap_m"], 0.15, delta=1e-6)

    def test_non_circular_path_is_not_orbit(self):
        spec = _make_whitebox_spec()
        # 直着横移 1.2m：不是环绕
        spec["camera"]["moves"] = [
            [1, -2.0, -2.0, 1.35, 0.0, 0.0, 1.15, 85.0],
            [48, -0.4, -2.0, 1.35, 0.0, 0.0, 1.15, 85.0],
            [96, -0.8, -2.08, 1.35, 0.0, 0.0, 1.15, 85.0],
        ]
        result = sc.evaluate_whitebox(spec, _contract(spec))
        self.assertGreater(result["metrics"]["radius_drift_ratio"],
                           DELIVERED_THRESHOLDS["radius_drift_ratio"])
        self.assertNotEqual(result["metrics"]["movement_geometric"], "orbit")

    def test_threshold_override_is_respected(self):
        spec = _make_whitebox_spec()
        spec["camera"]["moves"][1][1] -= 0.05     # 轻微半径漂移（约 2%~3%）
        contract = _contract(spec)
        drift = sc.evaluate_whitebox(spec, contract)["metrics"]["radius_drift_ratio"]
        self.assertGreater(drift, 0.0)
        strict = sc.evaluate_whitebox(spec, contract,
                                      thresholds={"radius_drift_ratio": 1e-6})
        self.assertIn("radius_drift_ratio", _names(strict))
        loose = sc.evaluate_whitebox(spec, contract,
                                     thresholds={"radius_drift_ratio": 1.0})
        self.assertNotIn("radius_drift_ratio", _names(loose))

    def test_missing_media_returns_failure_instead_of_crash(self):
        result = sc.evaluate_generated("D:/definitely/not/here.mp4",
                                       "D:/nor/here.mp4", {})
        self.assertFalse(result["ok"])
        self.assertIn("media_unavailable", _names(result))
        self.assertEqual(set(result), {"ok", "metrics", "failures", "limitations"})

    def test_phase_shift_subpixel_sign_and_determinism(self):
        import numpy as np

        base = np.zeros((180, 320), dtype=np.float32)
        base[40:90, 60:140] = 1.0
        base[120:150, 20:60] = 0.6
        moved = np.zeros_like(base)
        dy, dx = 3, 5
        moved[dy:, dx:] = base[:-dy, :-dx]           # 内容右移 5、下移 3
        ddx, ddy, _peak = sc._phase_shift(base, moved)
        # 对齐位移 = -内容位移
        self.assertAlmostEqual(ddx, -5.0, delta=0.35)
        self.assertAlmostEqual(ddy, -3.0, delta=0.35)
        again = sc._phase_shift(base, moved)
        self.assertEqual((ddx, ddy), (again[0], again[1]))

    def test_segment_subject_finds_bright_block(self):
        import numpy as np

        frame = np.full((180, 320, 3), 40, dtype=np.uint8)
        frame[50:140, 100:220] = 210
        seg = sc._segment_subject(frame)
        self.assertIsNotNone(seg)
        x0, y0, x1, y1 = seg["bbox"]
        self.assertLessEqual(abs(x0 - 100), 2)
        self.assertLessEqual(abs(y0 - 50), 2)
        self.assertLessEqual(abs(x1 - 219), 2)
        self.assertLessEqual(abs(y1 - 139), 2)
        self.assertAlmostEqual(seg["height_ratio"], 90 / 180.0, delta=0.02)

    def test_no_network_and_no_randomness_in_source(self):
        src = Path(sc.__file__).read_text(encoding="utf-8")
        for token in ("import requests", "import urllib", "http://", "https://",
                      "import socket", "import random", "np.random"):
            self.assertNotIn(token, src, f"验收器不应依赖 {token!r}（必须可复算、不联网）")
        self.assertIn("ffmpeg", src)

    def test_contract_threshold_interface_still_exposes_required_keys(self):
        missing = set(DELIVERED_THRESHOLDS) - set(THRESHOLDS)
        self.assertFalse(missing,
                         f"shot_contract.THRESHOLDS 缺少验收器依赖的键: {sorted(missing)}")
        # 阈值数值允许后续调整（本套测试基线已冻结在 DELIVERED_THRESHOLDS）
        self.assertTrue(all(isinstance(v, (int, float)) for v in THRESHOLDS.values()))


# --------------------------------------------------------------------------- #
# 1. 纯合成：交付硬检查（造一段合成 mp4 作正/负样本）
# --------------------------------------------------------------------------- #
@unittest.skipUnless(FF_OK, "需要 ffmpeg/ffprobe")
class TestSyntheticDelivery(unittest.TestCase):
    def test_subtitle_bar_heuristic_fires_on_synthetic_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = _make_synthetic_clip(Path(tmp) / "subtitle.mp4", subtitle=True)
            result = sc.evaluate_delivery(str(clip), {"duration": 0.5, "fps": 24})
            self.assertTrue(result["metrics"]["watermark_suspected"],
                            result["metrics"])
            self.assertTrue(any("字幕" in line for line in result["limitations"]))
            # 只告警不下结论：不影响 ok
            self.assertTrue(result["ok"], _names(result))

    def test_subtitle_heuristic_silent_on_clean_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = _make_synthetic_clip(Path(tmp) / "clean.mp4", subtitle=False)
            result = sc.evaluate_delivery(str(clip), {"duration": 0.5, "fps": 24})
            self.assertFalse(result["metrics"]["watermark_suspected"], result["metrics"])
            self.assertTrue(result["ok"], _names(result))


# --------------------------------------------------------------------------- #
# 2. 真实素材：A 组（契约 ↔ 白膜几何）
# --------------------------------------------------------------------------- #
@unittest.skipUnless(MEDIA_OK, f"需要素材目录 {WORK}")
class TestWhiteboxAgainstRealSpecs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corr_spec = _load(ASSETS["corr_spec"])["shots"][0]
        cls.old_spec = _load(ASSETS["old_spec"])["shots"][0]
        cls.corr_contract = _contract(cls.corr_spec)
        cls.old_contract = _contract(cls.old_spec)
        cls.corr_result = sc.evaluate_whitebox(cls.corr_spec, cls.corr_contract)
        cls.old_result = sc.evaluate_whitebox(cls.old_spec, cls.old_contract)

    def test_corrected_whitebox_spec_passes_geometry(self):
        result = self.corr_result
        self.assertTrue(result["ok"], _names(result))
        m = result["metrics"]
        self.assertAlmostEqual(m["radius_drift_ratio"], 0.000719, delta=0.0002)
        self.assertAlmostEqual(m["arc_degrees"], 90.0, delta=0.05)
        self.assertAlmostEqual(m["camera_height_span_m"], 0.0, delta=1e-9)
        self.assertAlmostEqual(m["contact_gap_m"], 0.0, delta=1e-9)
        self.assertEqual(m["support_top_z"], 0.75)
        self.assertEqual(m["movement_geometric"], "orbit")
        self.assertEqual(m["last_key_frame"], 96)

    def test_floating_whitebox_spec_fails_with_reasons(self):
        result = self.old_result
        self.assertFalse(result["ok"])
        names = _names(result)
        for expected in ("radius_drift_ratio", "subject_on_surface", "support_height",
                         "subject_contact", "movement"):
            self.assertIn(expected, names)
        m = result["metrics"]
        self.assertAlmostEqual(m["contact_gap_m"], 0.9, delta=1e-6)  # 悬空 0.9m
        self.assertAlmostEqual(m["radius_drift_ratio"], 0.237251, delta=0.002)
        self.assertEqual(m["movement_geometric"], "track")
        # 2026-10-25 契约层加了白膜运动词别名（push_in→dolly_in）：
        # 原先这里冻结的是「白膜在推、契约却判 static」的错误行为，现按修正后语义断言。
        self.assertEqual(m["movement_contract"], "dolly_in")

    def test_measured_camera_samples_change_lens_without_failing(self):
        """渲染器把 85mm 改成 46.75mm 取景适配：焦距按渲染实测记录，但不应因此判失败。"""
        samples = _load(ASSETS["corr_samples"])
        contract = _contract(self.corr_spec, samples=samples)
        self.assertTrue(contract["lens_rewritten"])
        self.assertAlmostEqual(contract["lens_mm"], 46.75, delta=0.01)
        result = sc.evaluate_whitebox(self.corr_spec, contract)
        self.assertTrue(result["ok"], _names(result))
        self.assertTrue(any("焦距" in line for line in result["limitations"]))

    def test_script_intent_arc_is_cross_checked(self):
        text = "镜头以立方体为圆心做 90 度弧线横移，85mm 定焦，不推拉、不变焦、单一连续镜头"
        contract = _contract(self.corr_spec, text)
        self.assertEqual(contract["arc_degrees_intent"], 90.0)
        result = sc.evaluate_whitebox(self.corr_spec, contract)
        self.assertTrue(result["ok"], _names(result))
        # 意图写 120°，实际 90° → 必须报 arc_degrees_intent
        bad = _contract(self.corr_spec, "以立方体为圆心做 120 度弧线横移")
        self.assertIn("arc_degrees_intent",
                      _names(sc.evaluate_whitebox(self.corr_spec, bad)))


# --------------------------------------------------------------------------- #
# 3. 真实素材：B 组（白膜 ↔ 480p 成片）—— 区分度在这里
# --------------------------------------------------------------------------- #
@unittest.skipUnless(MEDIA_OK, f"需要素材目录 {WORK}")
class TestGeneratedContrast(unittest.TestCase):
    """两组对照 + 交叉验证。耗时大头都在 setUpClass（各跑一次，缓存复用）。"""

    @classmethod
    def setUpClass(cls):
        cls.corr_spec = _load(ASSETS["corr_spec"])["shots"][0]
        cls.old_spec = _load(ASSETS["old_spec"])["shots"][0]
        cls.corr_contract = _contract(cls.corr_spec)
        cls.old_contract = _contract(cls.old_spec)
        cls.correct = sc.evaluate_generated(str(ASSETS["corr_mp4"]),
                                            str(ASSETS["gen2_mp4"]), cls.corr_contract)
        cls.wrong = sc.evaluate_generated(str(ASSETS["old_mp4"]),
                                          str(ASSETS["gen2_mp4"]), cls.old_contract)
        cls.old_vs_gen1 = sc.evaluate_generated(str(ASSETS["old_mp4"]),
                                                str(ASSETS["gen1_mp4"]), cls.old_contract)
        cls.corr_vs_gen1 = sc.evaluate_generated(str(ASSETS["corr_mp4"]),
                                                 str(ASSETS["gen1_mp4"]), cls.corr_contract)

    # ---- 2.1 冻结数字：正确组 ----
    def test_correct_pair_metrics_match_baseline(self):
        m = self.correct["metrics"]
        base = BASELINE["correct_vs_gen2"]
        for key in ("ssim", "silhouette_iou_mean", "edge_ncc",
                    "subject_height_whitebox", "subject_height_generated",
                    "subject_height_delta", "motion_coherence_whitebox",
                    "motion_coherence_generated"):
            self.assertAlmostEqual(m[key], base[key], delta=0.02, msg=key)
        self.assertEqual(m["zoom_whitebox"], base["zoom_whitebox"])
        self.assertEqual(m["zoom_generated"], base["zoom_generated"])
        self.assertEqual(_names(self.correct), base["failures"])

    # ---- 2.2 冻结数字：错误组 ----
    def test_wrong_pair_metrics_match_baseline(self):
        m = self.wrong["metrics"]
        base = BASELINE["wrong_vs_gen2"]
        for key in ("ssim", "silhouette_iou_mean", "edge_ncc",
                    "subject_height_delta", "motion_coherence_whitebox"):
            self.assertAlmostEqual(m[key], base[key], delta=0.02, msg=key)
        self.assertEqual(m["zoom_whitebox"], True)
        self.assertEqual(m["zoom_generated"], False)
        self.assertEqual(set(_names(self.wrong)), set(base["failures"]))

    # ---- 2.3 核心要求：正确组明显更好 ----
    def test_correct_group_clearly_beats_wrong_group(self):
        good = self.correct["metrics"]
        bad = self.wrong["metrics"]
        # 轮廓 IoU：2.4 倍差距
        self.assertGreater(good["silhouette_iou_mean"],
                           bad["silhouette_iou_mean"] + 0.25)
        # 结构相似度：6 倍差距（SSIM 在跨渲染风格时几乎不动，作为对照写在这里）
        self.assertGreater(good["edge_ncc"], bad["edge_ncc"] * 3.0)
        # 主体占画高：正确组 0.3%，错误组 8.3%（阈值 5%）
        self.assertLess(good["subject_height_delta"], 0.005)
        self.assertGreater(bad["subject_height_delta"], 0.05)
        # SSIM 的判别力很弱（这正是要加 edge_ncc 的原因），但方向不能反
        self.assertGreaterEqual(good["ssim"], bad["ssim"] - 0.005)
        self.assertGreater(good["ssim"], 0.80)   # 契约 ssim_min 可过
        self.assertLess(bad["silhouette_iou_mean"], 0.35)
        self.assertLess(bad["edge_ncc"], 0.20)

    # ---- 2.4 阈值建议的直接证据：0.65 连「同源对」都过不了 ----
    def test_iou_threshold_065_is_unrealistic_even_for_matched_pair(self):
        iou_same_source = self.old_vs_gen1["metrics"]["silhouette_iou_mean"]
        self.assertLess(iou_same_source, DELIVERED_THRESHOLDS["silhouette_iou_min"])  # 0.549 < 0.65
        relaxed = sc.evaluate_generated(str(ASSETS["old_mp4"]), str(ASSETS["gen1_mp4"]),
                                        self.old_contract,
                                        thresholds={"silhouette_iou_min": 0.45})
        self.assertTrue(relaxed["ok"], _names(relaxed))   # 同源对在 0.45 下全绿
        # 正确白膜 vs 第 2 轮：放宽 IoU 后只剩「运动没被复现」
        relaxed_corr = sc.evaluate_generated(str(ASSETS["corr_mp4"]),
                                            str(ASSETS["gen2_mp4"]),
                                            self.corr_contract,
                                            thresholds={"silhouette_iou_min": 0.45})
        self.assertEqual(_names(relaxed_corr), ["motion_not_reproduced"])

    # ---- 2.5 交叉验证：指标确实在识别「哪条成片来自哪条白膜」----
    def test_cross_round_pairing_is_detected(self):
        same = self.old_vs_gen1["metrics"]          # 第 1 轮 ← 旧白膜（同源）
        cross = self.corr_vs_gen1["metrics"]        # 第 1 轮 ← 正确白膜（错配）
        self.assertGreater(same["silhouette_iou_mean"], cross["silhouette_iou_mean"] + 0.2)
        self.assertGreater(same["edge_ncc"], cross["edge_ncc"] * 5.0)
        self.assertLess(abs(same["subject_height_delta"]), 0.01)
        self.assertGreater(cross["subject_height_delta"], 0.05)
        self.assertIn("suspected_zoom", _names(self.corr_vs_gen1))
        self.assertNotIn("subject_height_ratio", _names(self.old_vs_gen1))

    def test_motion_metrics_expose_camera_move_not_reproduced(self):
        m = self.correct["metrics"]
        # 白膜逐帧同向平移（相干度 ~0.99），成片方向随机（~0.09）→ 判「没复现」
        self.assertGreater(m["motion_coherence_whitebox"], 0.90)
        self.assertLess(m["motion_coherence_generated"], 0.20)
        self.assertFalse(m["motion_verified"])
        self.assertIn("motion_not_reproduced", _names(self.correct))
        # 该判据只在白膜确实有相干运动时生效（旧白膜也相干）
        self.assertGreater(self.wrong["metrics"]["motion_coherence_whitebox"], 0.5)
        self.assertIn("motion_not_reproduced", _names(self.wrong))


# --------------------------------------------------------------------------- #
# 4. 真实素材：C 组（交付硬检查）
# --------------------------------------------------------------------------- #
@unittest.skipUnless(MEDIA_OK, f"需要素材目录 {WORK}")
class TestDeliveryHygiene(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corr_spec = _load(ASSETS["corr_spec"])["shots"][0]
        cls.old_spec = _load(ASSETS["old_spec"])["shots"][0]
        cls.contract = _contract(cls.corr_spec)
        cls.old_contract = _contract(cls.old_spec)

    def test_silent_delivery_passes(self):
        result = sc.evaluate_delivery(str(ASSETS["delivery_mp4"]), self.contract)
        self.assertTrue(result["ok"], _names(result))
        m = result["metrics"]
        self.assertFalse(m["has_audio"])
        self.assertAlmostEqual(m["aspect_ratio"], 16 / 9, delta=1e-4)
        self.assertEqual(m["frames"], 96)
        self.assertAlmostEqual(m["duration_s"], 4.0, delta=1e-3)
        self.assertFalse(m["watermark_suspected"])

    def test_audio_and_overlength_are_rejected(self):
        result = sc.evaluate_delivery(str(ASSETS["gen1_mp4"]), self.old_contract)
        self.assertFalse(result["ok"])
        names = _names(result)
        for expected in ("delivery_silent", "delivery_frames", "delivery_duration",
                         "delivery_aspect"):
            self.assertIn(expected, names)
        self.assertTrue(result["metrics"]["has_audio"])
        self.assertEqual(result["metrics"]["audio_codec"], "aac")
        self.assertEqual(result["metrics"]["frames"], 121)

    def test_padding_aspect_is_flagged_but_model_length_tolerated(self):
        raw = sc.evaluate_delivery(str(ASSETS["gen2_mp4"]), self.contract)
        self.assertIn("delivery_aspect", _names(raw))
        # 97 帧 vs 96（±1 帧）与 4.0417s vs 4.0（±1 帧）都在容差内
        self.assertNotIn("delivery_frames", _names(raw))
        self.assertNotIn("delivery_duration", _names(raw))
        self.assertAlmostEqual(raw["metrics"]["frame_delta"], 1.0, delta=1e-6)

    def test_reference_image_only_adds_limitation(self):
        result = sc.evaluate_delivery(str(ASSETS["delivery_mp4"]), self.contract,
                                      reference_image="D:/ref/preview.png")
        self.assertTrue(result["ok"])
        self.assertTrue(any("参考图" in line or "语义" in line
                            for line in result["limitations"]))
        self.assertEqual(result["metrics"]["reference_image"], "D:/ref/preview.png")

    def test_probe_media_reads_expected_facts(self):
        info = sc.probe_media(str(ASSETS["corr_mp4"]))
        self.assertEqual((info["width"], info["height"]), (960, 540))
        self.assertAlmostEqual(info["fps"], 24.0, delta=1e-6)
        self.assertEqual(info["frames"], 96)
        self.assertFalse(info["has_audio"])
        self.assertAlmostEqual(info["duration"], 4.0, delta=1e-6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
