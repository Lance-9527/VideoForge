"""生成写实关键帧（白膜灰模 + 场景图 + 角色图 → 写实关键帧）。只花一张图的钱。"""
import asyncio
import os
import sys

os.environ.setdefault('VIDEOFORGE_DATA_DIR', os.path.expandvars(r'%LOCALAPPDATA%\VideoForge\data'))
sys.path.insert(0, r'D:\VideoForge-dev\backend')

SHOT = '36f95d2c-6169-4657-9b3f-c63fc1efc9f1'
ROOT = r'D:\VideoForge-dev\backend\data\outputs'


async def main():
    from core.db import Database
    from core import shot_prompts as sp
    from core import keyframe as kf

    db = Database(os.path.join(os.environ['VIDEOFORGE_DATA_DIR'], 'videoforge.db'))
    if hasattr(db, 'init'):
        db.init()
    settings = db.get_all_settings()
    shot = db.get_shot(SHOT)

    sp.compile_project_prompts(db, str(shot.get('project_id')))
    compiled = sp.stored_prompts(shot)
    video_prompt = compiled.get('video_prompt') or ''

    # 关键帧只取「画面质感 + 场景 + 主体」三段（不要镜头运动和动作）
    head = video_prompt
    for marker in ('镜头：', '动作：', '这一镜要讲清楚的是：'):
        if marker in head:
            head = head.split(marker)[0]
    prompt = kf.KEYFRAME_PROMPT + '\n' + head.strip()

    print('=== 关键帧提示词 ===')
    print(prompt[:600])
    print(flush=True)

    result = await kf.build_keyframe(db, shot, settings, output_dir=ROOT, prompt=prompt, use_extra_refs=True)
    print('=== 结果', result, flush=True)


asyncio.run(main())
