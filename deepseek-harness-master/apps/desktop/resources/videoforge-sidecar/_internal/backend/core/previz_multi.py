"""
VideoForge · 白膜 **C 档**：真正的透视多机位覆盖（内置，不依赖 Three.js / Blender）

═══════════════════════════════════════════════════════════════════════
为什么还要有 C 档（B 档已经不够在哪）
═══════════════════════════════════════════════════════════════════════
B 档出的是**一张静态联络表**（俯视/正面/侧面拼在一起）。它能回答
"人和桌子会不会撞"，但回答不了这两件事：

  · **机位选择**：这场戏用正面、侧面还是过肩？哪个角度叙事更清楚？
    —— 静态图看不出"换个角度之后前景多了什么、少了什么"。
  · **运动中的遮挡**：人往前走三步之后，柱子是不是正好把脸挡住了？
    —— 静态图只有一帧，运动过程完全看不到。

C 档就是补上这两件事：**同一场戏，多个机位，各出一段有时长的视频。**

═══════════════════════════════════════════════════════════════════════
实现方式（以及为什么不用 Three.js / Blender）
═══════════════════════════════════════════════════════════════════════
用**针孔透视投影 + 画家算法**在纯 Python 里逐帧画像素，再用 ffmpeg 编码。
不引第三方依赖、不需要 GPU、不需要装 Blender —— 白膜阶段**成本必须是 0**，
一旦要用户先装环境，这个功能就等于不存在。

关键性能手法（否则 1080p 逐像素在 Python 里要几分钟）：
  · 内部分辨率低（默认 640×360），交给 ffmpeg 放大到成片尺寸
  · 填充走 `bytearray` **切片赋值**（C 速度），绝不写逐像素 for 循环
  · 盒子只画投影后的包围矩形，不画多边形

═══════════════════════════════════════════════════════════════════════
诚实边界（别把这张图当成"渲染"）
═══════════════════════════════════════════════════════════════════════
· 盒子是**轴对齐包围盒**，不是真几何：人物是方块、没有骨骼、没有曲面。
· 有**真透视**（近大远小、视差）和**正确的前后遮挡**（画家算法按深度排序）。
· **没有光影、没有材质、没有景深**。它的用途是**空间与运镜决策**，
  不是看画面好不好看 —— 画面质感那一层由定妆照 + 生成模型负责。
· 与 B 档的关系：B 档一张图看全局，C 档一段视频看运动和多机位。
  两者都免费，都只是**控制信号**。
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.previz_multi")

# 灰度分档：**必须纯灰**。彩色白模的颜色会渗进成片（实测：蓝色人群白模
# → 成片出现"蓝面罩人群"）。用明度区分层次，不用色相。
SHADES = [0x9A, 0x6E, 0xB4, 0x84, 0x5A, 0xC0, 0x78, 0xA8]
GROUND = 0x50
GRID = 0x64
BG = 0x80
EDGE = 0xE6          # 盒子轮廓：比填充亮，任何底色上都看得见
TOP_FACE = 0xC8      # 顶面：给俯视一点点立体感

#: 机位表：名字 → (方位角°, 俯仰角°, 距离倍率)
#: 方位角 0 = 分镜原本的正面；负 = 绕到左侧；俯仰角正 = 从上往下看
ANGLES: Dict[str, Tuple[float, float, float]] = {
    "master": (0.0, 0.0, 1.00),      # 复现分镜构图，作为基准
    "left": (-38.0, 8.0, 1.18),      # 左前 3/4 —— 最常用的对话机位
    "right": (38.0, 8.0, 1.18),      # 右前 3/4
    "top": (0.0, 52.0, 1.30),        # 高角 —— 看全局走位
    "low": (14.0, -14.0, 0.92),      # 低角 —— 压迫感
    "over": (-24.0, 5.0, 0.62),      # 过肩 —— 贴近主角
}

#: 默认取哪几个（4 个够做决策，再多就是浪费渲染时间）
DEFAULT_ANGLES = ["master", "left", "right", "top"]

ANGLE_LABELS = {
    "master": "正面（分镜原构图）",
    "left": "左前 3/4",
    "right": "右前 3/4",
    "top": "高角俯视",
    "low": "低角仰拍",
    "over": "过肩近景",
}


# ──────────────────────── 画布（切片赋值，快） ────────────────────────


class FastCanvas:
    """灰度画布。填充用 `bytearray` 切片赋值 —— 实测比逐像素快两个数量级。

    ★ 踩过的坑：`previz_3d.Canvas.rect()` 是 `for xx in range(...)` 逐像素写，
      单帧 640×360 就要 23 万次 Python 循环。一秒钟 24 帧就是 550 万次 ——
      一段 5 秒的白膜要几十秒。改成切片赋值后同样的帧是**毫秒级**。
    """

    __slots__ = ("w", "h", "px")

    def __init__(self, w: int, h: int, bg: int = BG):
        self.w, self.h = w, h
        self.px = bytearray([bg]) * (w * h)

    def fill(self, v: int) -> None:
        self.px[:] = bytes([v]) * (self.w * self.h)

    def rect(self, x: int, y: int, w: int, h: int, v: int) -> None:
        if w <= 0 or h <= 0:
            return
        x0 = max(0, x)
        x1 = min(self.w, x + w)
        if x1 <= x0:
            return
        row = bytes([v & 0xFF]) * (x1 - x0)
        y0 = max(0, y)
        y1 = min(self.h, y + h)
        for yy in range(y0, y1):
            o = yy * self.w + x0
            self.px[o:o + (x1 - x0)] = row

    def hline(self, x0: int, x1: int, y: int, v: int) -> None:
        if not (0 <= y < self.h):
            return
        a, b = max(0, min(x0, x1)), min(self.w, max(x0, x1))
        if b <= a:
            return
        o = y * self.w + a
        self.px[o:o + (b - a)] = bytes([v & 0xFF]) * (b - a)

    def frame_rect(self, x: int, y: int, w: int, h: int, v: int) -> None:
        self.hline(x, x + w, y, v)
        self.hline(x, x + w, y + h - 1, v)

    def fill_poly(self, pts: List[Tuple[float, float]], v: int) -> None:
        """扫描线填充凸多边形（切片赋值，快）。

        ★★ 为什么必须有多边形填充（这是 C 档第一版最大的教训）：
          第一版把三维盒子投影后取**屏幕包围矩形**来画。正面机位看着还行，
          但机位一抬高（俯视），盒子的包围矩形就**塌成一条线** ——
          顶视图里四个人变成四根竖线，完全没法用来判断走位。
          改成"投影 8 个角点 → 按可见面填真多边形"之后，
          俯视图才真的像俯视图。
        """
        if len(pts) < 3:
            return
        ys = [p[1] for p in pts]
        y0 = max(0, int(math.floor(min(ys))))
        y1 = min(self.h - 1, int(math.ceil(max(ys))))
        if y1 < y0:
            return
        n = len(pts)
        row_bytes = None
        for y in range(y0, y1 + 1):
            yc = y + 0.5
            xs: List[float] = []
            for i in range(n):
                a, b = pts[i], pts[(i + 1) % n]
                ay, by = a[1], b[1]
                if (ay <= yc < by) or (by <= yc < ay):
                    t = (yc - ay) / (by - ay)
                    xs.append(a[0] + (b[0] - a[0]) * t)
            if len(xs) < 2:
                continue
            xs.sort()
            for k in range(0, len(xs) - 1, 2):
                xa = max(0, int(math.floor(xs[k])))
                xb = min(self.w, int(math.ceil(xs[k + 1])))
                if xb <= xa:
                    continue
                if row_bytes is None or len(row_bytes) != (xb - xa):
                    row_bytes = bytes([v & 0xFF]) * (xb - xa)
                o = y * self.w + xa
                self.px[o:o + (xb - xa)] = row_bytes

    def to_pgm(self, path: str) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"P5\n%d %d\n255\n" % (self.w, self.h))
            f.write(bytes(self.px))
        return path


# ──────────────────────── 三维数学 ────────────────────────


def _norm(v: Tuple[float, float, float]) -> Tuple[float, float, float]:
    m = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return (v[0] / m, v[1] / m, v[2] / m)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


class Camera:
    """针孔相机。`f = D * out_w / frame_w` 保证 master 机位**复现分镜构图**。

    为什么这条要特意保证：白膜的唯一价值是"和分镜对得上"。
    如果 master 机位都跟分镜的构图不一样，用户就没法拿它做决策了。
    """

    def __init__(self, center, dist: float, f: float, out_w: int, out_h: int,
                 az_deg: float = 0.0, el_deg: float = 0.0,
                 offset=(0.0, 0.0, 0.0)):
        self.out_w, self.out_h, self.f = out_w, out_h, f
        az, el = math.radians(az_deg), math.radians(el_deg)
        # 基础视线方向：从 +Z 方向看向场景中心
        d = (math.sin(az) * math.cos(el), math.sin(el),
             math.cos(az) * math.cos(el))
        self.pos = (center[0] + d[0] * dist + offset[0],
                    center[1] + d[1] * dist + offset[1],
                    center[2] + d[2] * dist + offset[2])
        self.forward = _norm((center[0] - self.pos[0], center[1] - self.pos[1],
                              center[2] - self.pos[2]))
        up_hint = (0.0, 1.0, 0.0)
        if abs(_dot(self.forward, up_hint)) > 0.98:      # 俯视时换参考上方向
            up_hint = (0.0, 0.0, -1.0)
        self.right = _norm(_cross(self.forward, up_hint))
        self.up = _cross(self.right, self.forward)

    def project(self, p) -> Optional[Tuple[float, float, float]]:
        """世界点 → (屏幕 x, 屏幕 y, 相机空间深度)。在相机后方返回 None。"""
        v = (p[0] - self.pos[0], p[1] - self.pos[1], p[2] - self.pos[2])
        zc = _dot(v, self.forward)
        if zc <= 1e-3:
            return None
        xc = _dot(v, self.right)
        yc = _dot(v, self.up)
        return (self.out_w / 2.0 + self.f * xc / zc,
                self.out_h / 2.0 - self.f * yc / zc, zc)


# ──────────────────────── 场景构建 ────────────────────────


def build_scene(spec: Dict[str, Any]) -> Dict[str, Any]:
    """把分镜 spec 变成三维场景（盒子 + 运动轨迹）。

    坐标映射（**说清楚，因为这是全模块最容易搞错的地方**）：
      · 世界 X ← 主体的 `x` + `width/2`（画面像素，横向）
      · 世界 Y ← `height`（高度，脚在 Y=0 地面上）
      · 世界 Z ← `z`（纵深；**越大越靠近 master 机位**，沿用 B 档的约定）
      · 世界深度尺寸 ← `depth`（没给就按宽度的 0.6 估，并在 lint 里提醒）
    """
    frame = spec.get("frame") or {}
    fw = float(frame.get("width") or 1920)
    fh = float(frame.get("height") or 1080)
    boxes: List[Dict[str, Any]] = []

    for i, s in enumerate(spec.get("subjects") or []):
        if not isinstance(s, dict):
            continue
        try:
            x = float(s.get("x"))
            w = float(s.get("width") or 0)
            h = float(s.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        z = float(s.get("z") or 0.0)
        d = float(s.get("depth") or max(1.0, w * 0.6))
        boxes.append({
            "i": i,
            "name": s.get("name") or f"主体{i+1}",
            "cx": x + w / 2.0,          # 世界 X 中心
            "top": h,                   # 世界 Y 上沿（脚在 0）
            "z": z,
            "hw": w / 2.0,
            "d": d,
            "shade": SHADES[i % len(SHADES)],
            "humanoid": bool(s.get("humanoid")),
            "facing": s.get("facing") or "right",
            "move": s.get("move") or [],
        })

    if not boxes:
        return {"ok": False, "error": "没有带 x/width/height 的主体，C 档渲不了", "boxes": []}

    xs = [b["cx"] - b["hw"] for b in boxes] + [b["cx"] + b["hw"] for b in boxes]
    zs = [b["z"] for b in boxes] + [b["z"] + b["d"] for b in boxes]
    ys = [b["top"] for b in boxes]
    wx0, wx1 = min(xs), max(xs)
    wz0, wz1 = min(zs), max(zs)
    span_x = max(1.0, wx1 - wx0)
    span_z = max(1.0, wz1 - wz0)
    span_y = max(1.0, max(ys))

    center = ((wx0 + wx1) / 2.0, span_y * 0.45, (wz0 + wz1) / 2.0)
    # 距离要够远才不变形；太近会广角畸变，太远就没有视差
    dist = max(span_x, span_z, span_y * 1.6) * 1.55 + 240.0

    return {"ok": True, "boxes": boxes, "frame_w": fw, "frame_h": fh,
            "center": center, "dist": dist,
            "span": (span_x, span_y, span_z),
            "ground_y": 0.0}


def _sample_move(box: Dict[str, Any], t: float) -> Tuple[float, float]:
    """按进度 t（0~1）取主体的横向/纵向偏移。

    `move` 是画面像素坐标的点列（沿用分镜格式）。这里只取**横向偏移**
    和**纵向抬升**：横向直接加到世界 X；纵向把它当作"离地高度"，
    也就是人物跳起/台阶/上坡。不做这件事的话，走位就退化成原地踏步。
    """
    mv = box.get("move") or []
    pts: List[Tuple[float, float]] = []
    for p in mv:
        try:
            pts.append((float(p[0]), float(p[1])))
        except (TypeError, ValueError, IndexError):
            continue
    if len(pts) < 2:
        return (0.0, 0.0)

    seg = t * (len(pts) - 1)
    k = min(int(seg), len(pts) - 2)
    f = seg - k
    x0, y0 = pts[k]
    x1, y1 = pts[k + 1]
    return (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)


def _camera_motion(camera: str, t: float) -> Tuple[float, float, float, Tuple[float, float, float]]:
    """把运镜名变成随时间变化的相机参数。

    返回 (方位角增量, 俯仰角增量, 距离倍率, 世界偏移)。
    ★ 这是 C 档比 A 档强的地方：A 档靠 ffmpeg 的 zoompan 假装推拉，
      这里是真的把相机在三维空间里移动 —— 所以**换机位后运镜依然成立**。
    """
    e = t  # 0 → 1
    if camera == "dolly_in":
        return (0.0, 0.0, 1.0 - 0.22 * e, (0.0, 0.0, 0.0))
    if camera == "dolly_out":
        return (0.0, 0.0, 1.0 + 0.28 * e, (0.0, 0.0, 0.0))
    if camera == "pan_left":
        return (-14.0 * e, 0.0, 1.0, (0.0, 0.0, 0.0))
    if camera == "pan_right":
        return (14.0 * e, 0.0, 1.0, (0.0, 0.0, 0.0))
    if camera == "tilt_up":
        return (0.0, 12.0 * e, 1.0, (0.0, 0.0, 0.0))
    if camera == "tilt_down":
        return (0.0, -12.0 * e, 1.0, (0.0, 0.0, 0.0))
    if camera == "track":
        return (0.0, 0.0, 1.0, (-160.0 * e, 0.0, 0.0))
    return (0.0, 0.0, 1.0, (0.0, 0.0, 0.0))


# ──────────────────────── 单帧绘制 ────────────────────────


def _convex_hull(pts: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Andrew 单调链凸包。用于画**真实轮廓**而不是 8 角点的包围盒。

    ★ 为什么不用包围盒：俯视时机位看到的是盒子顶面，8 角点的包围盒
      比实际轮廓大一圈，识别条纹和"鼻子"就飘到盒子外面去了（第一版实测）。
    """
    ps = sorted(set((round(x, 3), round(y, 3)) for x, y in pts))
    if len(ps) <= 2:
        return ps

    def half(seq):
        out: List[Tuple[float, float]] = []
        for p in seq:
            while len(out) >= 2:
                (x1, y1), (x2, y2) = out[-2], out[-1]
                if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) <= 0:
                    out.pop()
                else:
                    break
            out.append(p)
        return out

    lower = half(ps)
    upper = half(list(reversed(ps)))
    return lower[:-1] + upper[:-1]


def _shade(v: int, mul: float) -> int:
    return max(0, min(255, int(v * mul)))


#: 盒子的 6 个面 —— 每个面 4 个角点的索引（角点编号 bit0=x, bit1=y, bit2=z）
_BOX_FACES: List[Tuple[Tuple[int, int, int, int], Tuple[float, float, float]]] = [
    ((0, 2, 3, 1), (0.0, 0.0, -1.0)),     # z 小的一侧（朝 master 机位）
    ((4, 5, 7, 6), (0.0, 0.0, 1.0)),      # z 大的一侧
    ((0, 1, 5, 4), (-1.0, 0.0, 0.0)),     # x 小的一侧
    ((2, 6, 7, 3), (1.0, 0.0, 0.0)),      # x 大的一侧
    ((0, 4, 6, 2), (0.0, -1.0, 0.0)),     # 底面
    ((1, 3, 7, 5), (0.0, 1.0, 0.0)),      # 顶面（俯视时最显眼）
]


def _box_corners(wx: float, hw: float, z: float, d: float,
                 my: float, top: float) -> List[Tuple[float, float, float]]:
    """8 个角点，索引 bit0=x(0=小,1=大) bit1=y(0=底,1=顶) bit2=z(0=近,1=远)。"""
    out = []
    for i in range(8):
        x = wx + (hw if (i & 1) else -hw)
        y = (top + my) if (i & 2) else my
        zz = z + (d if (i & 4) else 0.0)
        out.append((x, y, zz))
    return out


def draw_frame(scene: Dict[str, Any], angle: str, t: float,
               out_w: int, out_h: int, camera_move: str = "static") -> FastCanvas:
    """画一帧。`t` 是 0~1 的时间进度。

    画法：**真三维盒子 + 背面剔除 + 画家算法**。
    每个盒子投影 8 个角点，按法线挑出朝向相机的 3 个面填多边形，
    面的明度不同（顶面最亮、侧面最暗），所以任何机位下都能看出体块。
    """
    c = FastCanvas(out_w, out_h, BG)
    az0, el0, dmul = ANGLES.get(angle, ANGLES["master"])

    madj = _camera_motion(camera_move, t)
    cam = Camera(scene["center"], scene["dist"] * dmul * madj[2],
                 scene["dist"] * dmul * madj[2] * out_w / scene["frame_w"],
                 out_w, out_h,
                 az_deg=az0 + madj[0], el_deg=el0 + madj[1],
                 offset=madj[3])

    # ── 地面：填一块**真地板**（比背景略暗），再铺网格线 ──
    #    ★ 第一版是画地板四边形的**轮廓线**，结果那几条线在画面里
    #      被读成"乱线"而不是"地面"。填成面之后才有"站在地上"的感觉。
    gx0 = scene["center"][0] - scene["span"][0] * 1.3
    gx1 = scene["center"][0] + scene["span"][0] * 1.3
    gz0 = scene["center"][2] - scene["span"][2] * 1.3
    gz1 = scene["center"][2] + scene["span"][2] * 1.3
    gp = [cam.project(p) for p in ((gx0, 0.0, gz0), (gx1, 0.0, gz0),
                                   (gx1, 0.0, gz1), (gx0, 0.0, gz1))]
    if all(p is not None for p in gp):
        c.fill_poly([(p[0], p[1]) for p in gp], GROUND)   # type: ignore[index]
        # 网格：横向 5 条 + 纵向 5 条，细一点，别抢主体
        for gi in range(1, 5):
            u = gi / 5.0
            zz = gz0 + (gz1 - gz0) * u
            seg = [cam.project((gx0, 0.0, zz)), cam.project((gx1, 0.0, zz))]
            if all(s is not None for s in seg):
                _thin_line(c, seg[0], seg[1], GRID)       # type: ignore[index]
            xx = gx0 + (gx1 - gx0) * u
            seg = [cam.project((xx, 0.0, gz0)), cam.project((xx, 0.0, gz1))]
            if all(s is not None for s in seg):
                _thin_line(c, seg[0], seg[1], GRID)       # type: ignore[index]

    # ── 盒子：远的先画（画家算法）──
    order = []
    for b in scene["boxes"]:
        # move 给的是**画面像素 x**，和世界 X 同量纲，直接用
        mx, my = _sample_move(b, t)
        wx = mx if b.get("move") else b["cx"]
        cz = b["z"] + b["d"] / 2.0
        dx = wx - cam.pos[0]
        dz = cz - cam.pos[2]
        order.append((dx * dx + dz * dz, b, wx, my))
    order.sort(key=lambda r: -r[0])

    for _, b, wx, my in order:
        corners3 = _box_corners(wx, b["hw"], b["z"], b["d"], my, b["top"])
        proj = [cam.project(p) for p in corners3]
        if any(p is None for p in proj):
            continue
        p2 = [(p[0], p[1]) for p in proj]                 # type: ignore[index]

        # 可见面 = 外法线与"相机→面中心"同向为背面，反向为正面
        visible = []
        for idx, (f, nrm) in enumerate(_BOX_FACES):
            cx = sum(corners3[k][0] for k in f) / 4.0
            cy = sum(corners3[k][1] for k in f) / 4.0
            cz = sum(corners3[k][2] for k in f) / 4.0
            to_cam = (cam.pos[0] - cx, cam.pos[1] - cy, cam.pos[2] - cz)
            if _dot(nrm, to_cam) > 0:
                visible.append(idx)
        if not visible:
            visible = [0]

        for idx in visible:
            f, _n = _BOX_FACES[idx]
            mul = {5: 1.22, 4: 0.72, 0: 1.0, 1: 0.94}.get(idx, 0.86)
            c.fill_poly([p2[k] for k in f], _shade(b["shade"], mul))

        # 轮廓：可见面的**凸包**，不是 8 角点的包围盒（见 _convex_hull 注释）
        vis_pts: List[Tuple[float, float]] = []
        for idx in visible:
            f, _n = _BOX_FACES[idx]
            vis_pts.extend(p2[k] for k in f)
        hull = _convex_hull(vis_pts) or p2
        for k in range(len(hull)):
            _thin_line(c, hull[k], hull[(k + 1) % len(hull)], EDGE)

        xs = [q[0] for q in vis_pts] or [p[0] for p in p2]
        ys = [q[1] for q in vis_pts] or [p[1] for p in p2]
        bx, by = int(min(xs)), int(min(ys))
        bw, bh = int(max(xs) - min(xs)), int(max(ys) - min(ys))
        if bw < 2 or bh < 2:
            continue

        # ★ 识别标记：第 i 个主体画 i+1 条竖条纹。
        #   没有标记的话几个灰块长得一模一样，用户根本分不清谁是谁 ——
        #   那这张白膜就没法用来做决策。
        n_stripe = min(5, b["i"] + 1)
        sw = max(2, bw // (n_stripe * 3 + 2))
        for k in range(n_stripe):
            c.rect(bx + sw + k * sw * 2, by + max(3, bh // 12), sw,
                   max(4, bh // 3), EDGE)
        # 人形：朝向标记（一侧凸出的小块 = 鼻子）
        if b["humanoid"]:
            nose_w = max(2, bw // 6)
            nx = bx + bw - 1 if b["facing"] == "right" else bx - nose_w + 1
            c.rect(nx, by + max(2, bh // 10), nose_w, max(3, bh // 10), EDGE)

    # 三分构图线（很淡，只做参考）
    for k in (1, 2):
        c.hline(0, out_w, out_h * k // 3, GRID)
        o = (out_w * k // 3)
        for yy in range(out_h):
            if 0 <= o < out_w:
                c.px[yy * out_w + o] = GRID
    return c


def _thin_line(c: FastCanvas, a, b, v: int) -> None:
    """两点之间画一条 1px 线（网格用，不需要抗锯齿）。"""
    x0, y0 = a[0], a[1]
    x1, y1 = b[0], b[1]
    steps = max(2, int(max(abs(x1 - x0), abs(y1 - y0))))
    if steps > 4000:
        return
    for s in range(steps + 1):
        u = s / steps
        x = int(x0 + (x1 - x0) * u)
        y = int(y0 + (y1 - y0) * u)
        if 0 <= x < c.w and 0 <= y < c.h:
            c.px[y * c.w + x] = v


# ──────────────────────── 编码 ────────────────────────


def _ffmpeg() -> str:
    from core.ffmpeg_manager import get_ffmpeg_for_postprocess
    return get_ffmpeg_for_postprocess()


def _encode(frames_dir: str, pattern: str, out_path: str,
            duration: float, out_w: int, out_h: int) -> Dict[str, Any]:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(), "-y", "-v", "error",
        "-framerate", "24", "-i", os.path.join(frames_dir, pattern),
        "-vf", f"scale={out_w}:{out_h}:flags=neighbor",
        "-t", f"{duration:.3f}", "-r", "24",
        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "20", out_path,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600)
    if p.returncode != 0 or not os.path.exists(out_path):
        return {"ok": False, "error": f"ffmpeg rc={p.returncode}: {(p.stderr or '')[:300]}"}
    return {"ok": True, "path": out_path}


def _concat(clips: List[str], out_path: str) -> Dict[str, Any]:
    """把多个机位拼成一条"覆盖片"：一条视频里把所有机位连着看一遍。

    ★ 为什么要这条：用户要判断"哪个机位好"，来回点四个文件很烦；
      拼成一条 20 秒的片子，一次看完就能选。
    """
    if not clips:
        return {"ok": False, "error": "没有片段可拼"}
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    lst = out_path + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for c in clips:
            f.write("file '" + str(c).replace("\\", "/").replace("'", "'\\''") + "'\n")
    cmd = [_ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
           "-i", lst, "-c", "copy", out_path]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600)
    if p.returncode != 0 or not os.path.exists(out_path):
        # copy 失败（编码参数不一致）就重编码一次，别直接放弃
        cmd = [_ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
               "-i", lst, "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "20", "-pix_fmt", "yuv420p", out_path]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900)
    try:
        os.remove(lst)
    except OSError:
        pass
    if p.returncode != 0 or not os.path.exists(out_path):
        return {"ok": False, "error": f"concat rc={p.returncode}: {(p.stderr or '')[:300]}"}
    return {"ok": True, "path": out_path}


def render_coverage(spec: Dict[str, Any], out_dir: str, *,
                    angles: Optional[List[str]] = None,
                    render_w: int = 640, render_h: int = 360,
                    out_w: int = 1280, out_h: int = 720,
                    fps: int = 24) -> Dict[str, Any]:
    """C 档主入口：**同一场戏、多个机位、各出一段视频**。

    Returns:
        {ok, angles:[{name,label,path,frames}], cut, sheet, dir, note, error?}
    """
    scene = build_scene(spec)
    if not scene.get("ok"):
        return {"ok": False, "error": scene.get("error") or "场景构建失败",
                "angles": []}

    angles = [a for a in (angles or DEFAULT_ANGLES) if a in ANGLES] or DEFAULT_ANGLES
    duration = float(spec.get("duration") or 5.0)
    camera_move = str(spec.get("camera") or "static")
    n_frames = max(2, int(round(duration * fps)))

    out_dir = str(out_dir)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="vf_cov_", dir=None)

    made: List[Dict[str, Any]] = []
    first_frames: List[str] = []
    try:
        for a in angles:
            fdir = os.path.join(tmp, a)
            Path(fdir).mkdir(parents=True, exist_ok=True)
            for fi in range(n_frames):
                t = fi / max(1, n_frames - 1)
                c = draw_frame(scene, a, t, render_w, render_h, camera_move)
                p = os.path.join(fdir, f"f{fi:04d}.pgm")
                c.to_pgm(p)
                if fi == 0:
                    first_frames.append(p)
            mp4 = os.path.join(out_dir, f"cam_{a}.mp4")
            enc = _encode(fdir, "f%04d.pgm", mp4, duration, out_w, out_h)
            if not enc.get("ok"):
                logger.warning("C 档机位 %s 编码失败：%s", a, enc.get("error"))
                continue
            made.append({"name": a, "label": ANGLE_LABELS.get(a, a),
                         "path": mp4, "frames": n_frames})

        if not made:
            return {"ok": False, "error": "所有机位都编码失败", "angles": []}

        cut = os.path.join(out_dir, "coverage_cut.mp4")
        cutres = _concat([m["path"] for m in made], cut)

        # 覆盖联络表：每个机位首帧拼一行（PGM 交给 ffmpeg 转 PNG）
        sheet_pgm = os.path.join(out_dir, "coverage_sheet.pgm")
        _sheet(first_frames, sheet_pgm)

        return {
            "ok": True,
            "angles": made,
            "cut": cut if cutres.get("ok") else "",
            "cut_error": "" if cutres.get("ok") else cutres.get("error", ""),
            "sheet": sheet_pgm,
            "dir": out_dir,
            "duration": duration,
            "frames_each": n_frames,
            "note": (f"C 档：{len(made)} 个机位各出一段 {duration:.1f} 秒白膜"
                     f"（{'/'.join(m['label'] for m in made)}）。"
                     "有真透视与前后遮挡，用来**选机位**和**看运动中的遮挡**。"
                     "纯本地渲染，成本 0。"),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sheet(frame_paths: List[str], out_path: str) -> str:
    """把各机位首帧横向拼成一张联络表（PPM 拼接，ffmpeg 能读）。"""
    canvases: List[FastCanvas] = []
    for p in frame_paths:
        try:
            with open(p, "rb") as f:
                data = f.read()
            # 解析 P5 头
            parts = data.split(b"\n", 3)
            if len(parts) < 4 or parts[0].strip() != b"P5":
                continue
            w, h = (int(x) for x in parts[1].split())
            px = parts[3][:w * h]
            c = FastCanvas(w, h, 0)
            c.px[:] = px
            canvases.append(c)
        except Exception:
            continue
    if not canvases:
        return ""
    gap = 8
    total_w = sum(c.w for c in canvases) + gap * (len(canvases) + 1)
    total_h = max(c.h for c in canvases) + gap * 2
    sheet = FastCanvas(total_w, total_h, 0x20)
    ox = gap
    for c in canvases:
        for yy in range(c.h):
            src = yy * c.w
            dst = (gap + yy) * total_w + ox
            sheet.px[dst:dst + c.w] = c.px[src:src + c.w]
        ox += c.w + gap
    return sheet.to_pgm(out_path)


def render_plan_for(spec: Dict[str, Any]) -> Dict[str, Any]:
    """这个镜头该走哪一档 —— C 档的判定条件写在这里，便于单测。

    返回值统一带上 `builtin` / `free` / `cost_note`：
    这三个字段是给 agent 和前端看的，必须每档都有 ——
    少了 `free` 会让调用方以为白膜要花钱（实测被单测抓过）。
    """
    subs = [s for s in (spec.get("subjects") or []) if isinstance(s, dict)]
    n = len(subs)
    has_move = any((s.get("move") or []) for s in subs)
    has_depth = any((s.get("z") is not None or s.get("depth") is not None)
                    for s in subs)
    cam = str(spec.get("camera") or "static")
    moving_cam = cam not in ("static", "")
    try:
        multi_cam = int(spec.get("cameras") or 1) > 1
    except (TypeError, ValueError):
        multi_cam = False

    base = {"builtin": True, "free": True,
            "cost_note": "白膜阶段成本为 0，可无限重试"}
    # ★ 判档只看"有没有真的空间信息"。**不能只看主体个数** ——
    #   两个没有 z/depth 的主体在俯视图里会叠在同一条线上，
    #   画出来的"走位图"是假的。实测被单测抓过一次。
    if multi_cam or (has_move and moving_cam) or (n >= 3 and (has_depth or has_move)):
        return dict(base, tier="C",
                    why=(f"{'声明了多机位' if multi_cam else f'{n} 个主体'}"
                         f"{'、有走位' if has_move else ''}"
                         f"{'、有运镜' if moving_cam else ''}"
                         f" → 需要多机位**视频**才能选出机位、"
                         f"看清运动过程中的遮挡"),
                    angles=DEFAULT_ANGLES)
    if has_depth or (has_move and n >= 2):
        return dict(base, tier="B",
                    why="需要三维走位/遮挡关系 → 多机位静态联络表（俯视/正面/侧面）",
                    angles=["top", "front", "side"])
    return dict(base, tier="A",
                why="只需验节奏与构图 → 平面灰模视频，最快",
                angles=[])
