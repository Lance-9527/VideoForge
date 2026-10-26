"""强制重出：清掉上一版分段，让关键帧首帧路径真正跑到，然后抽首帧对比。"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

os.environ.setdefault('VIDEOFORGE_DATA_DIR', os.path.expandvars(r'%LOCALAPPDATA%\VideoForge\data'))
sys.path.insert(0, r'D:\VideoForge-dev\backend')

PID = 'bceee486-2dc3-4134-b3ac-5ca7e048c3d5'
SHOT = '36f95d2c-6169-4657-9b3f-c63fc1efc9f1'
SEG_DIR = r'D:\VideoForge-dev\backend\data\outputs\bceee486-2dc3-4134-b3ac-5ca7e048c3d5\36f95d2c-6169-4657-9b3f-c63fc1efc9f1'
FFMPEG = r'C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Links\ffmpeg.EXE'


async def main():
    from core.db import Database
    from core.agent_tools_production import t_generate_final

    db = Database(os.path.join(os.environ['VIDEOFORGE_DATA_DIR'], 'videoforge.db'))
    if hasattr(db, 'init'):
        db.init()

    # ① 备份并清掉旧分段（否则会被"复用"跳过）
    if os.path.isdir(SEG_DIR):
        shutil.copytree(SEG_DIR, SEG_DIR + '.bak-' + time.strftime('%H%M%S'), dirs_exist_ok=True)
        for name in os.listdir(SEG_DIR):
            if name.endswith('.mp4'):
                os.remove(os.path.join(SEG_DIR, name))
    shot = db.get_shot(SHOT)
    with db._lock if hasattr(db, '_lock') else __import__('contextlib').nullcontext():
        db.update_shot(SHOT, segments=json.dumps([], ensure_ascii=False))
    print('旧分段已清空', flush=True)

    # ② 重出
    result = await t_generate_final({'shot_id': SHOT, 'confirm': True})
    text = str(result)
    print('RESULT >>>', text[:900], flush=True)

    # ③ 抽首帧并与关键帧对比
    seg = os.path.join(SEG_DIR, 'seg_01.mp4')
    if os.path.isfile(seg):
        out = r'D:\VideoForge-dsh\exp\keyframe-final-first.png'
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-ss', '0.2', '-i', seg, '-frames:v', '1', out], check=False)
        print('首帧已抽出:', out, os.path.exists(out), flush=True)
    else:
        print('没有新的 seg_01.mp4', flush=True)


asyncio.run(main())
