"""对照实验：平台到底认不认 role=first_frame？

变量只有一个：**不传角色/场景参考图**（于是适配器走 else 分支，首帧以 role=first_frame 发出）。
- 若成片构图跟白膜首帧一致 → 平台认首帧 ⇒ 走「写实关键帧」路线（白膜→写实关键帧→当首帧出片）。
- 若成片构图仍与白膜无关 → 平台忽略首帧 ⇒ 换模型/换方案。

提示词用编译好的三段式 video_prompt（角色锚定短语已写在里面）。
"""
import asyncio
import os
import sys

os.environ.setdefault('VIDEOFORGE_DATA_DIR', os.path.expandvars(r'%LOCALAPPDATA%\VideoForge\data'))
sys.path.insert(0, r'D:\VideoForge-dev\backend')

PID = 'bceee486-2dc3-4134-b3ac-5ca7e048c3d5'
SHOT = '36f95d2c-6169-4657-9b3f-c63fc1efc9f1'
OUT = r'D:\VideoForge-dsh\exp'
os.makedirs(OUT, exist_ok=True)


async def main():
    from core.db import Database
    from core import shot_prompts as sp
    from core.adapters import VideoGenRequest, get_adapter

    db = Database(os.path.join(os.environ['VIDEOFORGE_DATA_DIR'], 'videoforge.db'))
    if hasattr(db, 'init'):
        db.init()
    sp.compile_project_prompts(db, PID)
    shot = db.get_shot(SHOT)
    prompts = sp.stored_prompts(shot)
    video_prompt = prompts.get('video_prompt') or shot.get('layer1_overview') or ''
    previz = shot.get('previz_path') or ''
    settings = db.get_all_settings()
    keys = settings.get('api_keys') or {}
    provider = shot.get('model_provider') or 'seedance_t6'
    api_key = keys.get(provider) or ''
    print('provider', provider, '| model', shot.get('model_name'), '| res', shot.get('resolution'), flush=True)
    print('previz first frame:', previz, flush=True)
    print('prompt len:', len(video_prompt), flush=True)

    adapter = get_adapter(provider, api_key=api_key, config={'model_name': shot.get('model_name')})
    # ★ 关键变量：character_reference=[] ⇒ 适配器走 else 分支，首帧以 role=first_frame 发出
    req = VideoGenRequest(
        prompt=video_prompt,
        duration=12,
        aspect_ratio=shot.get('aspect_ratio') or '16:9',
        resolution='480p',
        character_reference=[],
        first_frame=previz,
    )
    res = await adapter.generate(req)
    print('success:', res.success, '| error:', str(res.error or '')[:300], flush=True)
    if res.success and res.video_url:
        import urllib.request
        target = os.path.join(OUT, 'firstframe-test.mp4')
        urllib.request.urlretrieve(res.video_url, target)
        print('saved:', target, os.path.getsize(target), 'bytes', flush=True)
    await adapter.close()


asyncio.run(main())
