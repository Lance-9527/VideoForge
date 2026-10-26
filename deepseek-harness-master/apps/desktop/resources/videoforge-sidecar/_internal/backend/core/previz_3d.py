"""
VideoForge · 白膜 B/C 档：多机位与三维走位

════════════════════════════════════════════════════════════════════════
A 档缺什么（这是 B/C 档存在的理由）
════════════════════════════════════════════════════════════════════════
A 档是**一个平面**：把几个矩形摞在一起，加个推拉摇移。
它能验"节奏"和"构图"，但验不了这两件事：

  · **走位**：人物从左边走到右边，途中会不会绕过桌子？A 档看不出来。
  · **遮挡**：前景的柱子挡住后景的人了吗？前后景各占多大？A 档看不出来。

而这两个恰恰是**最贵的错误** —— 等花了钱生成出来才发现"人穿过桌子了"
或者"柱子把主角全挡住了"，那这一镜就白抽了。

════════════════════════════════════════════════════════════════════════
本模块做什么（不引第三方依赖）
════════════════════════════════════════════════════════════════════════
用**纯 Python 画像素**做正交/透视投影 + 画家算法（远的先画、近的后画）：

  1. `render_views()` —— 一次出**多个机位**：俯视平面图 + 正面 + 侧视
     （多机位是 B 档的核心价值：一个角度看不出走位对不对，两个角度就能）
  2. 深度遮挡 —— 按 `z` 排序后绘制，前面的物体真的会挡住后面的
  3. `render_camera_path()` —— 单独出一张**相机路径图**（俯视带轨迹箭头），
     用于验证"这个运镜在空间中是否可行"（实测：只给首帧当 i2v 输入时，
     模型会自己编运镜；有了路径图才有约束依据）
  4. `lint_spatial()` —— 三维体检：主体重叠、出画、被前景完全遮挡、
     轨迹穿模（走过家具的位置）

════════════════════════════════════════════════════════════════════════
诚实边界
════════════════════════════════════════════════════════════════════════
· 这是**正投影 + 画家算法**，不是真正的 GPU 三维渲染。它能正确表达
  "谁在前谁在后""走位会不会撞"，但**没有光影、没有透视畸变**。
· 真透视的**多机位视频**已经内置在 `previz_multi.py`（C 档，针孔投影 +
  逐帧渲染）。复杂曲面、骨骼动画、真实材质仍然要 Three.js / Blender ——
  但白膜要回答的问题（走位、遮挡、机位、节奏）**不需要**那些。
· 像素级绘制在 1080p 上要几秒 —— 比 A 档慢，但仍远快于生成视频。
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.previz_3d")

# 灰度分档：**必须纯灰**。彩色白模的颜色会渗进成片（实测：蓝色人群白模
# → 成片出现"蓝面罩人群"）。这里用**明度**区分层次，不用色相。
SHADES = [0x9A, 0x6E, 0xB4, 0x84, 0x5A]
GROUND = 0x50
GRID = 0x64
BG = 0x80
PATH_COLOR = 0xD0      # 相机路径的浅灰（仍是灰阶）


# ──────────────────────── 绘图基础 ────────────────────────


class Canvas:
    """极简灰度画布（纯 Python，不依赖 PIL/numpy）。"""

    def __init__(self, w: int, h: int, bg: int = BG):
        self.w, self.h = w, h
        self.px = bytearray([bg]) * (w * h)

    def put(self, x: int, y: int, v: int) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[y * self.w + x] = v & 0xFF

    def rect(self, x: int, y: int, w: int, h: int, v: int) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        for yy in range(y0, y1):
            row = yy * self.w
            for xx in range(x0, x1):
                self.px[row + xx] = v & 0xFF

    def hline(self, x0: int, x1: int, y: int, v: int) -> None:
        if not (0 <= y < self.h):
            return
        for x in range(max(0, x0), min(self.w, x1)):
            self.px[y * self.w + x] = v & 0xFF

    def vline(self, x: int, y0: int, y1: int, v: int) -> None:
        if not (0 <= x < self.w):
            return
        for y in range(max(0, y0), min(self.h, y1)):
            self.px[y * self.w + x] = v & 0xFF

    def disc(self, cx: int, cy: int, r: int, v: int) -> None:
        for dy in range(-r, r + 1):
            span = int(math.sqrt(max(0, r * r - dy * dy)))
            self.hline(cx - span, cx + span + 1, cy + dy, v)

    def to_pgm(self, path: str) -> str:
        """写成 PGM（P5）。ffmpeg 能直接读，不需要 PIL。"""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"P5\n%d %d\n255\n" % (self.w, self.h))
            f.write(bytes(self.px))
        return path


# ──────────────────────── 三维体检 ────────────────────────


def _box_of(s: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """把一个主体规范成三维盒子 {x,y,z,w,h,d}。缺字段返回 None。"""
    try:
        x = float(s.get("x"))
        y = float(s.get("y"))
    except (TypeError, ValueError):
        return None
    w = float(s.get("width") or 0)
    h = float(s.get("height") or 0)
    if w <= 0 or h <= 0:
        return None
    return {"x": x, "y": y, "z": float(s.get("z") or 0.0),
            "w": w, "h": h, "d": float(s.get("depth") or max(1.0, w * 0.6)),
            "name": s.get("name") or "主体",
            "move": s.get("move") or []}


def lint_spatial(spec: Dict[str, Any]) -> Dict[str, Any]:
    """三维体检。**在渲染之前**跑，免费且秒级。

    查这四类问题（都是"等花了钱才发现就晚了"的）：
      1. 主体之间在三维空间里重叠（撞在一起 / 穿模）
      2. 轨迹穿模：走位路径经过另一个物体的位置
      3. 被前景完全遮挡（观众根本看不到这个主体）
      4. 主体出画（中心点跑出画幅）
    """
    errors: List[str] = []
    warnings: List[str] = []
    subs = [s for s in (spec.get("subjects") or []) if isinstance(s, dict)]
    boxes = [(s, _box_of(s)) for s in subs]
    boxes = [(s, b) for s, b in boxes if b]

    frame = spec.get("frame") or {}
    fw = float(frame.get("width") or 1920)
    fh = float(frame.get("height") or 1080)

    def overlap_2d(a: Dict[str, float], b: Dict[str, float], pad: float = 0.0):
        """在 x（横向）与 z（纵深）上是否重叠。体积碰撞看这两个轴。"""
        ax0, ax1 = a["x"] - pad, a["x"] + a["w"] + pad
        bx0, bx1 = b["x"] - pad, b["x"] + b["w"] + pad
        az0, az1 = a["z"] - pad, a["z"] + a["z"] + a["d"] + pad
        bz0, bz1 = b["z"] - pad, b["z"] + b["d"] + pad
        return not (ax1 <= bx0 or bx1 <= ax0) and not (az1 <= bz0 or bz1 <= az0)

    # 1) 主体互相重叠
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (sa, ba), (sb, bb) = boxes[i], boxes[j]
            if overlap_2d(ba, bb):
                # 纵向（画面高度方向）也要重叠才算真撞
                ay0, ay1 = ba["y"], ba["y"] + ba["h"]
                by0, by1 = bb["y"], bb["y"] + bb["h"]
                if not (ay1 <= by0 or by1 <= ay0):
                    errors.append(
                        f"「{ba['name']}」和「{bb['name']}」在空间上重叠了"
                        f"（x/z/高度都相交）—— 建模/走位有问题，"
                        f"成片里会看到穿模或两个人粘在一起。"
                    )

    # 2) 轨迹穿模
    #
    # ★★ 必须做**线段与矩形求交**，不能只查端点（2026-09-23 修）。
    #   我第一版只查了每段的**起点**：`(200,300) → (800,300)` 走向一张
    #   位于 x[700,1000] 的桌子时，起点在桌外 → 判定"没穿模"。
    #   可实际上人物**终点就落在桌子里**，也就是会穿过去。
    #   这种"漏判"最危险：它正是"花了钱生成出来才发现"的那类错误。
    #   现在用 slab 法做标准线段-矩形相交。
    def seg_hits_rect(x1, y1, x2, y2, rx0, ry0, rx1, ry1) -> bool:
        """线段 (x1,y1)-(x2,y2) 是否与矩形 [rx0,rx1]x[ry0,ry1] 相交。"""
        # 端点在矩形内 → 命中
        for (px, py) in ((x1, y1), (x2, y2)):
            if rx0 <= px <= rx1 and ry0 <= py <= ry1:
                return True
        dx, dy = x2 - x1, y2 - y1
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, x1 - rx0), (dx, rx1 - x1),
                     (-dy, y1 - ry0), (dy, ry1 - y1)):
            if abs(p) < 1e-12:
                if q < 0:
                    return False          # 平行且在矩形外
                continue
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
        return True

    for s, b in boxes:
        mv = b.get("move") or []
        if len(mv) < 2:
            continue
        for s2, b2 in boxes:
            if s2 is s:
                continue
            # ★★ 纵深也要判：人从柱子**前面**或**后面**走过去是正常的，
            #    只有"同一层里撞上"才是穿模（2026-09-24 修）。
            #    之前只看画面平面的 x/y 相交，于是"从柱子背后走过"这种
            #    完全合法的调度会被判成穿模、直接拦住渲染 —— 假阳性。
            #    纵深不重叠 = 不在同一层 = 不算撞。
            bz0, bz1 = b["z"], b["z"] + b["d"]
            cz0, cz1 = b2["z"], b2["z"] + b2["d"]
            if bz1 <= cz0 or cz1 <= bz0:
                continue
            rx0, rx1 = b2["x"], b2["x"] + b2["w"]
            ry0, ry1 = b2["y"], b2["y"] + b2["h"]
            for k in range(len(mv) - 1):
                try:
                    ax = float(mv[k][0]); ay = float(mv[k][1])
                    cx = float(mv[k + 1][0]); cy = float(mv[k + 1][1])
                except Exception:
                    continue
                if seg_hits_rect(ax, ay, cx, cy, rx0, ry0, rx1, ry1):
                    errors.append(
                        f"「{b['name']}」的走位路径会**穿过**「{b2['name']}」"
                        f"（第 {k+1} 段 ({ax:.0f},{ay:.0f})→({cx:.0f},{cy:.0f}) "
                        f"与对方的范围相交，且两者纵深重叠 "
                        f"[{bz0:.0f},{bz1:.0f}] vs [{cz0:.0f},{cz1:.0f}]）。"
                        f"要么挪开家具，要么改走位，"
                        f"要么把纵深错开（错开就是「从他前面/后面过」，不算撞）—— "
                        f"这个错误等花几十块生成出来才会发现，然后这一镜就白抽了。"
                    )
                    break

    # 3) 被前景完全遮挡
    for s, b in boxes:
        if not s.get("must_see"):
            continue
        cx = b["x"] + b["w"] / 2.0
        cy = b["y"] + b["h"] / 2.0
        blocked_by = []
        for s2, b2 in boxes:
            if s2 is s:
                continue
            # z 更大 = 更靠近镜头（约定：z 越大越近）
            if b2["z"] <= b["z"]:
                continue
            if (b2["x"] <= cx <= b2["x"] + b2["w"]
                    and b2["y"] <= cy <= b2["y"] + b2["h"]):
                blocked_by.append(b2["name"])
        if blocked_by:
            errors.append(
                f"「{b['name']}」标了必须出现，但它被前景的"
                f"「{'、'.join(blocked_by)}」**完全挡住**了 —— "
                f"观众看不到它。挪开前景，或把它往前挪（z 调大）。"
            )

    # 4) 出画
    for s, b in boxes:
        if not s.get("must_see"):
            continue
        cx = b["x"] + b["w"] / 2.0
        cy = b["y"] + b["h"] / 2.0
        if cx < 0 or cy < 0 or cx > fw or cy > fh:
            errors.append(
                f"「{b['name']}」标了必须出现，但中心点 ({cx:.0f},{cy:.0f}) "
                f"在画幅 {fw:.0f}x{fh:.0f} 之外。"
            )

    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "caveat": "三维体检只查「几何上确定是错的」（撞模/遮挡/出画）；"
                      "运镜好不好看仍然要看画面。"}


# ──────────────────────── 多机位渲染 ────────────────────────


def _fit(v: float, lo: float, hi: float, out_lo: float, out_hi: float) -> float:
    if hi <= lo:
        return out_lo
    return out_lo + (v - lo) / (hi - lo) * (out_hi - out_lo)


def render_views(spec: Dict[str, Any], out_dir: str, *,
                 views: Optional[List[str]] = None,
                 cell_w: int = 520, cell_h: int = 300) -> Dict[str, Any]:
    """渲染**多机位**白膜图（B 档核心）。

    机位（`views` 可选，默认前三个）：
      · `top`   俯视平面图 —— 看走位、看物体在空间里的相对位置（**最有用**）
      · `front` 正面 —— 相当于摄像机看到的样子
      · `side`  侧视 —— 补上正面看不出的纵深关系
      · `path`  俯视 + 运动轨迹

    输出一张**横向拼合**的联络表 PGM（ffmpeg 能直接读），像分镜表一样一眼看全。

    ★ 排版要点（第一版踩到的）：每个机位必须有自己的**独立面板**，
      不能共用一块地面带 —— 否则俯视图会画在"地板"上，
      而且世界坐标（0~1920）与面板尺寸（640）比例差太大，物体小得看不见。
      现在每个面板内部各自做坐标映射，物体永远填满面板。
    """
    views = views or ["top", "front", "side"]
    frame = spec.get("frame") or {}
    fw = float(frame.get("width") or 1920)
    fh = float(frame.get("height") or 1080)
    subs = [s for s in (spec.get("subjects") or []) if isinstance(s, dict)]

    boxes = []
    for i, s in enumerate(subs):
        b = _box_of(s)
        if b:
            b["shade"] = SHADES[i % len(SHADES)]
            b["humanoid"] = bool(s.get("humanoid"))
            b["facing"] = s.get("facing") or "right"
            b["raw"] = s
            boxes.append(b)
    boxes.sort(key=lambda b: b["z"])      # 画家算法：z 大的（近的）后画

    Path(out_dir).mkdir(parents=True, exist_ok=True)

    # ★ 世界范围要**紧贴实际物体**，不能把整个画幅铺满。
    #   第一版用了 `max(..., fw*0.3)` 当上界，于是 x 轴上界被撑到 1280+，
    #   而物体只占 180~1060 —— 结果俯视图里东西小得看不见（实测）。
    xs = ([b["x"] for b in boxes] + [b["x"] + b["w"] for b in boxes]) or [0, fw]
    zs = ([b["z"] for b in boxes] + [b["z"] + b["d"] for b in boxes]) or [0, 300]
    wx0, wx1 = min(xs), max(xs)
    wz0, wz1 = min(zs), max(zs)

    # ★★ 纵深轴的处理（第三版才对，前两版都是错的，记下来免得再犯）
    #
    #   背景：`x/y` 是**画面像素**坐标（几百），`z/depth` 是**纵深尺寸**。
    #   老数据（或者用户随手填的）`depth` 常常只有几十，于是俯视图里
    #   物体薄成一条线。我前两版都试图"把纵深轴撑开"，结果：
    #     第二版撑到横向的 0.45 倍 → 物体还是 4px 高
    #     第三版直接对齐横向跨度  → 纵深范围被撑到 1300，
    #                               每个物体反而变成 **-23px**（负数！），
    #                               被 max(·,8) 兜成 8px —— 更糟
    #
    #   正确做法：**别撑范围，撑物体**。
    #   · 纵向范围保持物体真实包围盒（不要动它，动了就会压缩物体尺寸）
    #   · 只对"画出来太薄"的物体给一个**最小尺寸**（示意图可读性优先）
    #   · 并且在 lint 里提醒深度比例不合理，让用户去改数据，而不是
    #     让渲染器替他猜
    span_x = max(1.0, wx1 - wx0)
    span_z = max(1.0, wz1 - wz0)
    if span_z < span_x * 0.25:
        logger.info("俯视图纵深跨度 %.0f 远小于横向 %.0f —— 深度比例可能不合理，"
                    "已对小物体套用最小尺寸（更该做的是把 depth 填大些）",
                    span_z, span_x)

    pad_x = max(20.0, (wx1 - wx0) * 0.08)
    pad_z = max(20.0, (wz1 - wz0) * 0.08)
    wx0 -= pad_x; wx1 += pad_x
    wz0 -= pad_z; wz1 += pad_z
    if wx1 - wx0 < 1:
        wx1 = wx0 + 100
    if wz1 - wz0 < 1:
        wz1 = wz0 + 100

    GAP = 12
    total_w = cell_w * len(views) + GAP * (len(views) + 1)
    total_h = cell_h + GAP * 2
    sheet = Canvas(total_w, total_h, 0x28)

    made: List[str] = []
    for vi, v in enumerate(views):
        ox = GAP + vi * (cell_w + GAP)
        oy = GAP
        c = Canvas(cell_w, cell_h, BG)          # 每个面板独立画，最后贴到联络表

        if v in ("top", "path"):
            # ── 俯视平面图：横轴 x，纵轴 z（往下 = 更靠近镜头）──
            # 板内留边距，物体填满
            m = 24
            def mx(x):
                return int(_fit(x, wx0, wx1, m, cell_w - m))
            def mz(z):
                return int(_fit(z, wz0, wz1, m, cell_h - m))
            # 地面底色
            c.rect(0, 0, cell_w, cell_h, 0x6A)
            for gx in range(m, cell_w - m, max(16, (cell_w - 2 * m) // 6)):
                c.vline(gx, m, cell_h - m, GRID)
            for gz in range(m, cell_h - m, max(16, (cell_h - 2 * m) // 4)):
                c.hline(m, cell_w - m, gz, GRID)
            # 相机位（俯视看，相机在画面下边缘之外）
            c.hline(m, cell_w - m, cell_h - 6, 0xFF)
            for b in boxes:
                # ★★ 俯视图是**示意图**，不是等比平面图（这里踩了三版坑，别改回去）
                #
                #   量纲矛盾是本质问题，不是画法问题：
                #     `x/y` 是**画面像素**（例：主体宽 90~300，画幅 1280）
                #     `z/depth` 是**纵深尺寸**（例：120~260）
                #   横向跨度 880 vs 纵深跨度 1120 —— 单看数字接近，
                #   但每个物体自身的 `depth` 只占纵深的 10~23%，
                #   而它的 `width` 占横向也是 10~34%。画出来就是**宽度几百像素、
                #   高度几像素**的一条线。
                #
                #   我试过"把纵深轴撑开"——那是**错的**：撑开范围会让每个物体的
                #   高度变成负数（被 max(·,8) 兜住），比不撑更糟。
                #
                #   所以最终做法：**画成标记块**，纵向给一个固定可读高度，
                #   横向按真实宽度比例。它表达的是"谁在前后、谁在左右、
                #   谁跟谁重叠"——这正是走位验证需要的信息；
                #   不需要（也不可能）从这套数据里得到等比平面图。
                px = mx(b["x"])
                py = mz(b["z"] + b["d"])          # 靠镜头的一边
                pw = max(10, mx(b["x"] + b["w"]) - mx(b["x"]))
                MH = 22                            # 标记块固定高度，保证可读
                c.rect(px, py, pw, MH, b["shade"])
                # 纵深用一条细线示意（它只表明"这个物体有多厚"）
                thick = max(2, mz(b["z"]) - mz(b["z"] + b["d"]))
                if thick > 2:
                    c.vline(px, py - thick, py, b["shade"])
                    c.vline(px + pw - 1, py - thick, py, b["shade"])
                if b["humanoid"]:
                    nx = px + pw if b["facing"] == "right" else px - 5
                    c.rect(max(0, nx), py + MH // 2 - 2, 5, 5, 0xE8)
                if v == "path" and b["move"]:
                    mv = b["move"]
                    for k in range(len(mv) - 1):
                        try:
                            ax, ay = mx(float(mv[k][0])), mz(float(mv[k][1]))
                            bx2, by2 = mx(float(mv[k + 1][0])), mz(float(mv[k + 1][1]))
                        except Exception:
                            continue
                        steps = max(2, int(max(abs(bx2 - ax), abs(by2 - ay))))
                        for t in range(steps + 1):
                            c.put(int(ax + (bx2 - ax) * t / steps),
                                  int(ay + (by2 - ay) * t / steps), PATH_COLOR)
        else:
            # ── 正面 / 侧面：把三维盒子投影成 2D 矩形，z 大的后画（正确遮挡）──
            span = (wx0, wx1) if v == "front" else (wz0, wz1)
            ground_y = int(cell_h * 0.78)
            c.rect(0, ground_y, cell_w, cell_h, GROUND)
            m = 20
            # 竖直方向按"画幅高度"缩放，让最高的物体接近满高
            tallest = max([b["h"] for b in boxes] + [fh * 0.5])
            for b in boxes:
                if v == "front":
                    px = int(_fit(b["x"], span[0], span[1], m, cell_w - m))
                    pw = max(4, int((b["w"] / max(1.0, span[1] - span[0])) * (cell_w - 2 * m)))
                else:
                    px = int(_fit(b["z"], span[0], span[1], m, cell_w - m))
                    pw = max(4, int((b["d"] / max(1.0, span[1] - span[0])) * (cell_w - 2 * m)))
                ph = max(4, int(b["h"] / tallest * (ground_y - 20)))
                py = ground_y - ph
                c.rect(px, py, pw, ph, b["shade"])
                c.rect(max(0, px - 3), ground_y - 3, pw + 6, 4, 0x38)   # 接触阴影
                if b["humanoid"]:
                    c.rect(px + max(1, pw // 5), py + int(ph * 0.26),
                           max(4, int(pw * 0.6)), max(3, ph // 14), 0xE8)

        # 三分线
        for k in (1, 2):
            c.hline(0, cell_w, cell_h * k // 3, GRID)
            c.vline(cell_w * k // 3, 0, cell_h, GRID)

        # 贴到联络表
        for yy in range(cell_h):
            base = yy * cell_w
            dst = (oy + yy) * total_w + ox
            sheet.px[dst:dst + cell_w] = c.px[base:base + cell_w]

        p = os.path.join(out_dir, f"view_{v}.pgm")
        c.to_pgm(p)
        made.append(p)

    sheet_path = os.path.join(out_dir, "views_sheet.pgm")
    sheet.to_pgm(sheet_path)
    return {"ok": True, "sheet": sheet_path, "views": made, "dir": out_dir,
            "layout": views,
            "note": "多机位联络表（左到右：" + " / ".join(views) + "）。"
                    "俯视看走位与相对位置、正面看构图、侧面看纵深。"
                    "正投影 + 画家算法（谁近谁挡谁是对的），无光影与透视畸变。"}


def render_camera_path(spec: Dict[str, Any], out_path: str,
                       *, w: int = 800, h: int = 480) -> Dict[str, Any]:
    """单独出**相机路径图**（俯视 + 轨迹箭头）。

    用途：验证"这个运镜在空间里是否可行"。实测结论是：
    只给首帧当 i2v 输入时，模型**会自己编运镜**；有了路径图才有约束依据。
    """
    frame = spec.get("frame") or {}
    fw = float(frame.get("width") or 1920)
    subs = [s for s in (spec.get("subjects") or []) if isinstance(s, dict)]
    boxes = [b for b in (_box_of(s) for s in subs) if b]
    if not boxes:
        return {"ok": False, "error": "没有带坐标的主体，画不出路径图", "path": ""}

    c = Canvas(w, h, BG)
    c.rect(0, h // 2, w, h, GROUND)
    for gx in range(0, w, 40):
        c.vline(gx, h // 2, h, GRID)

    cam = str(spec.get("camera") or "static")
    # 相机位的起止（俯视）：用一条带箭头的灰线表示推/拉/摇
    cy = int(h * 0.30)
    if cam in ("dolly_in", "track"):
        c.rect(int(w * 0.10), cy, int(w * 0.30), 6, PATH_COLOR)
        c.disc(int(w * 0.42), cy + 3, 7, 0xFF)
    elif cam == "dolly_out":
        c.rect(int(w * 0.55), cy, int(w * 0.30), 6, PATH_COLOR)
        c.disc(int(w * 0.52), cy + 3, 7, 0xFF)
    elif cam in ("pan_left", "pan_right", "tilt_up", "tilt_down"):
        c.rect(int(w * 0.15), cy, int(w * 0.68), 6, PATH_COLOR)
        c.disc(int(w * 0.15), cy + 3, 6, 0xFF)
        c.disc(int(w * 0.83), cy + 3, 6, 0xFF)
    else:
        c.disc(w // 2, cy + 3, 8, 0xFF)

    for i, b in enumerate(sorted(boxes, key=lambda x: x["z"])):
        px = int(_fit(b["x"], 0, fw, 40, w - 40))
        pw = max(6, int(b["w"] / fw * (w - 80)))
        py = int(h * 0.60) + i * 26
        c.rect(px, py, pw, 20, SHADES[i % len(SHADES)])
        if b.get("move"):
            mv = b["move"]
            for k in range(len(mv) - 1):
                try:
                    ax = int(_fit(float(mv[k][0]), 0, fw, 40, w - 40))
                    bx2 = int(_fit(float(mv[k + 1][0]), 0, fw, 40, w - 40))
                except Exception:
                    continue
                c.hline(min(ax, bx2), max(ax, bx2), py + 10, PATH_COLOR)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    c.to_pgm(out_path)
    return {"ok": True, "path": out_path, "camera": cam,
            "note": "俯视相机路径示意：横线是机位移动方向，方块是主体与走位。"
                    "用来验证运镜在空间里是否可行。"}
