# -*- coding: utf-8 -*-
"""用 mock 验证海螺请求体正确（不调用真实 API，不产生费用）"""
import asyncio
import os
import sys

BK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BK)
os.environ["VIDEOFORGE_DATA_DIR"] = os.path.join(os.environ["LOCALAPPDATA"], "VideoForge", "data")

from core.adapters import get_adapter, VideoGenRequest  # noqa: E402

captured = {}


class FakeResp:
    status_code = 200

    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d

    @property
    def text(self):
        return str(self._d)


class FakeClient:
    async def post(self, url, headers=None, json=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json
        return FakeResp({"task_id": "T1", "base_resp": {"status_code": 0, "status_msg": "success"}})

    async def get(self, url, headers=None, params=None):
        captured.setdefault("gets", []).append((url, params))
        captured["get_url"] = url
        captured["get_params"] = params
        return FakeResp({"status": "Success", "file_id": "F1",
                         "base_resp": {"status_code": 0}})


async def main():
    ad = get_adapter("hailuo", api_key="sk-fake", config={"model_name": "MiniMax-Hailuo-02"})
    print("supports_first_frame =", ad.supports_first_frame, "| supports_last_frame =", ad.supports_last_frame)

    req = VideoGenRequest(
        prompt="镜头：中景，缓慢推近。农夫蹲下查看冻僵的蛇。环境：寒冬森林小径，白天，自然光",
        duration=10, aspect_ratio="16:9", resolution="1080p",
        first_frame="data:image/jpeg;base64,AAAABBBB",
    )
    print("validate_request ->", ad.validate_request(req))
    print("归一后的分辨率 ->", req.resolution)

    ad._ensure_client = lambda: _coro(FakeClient())
    ad._client = FakeClient()

    async def _ensure():
        return FakeClient()
    ad._ensure_client = _ensure
    ad.close = _noop

    # 阻止真实轮询：让 query 直接返回成功
    r = await ad.generate(req)
    print()
    print("=== 发出的请求 ===")
    print("  URL     :", captured.get("url"))
    print("  headers :", {k: (v[:14] + '...' if k == 'Authorization' else v)
                          for k, v in (captured.get("headers") or {}).items()})
    p = captured.get("payload") or {}
    print("  model   :", p.get("model"))
    print("  duration:", p.get("duration"))
    print("  resolution:", p.get("resolution"))
    print("  first_frame_image:", (str(p.get("first_frame_image"))[:40] + "...")
          if p.get("first_frame_image") else "❌ 缺失")
    print("  prompt  :", str(p.get("prompt"))[:70])
    print()
    print("  轮询序列:")
    for u, prm in (captured.get("gets") or []):
        print("     %s  %s" % (u, prm))
    print()
    gets = captured.get("gets") or []
    ok_query = any(u.endswith("/query/video_generation") and (prm or {}).get("task_id") == "T1"
                   for u, prm in gets)
    ok_retrieve = any(u.endswith("/files/retrieve") and (prm or {}).get("file_id") == "F1"
                      for u, prm in gets)
    ok = (p.get("first_frame_image") == "data:image/jpeg;base64,AAAABBBB"
          and ok_query and ok_retrieve
          and "minimaxi.com" in captured.get("url", ""))
    print("RESULT:", "PASS — 首帧进请求体 ✅ 查询端点正确 ✅ 取下载地址正确 ✅ 域名正确 ✅" if ok else "FAIL")
    return 0 if ok else 1


async def _noop(*a, **k):
    return None


async def _coro(v):
    return v


sys.exit(asyncio.run(main()))
