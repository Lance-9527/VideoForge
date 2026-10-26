"""同步第 7 轮进冻结载荷：音效垫底（环境声床）+ 混音层 + compose 接线。

本轮同时把 VideoForge 自己在会话里做出来的修复**固化成源码**：
prosody / minimax_tts / adapters / publish / scene_anchors / previz_ref /
agent_tools_production 已经用 sync 前的反向同步（载荷 → 源码）收进源码树，
这里只把这轮新增/改动的三个文件推进载荷。
"""
import hashlib
import py_compile
import shutil
from pathlib import Path

SRC = Path(r'D:\VideoForge-dev\backend')
PAY = Path(r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\resources\videoforge-sidecar\_internal\backend')

FILES = [
    'core/sfxbed.py',      # ★ 新：音效垫底（程序化环境声床，零费用）
    'core/assemble.py',    # ★ mux_film 支持 ambience + 三路 amix + 侧链避让
    'core/compose.py',     # ★ compose 里生成并混入环境声床
    # 下面这些是「把会话里的成功修复固化进源码」之后、保证载荷与源码一致
    'core/prosody.py',
    'core/voice/minimax_tts.py',
    'core/adapters/__init__.py',
    'core/publish.py',
    'core/scene_anchors.py',
    'core/previz_ref.py',
    'core/agent_tools_production.py',
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
    print(f'  {rel:36} {before} -> {hashlib.md5(d.read_bytes()).hexdigest()[:8]}')

for rel in FILES:
    t = PAY / rel
    if t.exists() and t.suffix == '.py':
        py_compile.compile(str(t), doraise=True)
print('载荷语法 OK')
