"""把本轮改动的后端文件同步进冻结载荷（onedir 里带的是源码，替换即生效）。"""
import hashlib
import shutil
from pathlib import Path

SRC = Path(r'D:\VideoForge-dev\backend')
PAYLOADS = [
    Path(r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\resources\videoforge-sidecar\_internal\backend'),
    Path(r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\.desktop-build\targets\win-x64\unsigned-artifacts\win-unpacked\resources\videoforge-sidecar\_internal\backend'),
]

FILES = [
    'main.py',
    'core/shot_prompts.py',
    'core/orchestrator.py',
    'core/agent_tools.py',
    'core/compose.py',
    'core/longvideo.py',
]


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


for payload in PAYLOADS:
    if not payload.exists():
        print('跳过（不存在）', payload)
        continue
    label = payload.parent.parent.name
    for rel in FILES:
        src = SRC / rel
        if not src.exists():
            print(f'  {label}: 源缺 {rel}')
            continue
        dst = payload / rel
        before = md5(dst)[:8] if dst.exists() else 'new'
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(f'{label}: {rel:26} {before} -> {md5(dst)[:8]}')
    import py_compile
    for rel in FILES:
        target = payload / rel
        if target.exists():
            py_compile.compile(str(target), doraise=True)
    print(f'  {label}: 语法 OK')
