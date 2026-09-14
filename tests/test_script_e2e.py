# -*- coding: utf-8 -*-
"""VideoForge 端到端链路测试：剧本生成（真实 LLM）"""
import json, sys, time
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://127.0.0.1:8899'
PID = 'caa9904f-2425-41b1-b446-e033207e7e8d'

def call(method, path, body=None, timeout=180):
    url = BASE + path
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8')
            return r.status, json.loads(raw) if raw else {}, time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8')[:800], time.time() - t0
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}', time.time() - t0

print('=== 1. 用户设置（当前 LLM provider）===')
st, d, el = call('GET', '/api/settings')
if isinstance(d, dict):
    data = d.get('data', {})
    print(f"  provider={data.get('llm_provider')} model={data.get('llm_model')} "
          f"key={'有' if data.get('llm_api_keys') else '无'} ({el:.1f}s)")

print('=== 2. 剧本生成（真实 LLM 调用）===')
body = {
    "user_prompt": "一个赛博朋克侦探在霓虹雨夜追查 AI 凶案，60 秒短片，主角叫林泽",
    "total_duration_seconds": 60,
    "style": "cinematic",
    "auto_split_scenes": True,
}
st, d, el = call('POST', f'/api/projects/{PID}/script/generate', body, timeout=300)
print(f"  HTTP={st} 耗时={el:.1f}s")
if isinstance(d, dict):
    print(f"  code={d.get('code')} message={str(d.get('message'))[:200]}")
    data = d.get('data')
    if isinstance(data, dict):
        print(f"  data keys={list(data.keys())}")
        sc = data.get('scenes') or []
        print(f"  场景数={len(sc)}")
        if data.get('outline'):
            print(f"  大纲前 300 字: {str(data['outline'])[:300]}")
        for s in sc[:3]:
            print(f"    - 场次{s.get('scene_number')}: {s.get('title')} | {s.get('location')} | {s.get('duration_seconds')}s")
    else:
        print(f"  data={str(data)[:300]}")
else:
    print(f"  {str(d)[:600]}")

print('=== 3. 回读剧本 ===')
st, d, el = call('GET', f'/api/projects/{PID}/script')
if isinstance(d, dict):
    data = d.get('data')
    if isinstance(data, dict):
        print(f"  ✅ 剧本已保存：keys={list(data.keys())} 场景数={len(data.get('scenes') or [])}")
        print(f"  大纲长度={len(str(data.get('outline') or ''))} 字")
    else:
        print(f"  ⚠ data={data}")
else:
    print(f"  {str(d)[:400]}")
