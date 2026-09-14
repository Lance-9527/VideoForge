# -*- coding: utf-8 -*-
"""VideoForge 全链路验证：剧本 → 分镜 → 任务 → 后期接口"""
import json, sys, time, urllib.request, urllib.error
sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://127.0.0.1:8899'
PID = 'caa9904f-2425-41b1-b446-e033207e7e8d'

def call(method, path, body=None, timeout=120):
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
        return e.code, e.read().decode('utf-8')[:300], time.time() - t0
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}', time.time() - t0

ok = lambda c: '✅' if c else '❌'
print('──── 链路 1：剧本 ────')
st, d, el = call('GET', f'/api/projects/{PID}/script')
sc = (d.get('data') or {}) if isinstance(d, dict) else {}
scenes = sc.get('scenes') or []
print(f"  {ok(st==200 and len(scenes)>0)} 剧本：{sc.get('title')} | {len(scenes)} 场 | outline {len(sc.get('outline') or '')} 字")

print('──── 链路 2：从剧本生成分镜 ────')
st, d, el = call('GET', f'/api/projects/{PID}/shots')
existing = ((d.get('data') or {}).get('shots') or []) if isinstance(d, dict) else []
print(f"  {ok(st==200)} 现有分镜数：{len(existing)}")

created = []
tlp = sc.get('three_layer_prompts') or {}
for s in scenes[:5]:
    body = {
        'project_id': PID,
        'order_index': (s.get('scene_number') or 1) - 1,
        'duration_seconds': s.get('duration_seconds') or 10,
        'layer1_overview': (tlp.get(f"scene_{s.get('scene_number')}") or {}).get('layer1_overview', ''),
        'layer2_timeline': (tlp.get(f"scene_{s.get('scene_number')}") or {}).get('layer2_timeline', []),
        'layer3_constraints': (tlp.get(f"scene_{s.get('scene_number')}") or {}).get('layer3_constraints', {}),
        'model_provider': 'kling',
        'model_name': 'kling-1.6',
        'aspect_ratio': '16:9',
        'resolution': '1080p',
    }
    st2, d2, _ = call('POST', '/api/shots', body)
    sid = (d2.get('data') or {}).get('id') if isinstance(d2, dict) else None
    if sid:
        created.append(sid)
print(f"  {ok(len(created)==len(scenes[:5]))} 新建分镜：{len(created)} 个")

st, d, el = call('GET', f'/api/projects/{PID}/shots')
now = ((d.get('data') or {}).get('shots') or []) if isinstance(d, dict) else []
print(f"  {ok(st==200 and len(now)>=len(created))} 分镜列表：{len(now)} 个（含三层提示词字段：{ok(any(s.get('layer1_overview') for s in now))}）")

print('──── 链路 3：任务系统 ────')
st, d, el = call('GET', '/api/tasks')
tasks = ((d.get('data') or {}).get('tasks') or []) if isinstance(d, dict) else []
done = [t for t in tasks if t.get('status') == 'completed']
failed = [t for t in tasks if t.get('status') == 'failed']
print(f"  {ok(st==200)} 任务总数 {len(tasks)}：完成 {len(done)}，失败 {len(failed)}")
for t in tasks[:3]:
    print(f"     - {t.get('type')} : {t.get('status')} {str(t.get('error') or '')[:70]}")

print('──── 链路 4：角色 / 场景 / 道具 ────')
for ep, key in (('/characters', 'characters'), ('/scenes', 'scenes'), ('/props', 'props')):
    st, d, el = call('GET', f'/api/projects/{PID}{ep}')
    n = len(((d.get('data') or {}).get(key) or [])) if isinstance(d, dict) else -1
    print(f"  {ok(st==200)} {ep:12} → {n} 条")

print('──── 链路 5：视频模型 provider 列表 ────')
st, d, el = call('GET', '/api/providers')
provs = ((d.get('data') or {}).get('providers') or []) if isinstance(d, dict) else []
print(f"  {ok(st==200 and len(provs)>0)} 可用视频模型 {len(provs)} 个：{', '.join(p.get('display_name') or p.get('name') for p in provs[:6])}…")

print('──── 链路 6：后期接口（无素材时应给出可读错误）────')
st, d, el = call('POST', f'/api/projects/{PID}/stitch?output_filename=t.mp4&add_subtitles=false', None, timeout=60)
print(f"  {ok(st in (200, 400, 500))} stitch 返回 HTTP {st}：{str(d)[:120]}")

print('──── 链路 7：LLM 对话流（provider 测试）────')
st, d, el = call('POST', '/api/chat/test?provider=deepseek', None, timeout=60)
avail = (d.get('data') or {}).get('available') if isinstance(d, dict) else None
print(f"  {ok(avail)} LLM 连通性：{str(d)[:140]}")
