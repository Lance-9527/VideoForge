"""用新管线重新出一版 12 秒 480P（验证：白膜首帧 + 新提示词 + 音色）。

流程：编译该项目的分镜提示词 → 直接调 generate_final(confirm=True) → 落盘。
（v2 的 stage 接口目前不转发 confirm，所以这里沿用上一轮验证过的方式。）
"""
import asyncio
import json
import os
import sys

os.environ.setdefault('VIDEOFORGE_DATA_DIR', os.path.expandvars(r'%LOCALAPPDATA%\VideoForge\data'))
sys.path.insert(0, r'D:\VideoForge-dev\backend')

PID = 'bceee486-2dc3-4134-b3ac-5ca7e048c3d5'
SHOT = '36f95d2c-6169-4657-9b3f-c63fc1efc9f1'


async def main():
    from core import shot_prompts as sp
    from core.agent_tools_production import t_generate_final
    from core import db as dbmod  # noqa: F401  仅为确保 db 初始化路径一致

    # ① 先编译提示词（新管线核心）
    import core.db as _db
    db = _db.get_db(os.environ['VIDEOFORGE_DATA_DIR'])
    n = sp.compile_project_prompts(db, PID)
    print('编译提示词镜数:', n, flush=True)
    shot = db.get_shot(SHOT)
    prompts = sp.stored_prompts(shot)
    print('video_prompt 长度:', len(prompts.get('video_prompt') or ''), flush=True)
    print('白膜首帧:', shot.get('previz_path'), flush=True)

    # ② 出片
    result = await t_generate_final({'shot_id': SHOT, 'confirm': True})
    print('RESULT >>>', str(result)[:1500], flush=True)


asyncio.run(main())
