# -*- coding: utf-8 -*-
"""验证剧本接口修复效果（专业大纲 + 数组场景）"""
import json, sys, urllib.request
sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://127.0.0.1:8899'
PID = 'caa9904f-2425-41b1-b446-e033207e7e8d'

with urllib.request.urlopen(f'{BASE}/api/projects/{PID}/script', timeout=20) as r:
    d = json.loads(r.read().decode('utf-8'))

data = d.get('data') or {}
print('code:', d.get('code'))
print('title:', data.get('title'))
print('logline:', str(data.get('logline'))[:150])
print('style:', data.get('style'))
sc = data.get('scenes')
print('scenes 类型:', type(sc).__name__, '| 数量:', len(sc) if isinstance(sc, list) else 'N/A')
tlp = data.get('three_layer_prompts')
print('three_layer_prompts 类型:', type(tlp).__name__, '| keys:', list(tlp.keys())[:5] if isinstance(tlp, dict) else 'N/A')
print()
print('=== outline（专业大纲文本，前端剧本工作台将显示这个）===')
print((data.get('outline') or '(空)')[:900])
print()
if isinstance(sc, list) and sc:
    print('=== 场景数组第 1 项（前端 .map 渲染用）===')
    print(json.dumps(sc[0], ensure_ascii=False, indent=2)[:600])
