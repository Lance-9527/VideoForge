"""3DGS ply 的**坐标归一化 + 尺度锚定 + 几何证据**。

## 为什么需要这个

摄影测量重建（COLMAP + Brush，见 OOOSplat）出来的点云有两个硬伤：

1. **坐标系是相机系**：Y 轴朝下、原点在第一台相机附近、朝向随机。
   直接丢进 3D 场景里，"地面"是斜的、"上"是歪的，固定机位（电商镜头表的
   yaw/pitch/zoom）全部对不上。
2. **尺度是任意的**：COLMAP 的 SfM 只解出相对尺度，"37 单位"到底是 37cm
   还是 37m **没有任何信息**。商品往场景里一放，比例全错 —— 而电商最不能错
   的就是比例。

所以这里做三件事，缺一不可：

* **摆正**：把场景的"下方向"（Brush 写进 ply 头的 `Vertical axis` 注释）
  旋到 -Y，让 +Y 朝上、地面水平。
* **锚定尺度**：用户给一个参考物的真实尺寸（"桌子宽 1.2m"），据此换算出
  比例尺。没有锚定点就没有尺度 —— 这一步必须由人给，机器猜不出来。
* **落位**：把地面放到 y=0、场景中心放到原点，方便用米制 orbit 机位。

## 顺带产出的"证据"

重建结果对不对，光看渲染图不算数。这里给出可核验的数值：点数、原始包围盒、
竖轴、施加的旋转与缩放、归一化后的包围盒。这些数字会跟着模型一起留在
sidecar 的接口里，UI 与回执都能引用。

## 不动原文件

归一化产物**另存**为新文件（`<name>.aligned.ply`），原始重建结果原样保留。
"模型是独立的" —— 重建结果和它的坐标归一化版本是两件事。
"""

from __future__ import annotations

import io
import json
import math
import os
import re
from pathlib import Path
from typing import Any

import numpy as np

# Brush 写进 ply 头的竖轴注释（单位向量，指向"下"）。
_VERTICAL_RE = re.compile(r"Vertical axis:\s*([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)")

# 锚定方向：宽度 / 高度 / 深度，对应 x / y / z（归一化之后）。
_ANCHOR_AXES = {"width": 0, "depth": 1, "height": 2}


class SplatFormatError(RuntimeError):
    """ply 不是本模块能处理的 3DGS ply。"""


def read_ply_header(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读 3DGS ply 的头：属性表、顶点数、注释、数据起始偏移。

    @param path - ply 文件路径。
    @returns 头部信息字典。
    """
    with io.open(path, "rb") as handle:
        raw = bytearray()
        while True:
            line = handle.readline()
            if not line:
                raise SplatFormatError("ply 头不完整")
            raw += line
            if line.strip() == b"end_header":
                break
        header = raw.decode("ascii", "replace")
        data_offset = handle.tell()

    properties: list[str] = []
    comments: list[str] = []
    count = 0
    fmt = ""
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.startswith("comment"):
            comments.append(stripped[len("comment"):].strip())
        elif stripped.startswith("element vertex"):
            count = int(stripped.split()[-1])
        elif stripped.startswith("property"):
            properties.append(stripped.split()[-1])
        elif stripped.startswith("format"):
            fmt = stripped.split()[1]

    if not properties or count <= 0:
        raise SplatFormatError("ply 头里没有顶点或属性")
    if "binary_little_endian" not in fmt:
        raise SplatFormatError(f"只支持 binary_little_endian，实际是 {fmt!r}")
    for required in ("x", "y", "z"):
        if required not in properties:
            raise SplatFormatError(f"ply 里没有 {required} 属性")

    vertical = None
    for comment in comments:
        matched = _VERTICAL_RE.search(comment)
        if matched is not None:
            vertical = [float(matched.group(index)) for index in (1, 2, 3)]
            break

    return {
        "comments": comments,
        "properties": properties,
        "count": count,
        "data_offset": data_offset,
        "vertical_axis": vertical,
    }


def _vertical_down_axis(header: dict[str, Any]) -> np.ndarray:
    """取场景的"下方向"（单位向量）。

    没有竖轴注释时按"已经是 Y 轴朝上"处理 —— 宁可不动，也不要猜错。

    @param header - ply 头信息。
    @returns 单位向量。
    """
    axis = header.get("vertical_axis")
    if axis is None:
        return np.array([0.0, -1.0, 0.0], dtype=np.float64)
    vector = np.asarray(axis, dtype=np.float64)
    norm = float(np.linalg.norm(vector))
    if norm < 1e-9:
        return np.array([0.0, -1.0, 0.0], dtype=np.float64)
    return vector / norm


def _rotation_to_down_is_minus_y(down: np.ndarray) -> np.ndarray:
    """求把 `down` 旋到 -Y 的旋转矩阵（Rodrigues）。

    @param down - 当前的下方向单位向量。
    @returns 3x3 旋转矩阵。
    """
    target = np.array([0.0, -1.0, 0.0], dtype=np.float64)
    dot = float(np.clip(np.dot(down, target), -1.0, 1.0))
    if dot > 0.999999:
        return np.eye(3, dtype=np.float64)
    if dot < -0.999999:
        # 方向完全相反：绕任意一根垂直于 down 的轴转 180°
        axis = np.cross(down, np.array([1.0, 0.0, 0.0]))
        if float(np.linalg.norm(axis)) < 1e-9:
            axis = np.cross(down, np.array([0.0, 0.0, 1.0]))
        axis /= float(np.linalg.norm(axis))
        return _rodrigues(axis, math.pi)
    axis = np.cross(down, target)
    axis /= float(np.linalg.norm(axis))
    return _rodrigues(axis, math.acos(dot))


def _rodrigues(axis: np.ndarray, angle: float) -> np.ndarray:
    """轴角 → 旋转矩阵。

    @param axis - 单位轴。
    @param angle - 弧度。
    @returns 3x3 旋转矩阵。
    """
    x, y, z = (float(axis[0]), float(axis[1]), float(axis[2]))
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    one = 1.0 - cos_a
    return np.array([
        [cos_a + x * x * one, x * y * one - z * sin_a, x * z * one + y * sin_a],
        [y * x * one + z * sin_a, cos_a + y * y * one, y * z * one - x * sin_a],
        [z * x * one - y * sin_a, z * y * one + x * sin_a, cos_a + z * z * one],
    ], dtype=np.float64)


def _read_xyz(path: str | os.PathLike[str], header: dict[str, Any]) -> np.ndarray:
    """用 memmap 读出所有顶点坐标（不把整份 SH 数据读进内存）。

    @param path - ply 路径。
    @param header - ply 头信息。
    @returns Nx3 的 float64 坐标数组。
    """
    dtype = np.dtype([(name, "<f4") for name in header["properties"]])
    count = header["count"]
    expected = header["data_offset"] + count * dtype.itemsize
    if not os.path.exists(path) or os.path.getsize(path) < expected:
        raise SplatFormatError("ply 文件长度与头部不符")
    points = np.memmap(path, dtype=dtype, mode="r", offset=header["data_offset"], shape=(count,))
    return np.stack([points["x"], points["y"], points["z"]], axis=1).astype(np.float64)


def probe(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读一个 3DGS ply 的几何证据（不写任何文件）。

    @param path - ply 路径。
    @returns 证据字典：点数、包围盒、竖轴、尺度建议。
    """
    header = read_ply_header(path)
    xyz = _read_xyz(path, header)
    minimum = xyz.min(axis=0)
    maximum = xyz.max(axis=0)
    extent = maximum - minimum
    down = _vertical_down_axis(header)
    rotation = _rotation_to_down_is_minus_y(down)
    rotated = xyz @ rotation.T
    rotated_extent = rotated.max(axis=0) - rotated.min(axis=0)
    return {
        "points": int(header["count"]),
        "properties": len(header["properties"]),
        "sh_degree": next((c for c in header["comments"] if c.lower().startswith("sh degree")), None),
        "vertical_axis": down.tolist(),
        "vertical_is_y_up": bool(abs(float(down[1]) + 1.0) < 0.02),
        "min": minimum.tolist(),
        "max": maximum.tolist(),
        "extent": extent.tolist(),
        "extent_after_align": rotated_extent.tolist(),
        "max_edge": float(rotated_extent.max()),
        "centroid": xyz.mean(axis=0).tolist(),
        "median": np.median(xyz, axis=0).tolist(),
        # 尺度建议：给用户几个"参考物 ↔ 米"的换算，别让人自己猜
        "scale_anchors": [
            {"meters": meters, "scale": meters / float(rotated_extent.max())}
            for meters in (0.5, 1.0, 1.2, 2.0, 3.0)
        ],
    }


def normalize(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
    anchor_meters: float | None = None,
    anchor_axis: str = "width",
) -> dict[str, Any]:
    """把一个 3DGS ply 归一化（摆正 + 锚定尺度 + 落位），写到 `target`。

    @param source - 源 ply。
    @param target - 目标 ply（另存，不覆盖源）。
    @param anchor_meters - 参考物的真实尺寸（米）。**给 None 就不缩放**（尺度未知时诚实地不猜）。
    @param anchor_axis - 参考物对应的方向：width/height/depth。
    @returns 归一化证据（含变换参数与结果包围盒）。
    """
    header = read_ply_header(source)
    xyz = _read_xyz(source, header)

    down = _vertical_down_axis(header)
    rotation = _rotation_to_down_is_minus_y(down)
    aligned = xyz @ rotation.T

    # ── 尺度锚定 ──
    extent = aligned.max(axis=0) - aligned.min(axis=0)
    if anchor_meters is not None:
        axis_index = _ANCHOR_AXES.get(anchor_axis, 0)
        span = float(extent[axis_index])
        if span <= 1e-9:
            raise SplatFormatError(f"场景在 {anchor_axis} 方向上没有厚度，无法按它锚定尺度")
        scale = float(anchor_meters) / span
    else:
        scale = 1.0
    scaled = aligned * scale

    # ── 落位：地面 y=0、中心对准原点 ──
    floor_y = float(np.percentile(scaled[:, 1], 1.0))
    center_xz = np.array([
        float(np.median(scaled[:, 0])),
        0.0,
        float(np.median(scaled[:, 2])),
    ])
    placed = scaled.copy()
    placed[:, 1] -= floor_y
    placed[:, 0] -= center_xz[0]
    placed[:, 2] -= center_xz[2]

    # ── 写盘：只改 x/y/z，其余属性（SH、scale、rot、opacity）原样保留 ──
    #    ★ 头部要**重写**：原头里的 `Vertical axis` 注释描述的是变换前的朝向，
    #      照抄会让下游（以及看头的人）以为坐标没动过 —— 头必须说实话。
    new_comments: list[str] = []
    replaced_vertical = False
    for comment in header["comments"]:
        if _VERTICAL_RE.search(comment) is not None:
            new_comments.append("Vertical axis: 0 -1 0")
            replaced_vertical = True
        else:
            new_comments.append(comment)
    if not replaced_vertical:
        new_comments.append("Vertical axis: 0 -1 0")
    new_comments.append(f"Aligned by shop-splat-normalize: scale={scale:.9g} anchor={anchor_axis}={anchor_meters}")
    new_comments.append(f"Aligned floor_y={floor_y:.9g} center_xz=({center_xz[0]:.9g}, {center_xz[2]:.9g})")

    property_lines = [f"property float {name}" for name in header["properties"]]
    head_lines = [
        "ply",
        "format binary_little_endian 1.0",
        *[f"comment {comment}" for comment in new_comments],
        f"element vertex {header['count']}",
        *property_lines,
        "end_header",
        "",
    ]
    new_head = ("\n".join(head_lines)).encode("ascii", "replace")

    dtype = np.dtype([(name, "<f4") for name in header["properties"]])
    block = dtype.itemsize
    with io.open(source, "rb") as src, io.open(target, "wb") as dst:
        src.seek(header["data_offset"])
        dst.write(new_head)
        remaining = header["count"]
        while remaining > 0:
            take = min(4096, remaining)
            raw = src.read(block * take)
            if len(raw) != block * take:
                raise SplatFormatError("ply 数据段长度与头部不符")
            dst.write(raw)
            remaining -= take

    # 坐标要按点写入 —— 分块 memmap 写，避免整份 40MB 进内存。
    points = np.memmap(target, dtype=dtype, mode="r+", offset=len(new_head), shape=(header["count"],))
    points["x"] = placed[:, 0].astype(np.float32)
    points["y"] = placed[:, 1].astype(np.float32)
    points["z"] = placed[:, 2].astype(np.float32)
    points.flush()
    del points

    result_extent = placed.max(axis=0) - placed.min(axis=0)
    return {
        "ok": True,
        "source": str(source),
        "target": str(target),
        "points": int(header["count"]),
        "rotation_applied": rotation.tolist(),
        "vertical_axis_before": down.tolist(),
        "scale": scale,
        "anchor_meters": anchor_meters,
        "anchor_axis": anchor_axis,
        "floor_y_before": floor_y,
        "center_xz": center_xz.tolist(),
        "extent_before": extent.tolist(),
        "extent_after": result_extent.tolist(),
        "max_edge_after": float(result_extent.max()),
        "bytes": int(os.path.getsize(target)),
    }


def normalize_named(
    out_dir: str | os.PathLike[str],
    name: str,
    anchor_meters: float | None = None,
    anchor_axis: str = "width",
) -> dict[str, Any]:
    """在输出目录里找 `name` 并归一化，产物叫 `<stem>.aligned.ply`。

    @param out_dir - 素材目录。
    @param name - ply 文件名。
    @param anchor_meters - 参考物尺寸（米）。
    @param anchor_axis - 参考物方向。
    @returns 归一化证据。
    """
    source = (Path(out_dir) / name).resolve()
    if not source.is_file() or source.suffix.lower() not in {".ply", ".splat"}:
        raise SplatFormatError(f"素材不存在或格式不支持：{name}")
    target = source.with_name(f"{source.stem}.aligned.ply")
    return normalize(source, target, anchor_meters=anchor_meters, anchor_axis=anchor_axis)


if __name__ == "__main__":  # 手动自测入口：python -m core.splat_normalize <ply> [anchor_meters]
    import sys

    target_path = sys.argv[1]
    anchor = float(sys.argv[2]) if len(sys.argv) > 2 else None
    print(json.dumps(probe(target_path), ensure_ascii=False, indent=2))
    if anchor is not None:
        out = str(Path(target_path).with_name(Path(target_path).stem + ".aligned.ply"))
        print(json.dumps(normalize(target_path, out, anchor_meters=anchor), ensure_ascii=False, indent=2))
