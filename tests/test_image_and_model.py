# -*- coding: utf-8 -*-
"""验证：分镜换模型（PATCH 路由）+ 图像生成接口"""
import json, sys, urllib.request, urllib.error, time, os
sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://127.0.0.1:8899'
PID = 'e62a182d-71ef-49ca-951b-921166c4f6c3'

def call(method, path, body=None, timeout=200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8')[:400]
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}'

print('=== 0. 等待后端 ===')
for _ in range(20):
    st, d = call('GET', '/api/health')
    if st == 200:
        print('  ready:', d['data']['status'])
        break
    time.sleep(1)

print('=== 1. 分镜列表 ===')
st, d = call('GET', f'/api/projects/{PID}/shots')
shots = ((d.get('data') or {}).get('shots') or []) if isinstance(d, dict) else []
print(f'  {len(shots)} 个分镜')
if shots:
    sid = shots[0]['id']
    print(f"  第 1 个分镜当前模型: {shots[0].get('model_provider')} / {shots[0].get('model_name')}")

    print('=== 2. PATCH 换模型（原来失败的接口）===')
    st, d = call('PATCH', f'/api/shots/{sid}', {'model_provider': 'wanx', 'model_name': 'wanx2.1-t2v-turbo'})
    if isinstance(d, dict) and d.get('code') == '000000':
        s = d['data']
        print(f"  ✅ 已切换为 {s.get('model_provider')} / {s.get('model_name')}")
    else:
        print('  ❌', st, d)

print('=== 3. 图像模型列表 ===')
st, d = call('GET', '/api/image/providers')
if isinstance(d, dict):
    for p in ((d.get('data') or {}).get('providers') or []):
        print(f"  - {p['name']} ({p['label']}) 默认模型 {p['default_model']}")
    print(f"  默认: {(d.get('data') or {}).get('default_provider')}")

print('=== 4. 角色图像生成（真实调用 MiniMax 图像 API）===')
st, d = call('GET', f'/api/projects/{PID}/characters')
chars = ((d.get('data') or {}).get('characters') or []) if isinstance(d, dict) else []
if chars:
    cid, cname = chars[0]['id'], chars[0]['name']
    print(f'  目标角色: {cname}')
    st, d = call('POST', f'/api/projects/{PID}/characters/{cid}/generate-image', {'provider': 'minimax'})
    if isinstance(d, dict) and d.get('code') == '000000':
        data = d['data']
        p = data.get('path', '')
        size = os.path.getsize(p) if p and os.path.exists(p) else 0
        print(f"  ✅ 生成成功: {os.path.basename(p)} ({size//1024} KB) via {data.get('provider')}/{data.get('model')}")
        print(f"     已写回角色 reference_image_path = {bool(data.get('character', {}).get('reference_image_path'))}")
    else:
        print('  ❌', st, str(d)[:300])
else:
    print('  无角色可测')
