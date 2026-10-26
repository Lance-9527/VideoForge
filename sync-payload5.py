"""同步本轮全部后端改动进冻结载荷（**含 data/previz_blender/ 渲染器**）。"""
import hashlib
import py_compile
import shutil
from pathlib import Path

SRC = Path(r'D:\VideoForge-dev\backend')
PAY = Path(r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\resources\videoforge-sidecar\_internal\backend')

FILES = [
    'main.py',
    'core/keyframe.py',
    'core/scene_anchors.py',
    'core/shot_chain.py',
    'core/shot_prompts.py',
    'core/longvideo.py',
    'core/agent_tools.py',
    'core/agent_tools_stages.py',   # ★ 出图/白膜并发
    'core/previz_design.py',        # ★ 白膜提示词升级 + 几何体检 + field 定义
    'core/previz_notes.py',         # ★ 白膜规格说明导出
    'core/previz_blender.py',       # ★ 渲染后导出规格说明
    'core/compose.py',
    'core/orchestrator.py',
    'data/previz_blender/design.py',        # ★ 人偶朝向标记
    'data/previz_blender/render_spec.py',
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
    print(f'  {rel:34} {before} -> {hashlib.md5(d.read_bytes()).hexdigest()[:8]}')

for rel in FILES:
    t = PAY / rel
    if t.exists() and t.suffix == '.py':
        py_compile.compile(str(t), doraise=True)
print('载荷语法 OK')
