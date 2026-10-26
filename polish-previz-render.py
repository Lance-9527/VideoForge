"""白膜渲染器打磨（移植 Proma 规范）：
① 人偶加**朝向标记**（Proma：mannequin figures with simple nose and chest marker）
   —— 灰模没有鼻子就看不出人物朝哪，用户的"白膜我看不出走位"多半是这个。
② 场景类型补上 `field`（雪后田间小路）的定义：开阔地平线、无墙无顶。
   之前提示词只解释了"房间/窄巷/街道"的尺寸，模型把"雪后田间小路"渲成了室内房间。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')

# ── ① design.py：人偶加朝向标记 ────────────────────────────────────────
d = Path(r'D:\VideoForge-dev\backend\data\previz_blender\design.py')
text = d.read_text(encoding='utf-8')
old = '''    else:  # 默认站立
        cube(f"{name}_躯", (x, y, tall / 2), (0.30, 0.20, tall * 0.55), key, r)
        cube(f"{name}_头", (x, y, tall + 0.12), (0.20, 0.20, 0.24), key, r)
        for s in (-1, 1):
            cube(f"{name}_臂{s}", (x + s * 0.20, y, tall * 0.65),
                 (0.10, tall * 0.50, 0.10), key, r)
            cube(f"{name}_腿{s}", (x + s * 0.10, y, tall / 4),
                 (0.12, tall * 0.45, 0.12), key, r)'''
new = '''    else:  # 默认站立
        cube(f"{name}_躯", (x, y, tall / 2), (0.30, 0.20, tall * 0.55), key, r)
        cube(f"{name}_头", (x, y, tall + 0.12), (0.20, 0.20, 0.24), key, r)
        for s in (-1, 1):
            cube(f"{name}_臂{s}", (x + s * 0.20, y, tall * 0.65),
                 (0.10, tall * 0.50, 0.10), key, r)
            cube(f"{name}_腿{s}", (x + s * 0.10, y, tall / 4),
                 (0.12, tall * 0.45, 0.12), key, r)
    # ★ 2026-10-22（按 Proma 的白膜规范）：灰模人偶要**一眼看出朝向**，否则
    #   用户在白膜里根本判断不了"人朝哪走"（这正是"白膜看不出走位"的根因）。
    #   做法（与 Proma 的 nose + chest marker 同一思路）：
    #     · 胸口一个小方块标记（正/背面观感不同）
    #     · 头前方一个小楔子（鼻子）—— 朝 -Y（人物默认面向 -Y）
    cube(f"{name}_胸口", (x, y - 0.11, tall * 0.72), (0.10, 0.04, 0.10), key, r)
    _nose_dy = -0.14
    cube(f"{name}_鼻", (x + _nose_dy * math.sin(r), y + _nose_dy * math.cos(r), tall + 0.10),
         (0.06, 0.12, 0.06), key, r)'''
if old in text and '胸口' not in text:
    shutil.copyfile(d, d.with_suffix(d.suffix + '.bak-' + STAMP))
    d.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('① 人偶已加朝向标记（胸口块 + 鼻子楔子）')
else:
    print('① 已存在或未匹配')

# ── ② previz_design：场景类型定义补 field ────────────────────────────
p = Path(r'D:\VideoForge-dev\backend\core\previz_design.py')
text = p.read_text(encoding='utf-8')
old_hard = '''· set 的尺寸要像真地方：窄巷宽 2~3.5m、普通房间 4~7m、街道宽 6~10m；'''
new_hard = '''· set 的尺寸要像真地方：窄巷宽 2~3.5m、普通房间 4~7m、街道宽 6~10m；
  **kind 的含义**：room=有墙有顶的室内；corridor=窄长通道；street=两侧有建筑的城市街道；
  roof=楼顶（四周无墙、有护栏）；field=**开阔野外**（田地/雪原/山野/河边 —— 地平线开阔、
  不要四面墙、不要天花板，远处可以有低矮起伏与稀疏树木剪影）；'''
if old_hard in text:
    shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + STAMP))
    p.write_text(text.replace(old_hard, new_hard, 1), encoding='utf-8')
    print('② 场景类型定义已补 field（开阔野外：地平线、无墙无顶）')
else:
    print('② 已存在或未匹配')

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK (previz_design)')
