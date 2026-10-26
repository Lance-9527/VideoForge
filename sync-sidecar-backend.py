"""把改好的后端两个文件同步进「冻结载荷」（PyInstaller onedir 里带的是源码）。

载荷里没有 __pycache__，所以替换 .py 后下次启动就生效，不需要重新跑 PyInstaller。
"""
import hashlib
import shutil
from pathlib import Path

SRC = Path(r'D:\VideoForge-dev\backend')
PAYLOADS = [
    Path(r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\resources\videoforge-sidecar\_internal\backend'),
    Path(r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\.desktop-build\targets\win-x64\unsigned-artifacts\win-unpacked\resources\videoforge-sidecar\_internal\backend'),
]

FILES = ['core/compose.py', 'core/longvideo.py']


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


for payload in PAYLOADS:
    if not payload.exists():
        print('跳过（不存在）', payload)
        continue
    for rel in FILES:
        src = SRC / rel
        dst = payload / rel
        before = md5(dst) if dst.exists() else 'n/a'
        shutil.copyfile(src, dst)
        print(f'{payload.parent.parent.name}: {rel} {before[:8]} -> {md5(dst)[:8]}')
    # 语法自检
    import py_compile
    for rel in FILES:
        py_compile.compile(str(payload / rel), doraise=True)
    print('  语法 OK')
