# -*- coding: utf-8 -*-
"""用可用的 DeepSeek key 验证完整剧本生成链路（dev 副本）

⚠️ DeepSeek API Key **不在源码中硬编码**。请通过以下任一方式提供：
  1. 环境变量 DEEPSEEK_API_KEY（推荐 — 保护本机与 CI）
  2. 在 VideoForge 启动后于「设置」页填入 key（会加密存 SQLite）

如果你看到 DS_KEY 为空字符串，请先填 key 再运行本测试。
"""
import json, os, sys, time, urllib.request
sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://127.0.0.1:8899'
PID = 'caa9904f-2425-41b1-b446-e033207e7e8d'
DS_KEY = os.environ.get('DEEPSEEK_API_KEY', '').strip()
if not DS_KEY:
    print('[warn] DEEPSEEK_API_KEY 未设置，本测试会跳或报错。设上后重跑。')
    sys.exit(0)

def call(method, path, body=None, timeout=300):
    url = BASE + path
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8')
            return r.status, (json.loads(raw) if raw else {}), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8')[:600], time.time() - t0
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}', time.time() - t0

print('=== 1. 切换 LLM 到 DeepSeek（dev 副本）===')
st, d, el = call('PUT', '/api/settings', {
    'llm_provider': 'deepseek',
    'llm_model': 'deepseek-chat',
    'llm_api_keys': {'deepseek': DS_KEY},
})
print(f'  HTTP={st} ({el:.1f}s) -> {str(d)[:200]}')

print('=== 2. 测试 LLM 连通性 ===')
st, d, el = call('POST', f'/api/chat/test?provider=deepseek&api_key={DS_KEY}', timeout=60)
print(f'  HTTP={st} ({el:.1f}s) -> {str(d)[:400]}')

print('=== 3. 剧本生成（完整链路）===')
body = {
    "user_prompt": "一个赛博朋克侦探在霓虹雨夜追查 AI 凶案，60 秒短片，主角叫林泽",
    "total_duration_seconds": 60, "style": "cinematic", "auto_split_scenes": True,
}
st, d, el = call('POST', f'/api/projects/{PID}/script/generate', body, timeout=300)
print(f'  HTTP={st} ({el:.1f}s)')
if isinstance(d, dict):
    print(f"  code={d.get('code')} msg={str(d.get('message'))[:200]}")
    data = d.get('data')
    if isinstance(data, dict):
        sc = data.get('scenes') or []
        print(f'  ✅ 场景数={len(sc)}')
        print(f"  大纲前 200 字: {str(data.get('outline'))[:200]}")
        for s in sc[:3]:
            print(f"    - 场次{s.get('scene_number')}: {s.get('title')} | {s.get('location')} | {s.get('duration_seconds')}s")
else:
    print(f'  {str(d)[:500]}')
