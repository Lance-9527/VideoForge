"""查 seedance 任务状态，拿新的签名 URL，把那张 480P 片子取回来（不再花一次钱）。"""
import json
import os
import urllib.request

KEY = open(r'D:\Tools\mcp-servers\agentai.key', encoding='utf-8').read().strip()
TASK = 'task-1792591326215-25542'
BASE = 'https://agentai.token6688.com/api/v3/contents/generations/tasks/'


def get(url):
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + KEY})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))


data = get(BASE + TASK)
print('status:', data.get('status'))
content = data.get('content') or {}
video_url = content.get('video_url') if isinstance(content, dict) else None
if video_url is None:
    print('raw:', json.dumps(data, ensure_ascii=False)[:1200])
else:
    target = r'D:\VideoForge-dsh\film\rainday-480p.mp4'
    os.makedirs(os.path.dirname(target), exist_ok=True)
    urllib.request.urlretrieve(video_url, target)
    print('saved', target, os.path.getsize(target), 'bytes')
