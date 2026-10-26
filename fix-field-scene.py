"""白膜「野外」场景重做：真正开阔的地平线（不要再画墙）。

★ 根因（2026-10-22 真机看帧发现）：
  field 分支里硬编码了「两座房子 + 一条 6 米高的 skyline 大墙」—— 那是"村口"模板。
  于是"雪后田间小路"这种野外场景，渲出来四面是墙，看着像室内房间（用户说"白膜没看到、
  感觉跟成片不一致"的根因之一）。
  而规格里 set.kind 明明是 field（8x20），**规格是对的，是渲染实现不对**。

改法：
  · field/outdoor → 开阔地平线：远处低矮起伏 + 稀疏树剪影 + 很矮的地平线带，**不画墙、不画房子**；
  · village → 保留原来的"村口"（围栏 + 房子 + 天际线），需要时仍可用；
  · 道具一律交给 spec.props（模型按剧本给的柴担/雪堆等）。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
p = Path(r'D:\VideoForge-dev\backend\data\previz_blender\render_spec.py')
text = p.read_text(encoding='utf-8')
shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + STAMP))

old = '''    elif kind in ("field", "village", "outdoor"):
        # 野外/村口：地面 + 围栏 + 远屋
        for i in range(5):
            RB.cube("fence%d" % i, (-w / 2 + w * (i + 0.5) / 5, d / 2 - 0.4, 0.55),
                    (0.10, 0.10, 1.10), "prop")
        RB.cube("fence_rail", (0, d / 2 - 0.4, 0.95), (w, 0.08, 0.08), "prop")
        for i, (bx, by, bh) in enumerate(((-4.5, 6.5, 2.6), (5.2, 8.0, 3.0))):
            RB.cube("house%d" % i, (bx, by, bh / 2), (4.0, 3.4, bh), "wall")
        RB.cube("skyline", (0, d / 2 + 10, 3.0), (max(w, 16) * 2, 1.0, 6.0), "sky")'''

new = '''    elif kind == "village":
        # 村口：地面 + 围栏 + 远屋 + 天际线（保留旧行为）
        for i in range(5):
            RB.cube("fence%d" % i, (-w / 2 + w * (i + 0.5) / 5, d / 2 - 0.4, 0.55),
                    (0.10, 0.10, 1.10), "prop")
        RB.cube("fence_rail", (0, d / 2 - 0.4, 0.95), (w, 0.08, 0.08), "prop")
        for i, (bx, by, bh) in enumerate(((-4.5, 6.5, 2.6), (5.2, 8.0, 3.0))):
            RB.cube("house%d" % i, (bx, by, bh / 2), (4.0, 3.4, bh), "wall")
        RB.cube("skyline", (0, d / 2 + 10, 3.0), (max(w, 16) * 2, 1.0, 6.0), "sky")

    elif kind in ("field", "outdoor"):
        # ★ 野外：**开阔地平线**（2026-10-22 重做）。
        #   原来这里硬编码了"两座房子 + 6 米高的 skyline 大墙"（那是村口模板），
        #   于是"雪后田间小路"这种野外被渲成四面墙的屋子 —— 白膜看着像室内房间，
        #   用户根本没法用它判断机位/走位。
        #   现在只画：远处低矮起伏（田埂/雪堆）+ 稀疏树剪影 + 一条很矮的地平线带。
        #   具体的道具（柴担、雪堆、树…）一律交给 spec.props —— 那是按剧本给的。
        _edge = d / 2 + 1.0
        for i in range(4):
            RB.cube("mound%d" % i,
                    (-w * 0.75 + w * 0.5 * i, _edge + 2.0 + (i % 2) * 2.2, 0.30),
                    (w * 0.45, 2.4, 0.60), "ground")
        for i in range(6):
            tx = -w * 0.85 + w * 0.34 * i
            ty = _edge + 3.2 + (i % 3) * 2.6
            th = 2.2 + (i % 3) * 0.7
            RB.cube("trunk%d" % i, (tx, ty, th / 2), (0.16, 0.16, th), "wall")
            RB.cube("crown%d" % i, (tx, ty, th + 0.35), (0.9, 0.9, 1.0), "prop")
        # 地平线带：很矮（0.8m），不构成"墙"
        RB.cube("horizon", (0, _edge + 14.0, 0.40), (max(w, 22) * 2, 0.6, 0.80), "sky")'''

if old in text:
    p.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('field 场景已重做为开阔地平线（village 保留旧行为）')
else:
    print('未匹配 field 分支')

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')
