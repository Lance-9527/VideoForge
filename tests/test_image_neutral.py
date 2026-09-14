# -*- coding: utf-8 -*-
"""用中性提示词测试图像生成完整链路（排除内容审核干扰）"""
import json, os, sys, urllib.request, urllib.error, time
sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://127.0.0.1:8899'
PID = 'e62a182d-71ef-49ca-951b-921166c4f6c3'

def call(method, path, body=None, timeout=240):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8')[:500]
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}'

# 等待后端（可能有重启）
for _ in range(15):
    st, d = call('GET', '/api/health')
    if st == 200:
        break
    time.sleep(1)
print('后端:', st)

st, d = call('GET', f'/api/projects/{PID}/characters')
chars = ((d.get('data') or {}).get('characters') or []) if isinstance(d, dict) else []
print(f'角色数: {len(chars)}')

target = next((c for c in chars if c['name'] == '小虎'), chars[1] if len(chars) > 1 else None)
if target:
    print(f"\n=== 生成角色形象：{target['name']}（中性提示词）===")
    st, d = call('POST', f'/api/projects/{PID}/characters/{target["id"]}/generate-image', {
        'provider': 'minimax',
        'prompt': '一位年轻男性士兵的角色设定图，正面半身像，身着简朴军装，神情坚毅，简洁灰白背景，电影级光影，高清写实',
    })
    if isinstance(d, dict) and d.get('code') == '000000':
        data = d['data']
        p = data.get('path', '')
        size = os.path.getsize(p) if p and os.path.exists(p) else 0
        print(f'  ✅ 生成成功: {os.path.basename(p)} ({size//1024} KB)')
        print(f'     provider={data.get("provider")} model={data.get("model")}')
        print(f'     已写回角色: {bool((data.get("character") or {}).get("reference_image_path"))}')
    else:
        print('  ❌', st, d)

# 顺便测场景
st, d = call('GET', f'/api/projects/{PID}/scenes')
scenes = ((d.get('data') or {}).get('scenes') or []) if isinstance(d, dict) else []
if scenes:
    s = scenes[0]
    print(f"\n=== 生成场景概念图：{s['name']} ===")
    st, d = call('POST', f'/api/projects/{PID}/scenes/{s["id"]}/generate-image', {
        'provider': 'minimax',
        'prompt': '一处山间小道的场景概念图，两侧是茂密树林，清晨薄雾，无人物，广角电影感，高清写实',
    })
    if isinstance(d, dict) and d.get('code') == '000000':
        p = d['data'].get('path', '')
        print(f'  ✅ 生成成功: {os.path.basename(p)} ({os.path.getsize(p)//1024 if p and os.path.exists(p) else 0} KB)')
    else:
        print('  ❌', st, str(d)[:300])
