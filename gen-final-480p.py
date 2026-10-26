"""直接调用后端的 generate_final handler 出一段 480P 成片（测试用）。

为什么要绕这一步：`/api/v2/run` 的 stage=final 走的是「提案 → 确认」路径，
而 confirm 参数目前没被 v2 转发到工具里（这是产品侧待修的缺口）。
为了先把片子跑出来，这里用同一份后端代码、同一个 DB，直接带 confirm=True 调用。
"""
import asyncio
import os
import sys

os.environ.setdefault('VIDEOFORGE_DATA_DIR', os.path.expandvars(r'%LOCALAPPDATA%\VideoForge\data'))
sys.path.insert(0, r'D:\VideoForge-dev\backend')

SHOT_ID = '36f95d2c-6169-4657-9b3f-c63fc1efc9f1'


async def main():
    from core.agent_tools_production import t_generate_final  # type: ignore
    result = await t_generate_final({'shot_id': SHOT_ID, 'confirm': True})
    print('RESULT >>>', str(result)[:2000])


asyncio.run(main())
