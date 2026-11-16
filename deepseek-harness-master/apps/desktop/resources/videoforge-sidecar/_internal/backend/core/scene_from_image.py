"""图片 → 可拆解的 3D 场景（分层深度图 LDI，2026-11-01 进产品）

## 为什么是 LDI，不是"AI 生成 3D"
本机没有可用的图生 3D / 深度模型权重（拉不下来、也不能联网付费）。
LDI 是**几何上诚实**的做法：把照片拆成很多片，每片放到一个"相机距离"上，
用精确的针孔投影摆位 ⇒

    X = X0 * (d / D),  Y = Y0 * (d / D),  z = D - d

从原机位 (0,0,D) 看，所有片叠起来**就是原图**；换机位 ⇒ 片间错位 ⇒ **真视差**。

## 与「可拆解物件」的关系
产物是**一个** splat 层（不是一堆语义物体）。拆件由 SuperSplat 自己的
`select_property` + `separate_selection` 完成（客户端「按深度拆」按钮），
拆出来的每一层都能单独显隐/改名/移动 —— 这就是"可拆解的场景物件"。

## 输出格式
SuperSplat 只吃 binary PLY + splat 专用属性，且 **URL 不能带查询串**
（它用 `url.split('/').pop()` 当文件名，带 `?` 就识别不出格式）。
所以这里只负责生成文件，路由用 `/api/studio/splat/files/{name}` 这种**路径式** URL 提供。
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image

SH_C0 = 0.28209479177387814

#: 参考深度（相机到"原图像平面"的距离，米）
D_CAM = 6.0
#: 参考深度处画面的半宽（米）
HALF_W = 3.0
#: 最近片 = D_CAM * NEAR_K
NEAR_K = 0.45
#: splat 半径系数
SIGMA_K = 1.7


def _box_blur_axis(values: np.ndarray, radius: int, axis: int) -> np.ndarray:
    """沿一条轴做盒式模糊（cumsum 实现，边缘复制）。一维/二维都支持。"""
    if radius <= 0:
        return values.astype(np.float32)
    source = values.astype(np.float32)
    if source.ndim == 1:
        padded = np.pad(source, (radius, radius), mode='edge')
        cumulative = np.concatenate([[0.0], np.cumsum(padded, dtype=np.float64)])
        window = cumulative[2 * radius + 1:] - cumulative[:cumulative.shape[0] - 2 * radius - 1]
        return (window / (2 * radius + 1)).astype(np.float32)
    moved = np.moveaxis(source, axis, 0)
    padded = np.pad(moved, ((radius, radius), (0, 0)), mode='edge')
    cumulative = np.cumsum(padded, axis=0, dtype=np.float64)
    cumulative = np.concatenate([np.zeros((1, *cumulative.shape[1:]), dtype=np.float64), cumulative], axis=0)
    window = cumulative[2 * radius + 1:] - cumulative[:cumulative.shape[0] - 2 * radius - 1]
    return np.moveaxis((window / (2 * radius + 1)).astype(np.float32), 0, axis)


def _blur2d(values: np.ndarray, radius: int) -> np.ndarray:
    return _box_blur_axis(_box_blur_axis(values, radius, 0), radius, 1)


def _norm(values: np.ndarray) -> np.ndarray:
    low = float(values.min())
    high = float(values.max())
    return (values - low) / (high - low + 1e-6)


def build_ldi_ply(data: bytes, size: int = 512) -> tuple[bytes, int]:
    """图片字节 → splat 规范的 binary PLY 字节。返回 (ply, 点数)。"""
    image = Image.open(io.BytesIO(data)).convert('RGB').resize((size, size), Image.LANCZOS)
    rgb = np.asarray(image).astype(np.float32) / 255.0

    lum = rgb.mean(axis=2)
    sat = rgb.max(axis=2) - rgb.min(axis=2)
    detail = np.sqrt(
        np.abs(np.diff(lum, axis=0, prepend=lum[:1])) ** 2
        + np.abs(np.diff(lum, axis=1, prepend=lum[:, :1])) ** 2,
    )
    row_lum = np.repeat(_box_blur_axis(lum.mean(axis=1), 12, 0)[None, :], size, axis=0)
    row_sat = np.repeat(_box_blur_axis(sat.mean(axis=1), 12, 0)[None, :], size, axis=0)
    row_det = np.repeat(_box_blur_axis(detail.mean(axis=1), 12, 0)[None, :], size, axis=0)

    depth_raw = (
        0.34 * (1.0 - _norm(lum))
        + 0.18 * _norm(sat)
        + 0.26 * _norm(detail)
        + 0.12 * (1.0 - _norm(row_lum))
        + 0.06 * (1.0 - _norm(row_sat))
        + 0.04 * (1.0 - _norm(row_det))
    )
    depth = _norm(_blur2d(depth_raw, 5))          # 0 = 远，1 = 近

    scale = 2.0 * HALF_W / size                   # 米/像素（参考深度处）
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
    d = (D_CAM * (1.0 - depth * (1.0 - NEAR_K))).astype(np.float32)
    k = d / D_CAM
    z = D_CAM - d

    rng = np.random.default_rng(41)
    x = (xs - size / 2.0) * scale * k + (rng.random(depth.shape, dtype=np.float32) - 0.5) * scale * 0.4
    y = (ys - size / 2.0) * scale * k + (rng.random(depth.shape, dtype=np.float32) - 0.5) * scale * 0.4
    zz = z + (rng.random(depth.shape, dtype=np.float32) - 0.5) * 0.015
    sigma = scale * k * SIGMA_K

    count = int(size * size)
    flat = lambda array: array.reshape(-1)  # noqa: E731
    dc = (rgb - 0.5) / SH_C0

    dtype = np.dtype([
        ('x', '<f4'), ('y', '<f4'), ('z', '<f4'),
        ('f_dc_0', '<f4'), ('f_dc_1', '<f4'), ('f_dc_2', '<f4'),
        ('opacity', '<f4'),
        ('scale_0', '<f4'), ('scale_1', '<f4'), ('scale_2', '<f4'),
        ('rot_0', '<f4'), ('rot_1', '<f4'), ('rot_2', '<f4'), ('rot_3', '<f4'),
    ])
    records = np.zeros(count, dtype=dtype)
    records['x'] = flat(x)
    records['y'] = flat(y)
    records['z'] = flat(zz)
    records['f_dc_0'] = flat(dc[:, :, 0])
    records['f_dc_1'] = flat(dc[:, :, 1])
    records['f_dc_2'] = flat(dc[:, :, 2])
    records['opacity'] = 4.59511985013459
    log_sigma = np.log(np.maximum(flat(sigma), 1e-6))
    records['scale_0'] = log_sigma
    records['scale_1'] = log_sigma
    records['scale_2'] = log_sigma
    records['rot_0'] = 1.0

    header = (
        'ply\n'
        'format binary_little_endian 1.0\n'
        f'element vertex {count}\n'
        'property float x\nproperty float y\nproperty float z\n'
        'property float f_dc_0\nproperty float f_dc_1\nproperty float f_dc_2\n'
        'property float opacity\n'
        'property float scale_0\nproperty float scale_1\nproperty float scale_2\n'
        'property float rot_0\nproperty float rot_1\nproperty float rot_2\nproperty float rot_3\n'
        'end_header\n'
    )
    return header.encode('ascii') + records.tobytes(), count


def write_scene_from_image(data: bytes, target: Path, size: int = 512) -> dict:
    """生成并落盘，返回给客户端用的元信息（含「还原原图」的机位）。"""
    ply, count = build_ldi_ply(data, size=size)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ply)
    fov = 2.0 * float(np.degrees(np.arctan((size / 2.0) * (2.0 * HALF_W / size) / D_CAM)))
    return {
        'name': target.name,
        'path': str(target),
        'bytes': len(ply),
        'points': count,
        'camera': {'position': [0.0, 0.0, D_CAM], 'target': [0.0, 0.0, 0.0], 'fov': fov},
        'note': 'LDI 分层深度图：原机位能还原原图，换机位有真实视差；可用「按深度拆」拆成多个物件',
    }
