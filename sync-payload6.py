"""同步第 6 轮改动进冻结载荷（白膜骨架步态 + 白膜视频 + 单镜渲染修复）。"""
import hashlib
import py_compile
import shutil
from pathlib import Path

SRC = Path(r'D:\VideoForge-dev\backend')
PAY = Path(r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\resources\videoforge-sidecar\_internal\backend')

FILES = [
    'main.py',                        # ★ /shots 返回 previz_video
    'core/previz_blender.py',         # ★ 帧→白膜视频编码
    'core/previz_ref.py',             # ★ white_model_video：白模参考只吃视频
    'core/agent_tools_production.py',  # ★ render_previz 回传白膜视频路径
    'data/previz_blender/rig.py',      # ★★ 新：骨架/步态/自检足部解算
    'data/previz_blender/render_spec.py',  # ★ 接 rig + only-shot 修复
    'data/previz_blender/design.py',
]

for rel in FILES:
    s = SRC / rel
    d = PAY / rel
    if not s.exists():
        print('  源缺', rel)
        continue
    before = hashlib.md5(d.read_bytes()).hexdigest()[:8] if d.exists() else 'new'
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(s, d)
    print(f'  {rel:38} {before} -> {hashlib.md5(d.read_bytes()).hexdigest()[:8]}')

for rel in FILES:
    t = PAY / rel
    if t.exists() and t.suffix == '.py':
        py_compile.compile(str(t), doraise=True)
print('载荷语法 OK')
