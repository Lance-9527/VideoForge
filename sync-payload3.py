"""同步本轮后端改动进冻结载荷（关键帧 + 场景锚点 + 提示词编译 + 检查器）。"""
import hashlib
import py_compile
import shutil
from pathlib import Path

SRC = Path(r'D:\VideoForge-dev\backend')
PAYLOADS = [
    Path(r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\resources\videoforge-sidecar\_internal\backend'),
]
FILES = [
    'main.py',
    'core/keyframe.py',
    'core/scene_anchors.py',
    'core/longvideo.py',
    'core/shot_prompts.py',
    'core/orchestrator.py',
    'core/agent_tools.py',
    'core/compose.py',
]
for payload in PAYLOADS:
    if not payload.exists():
        print('跳过', payload)
        continue
    for rel in FILES:
        src = SRC / rel
        if not src.exists():
            print('  源缺', rel)
            continue
        dst = payload / rel
        before = hashlib.md5(dst.read_bytes()).hexdigest()[:8] if dst.exists() else 'new'
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(f'  {rel:26} {before} -> {hashlib.md5(dst.read_bytes()).hexdigest()[:8]}')
    for rel in FILES:
        t = payload / rel
        if t.exists():
            py_compile.compile(str(t), doraise=True)
    print('  载荷语法 OK')
