# -*- coding: utf-8 -*-
"""验证资产库接口 + 从剧本提取功能"""
import json, sys, urllib.request, urllib.error, time
sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://127.0.0.1:8899'
PID = 'e62a182d-71ef-49ca-951b-921166c4f6c3'   # 抗日英豪项目

def call(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8')[:300]
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}'

print('=== 1. 等待后端就绪 ===')
for i in range(20):
    st, d = call('GET', '/api/health')
    if st == 200:
        print('  后端就绪:', json.dumps(d.get('data', {}), ensure_ascii=False)[:120])
        break
    time.sleep(1)

print('=== 2. 资产库接口（跨项目）===')
st, d = call('GET', '/api/library/characters')
print('  角色库:', st, json.dumps(d, ensure_ascii=False)[:150] if isinstance(d, dict) else d)
st2, d2 = call('GET', '/api/library/scenes')
print('  场景库:', st2, json.dumps(d2, ensure_ascii=False)[:150] if isinstance(d2, dict) else d2)

print('=== 3. 从剧本提取角色（联动）===')
st, d = call('POST', f'/api/projects/{PID}/characters/from-script')
if isinstance(d, dict):
    data = d.get('data') or {}
    print(f"  创建 {data.get('count')} 个角色（剧本中共 {data.get('total_in_script')} 个人物名，跳过 {data.get('skipped')}）")
    for c in (data.get('created') or [])[:5]:
        print(f"    - {c.get('name')} ({c.get('description')})")
else:
    print('  ', d)

print('=== 4. 从剧本提取场景（联动）===')
st, d = call('POST', f'/api/projects/{PID}/scenes/from-script')
if isinstance(d, dict):
    data = d.get('data') or {}
    print(f"  创建 {data.get('count')} 个场景（剧本中共 {data.get('total_in_script')} 个地点）")
    for s in (data.get('created') or [])[:5]:
        print(f"    - {s.get('name')} ({s.get('location_type')})")
else:
    print('  ', d)

print('=== 5. 当前项目的角色/场景 ===')
st, d = call('GET', f'/api/projects/{PID}/characters')
n1 = len((d.get('data') or {}).get('characters') or []) if isinstance(d, dict) else -1
st, d = call('GET', f'/api/projects/{PID}/scenes')
n2 = len((d.get('data') or {}).get('scenes') or []) if isinstance(d, dict) else -1
print(f'  角色 {n1} 个 / 场景 {n2} 个')

print('=== 6. 资产库导入测试（把本项目的角色导入到另一个项目）===')
OTHER = 'caa9904f-2425-41b1-b446-e033207e7e8d'
st, d = call('GET', f'/api/projects/{PID}/characters')
chars = ((d.get('data') or {}).get('characters') or []) if isinstance(d, dict) else []
ids = [c['id'] for c in chars[:2]]
if ids:
    st, d = call('POST', f'/api/projects/{OTHER}/characters/import', {'ids': ids})
    if isinstance(d, dict):
        print(f"  导入结果: {d.get('data', {}).get('count')} 个角色 → 项目 {OTHER[:8]}")
        for c in (d.get('data', {}).get('imported') or []):
            print(f"    - {c.get('name')} (新 id {str(c.get('id'))[:8]})")
    else:
        print('  ', d)
else:
    print('  跳过（无角色可导入）')
