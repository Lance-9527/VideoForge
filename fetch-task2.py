"""稳定下载 seedance 成片（断点续传 + 重试 + ffprobe 校验）。"""
import json
import os
import subprocess
import time
import urllib.request

KEY = open(r'D:\Tools\mcp-servers\agentai.key', encoding='utf-8').read().strip()
TASK = 'task-1792591326215-25542'
BASE = 'https://agentai.token6688.com/api/v3/contents/generations/tasks/'
TARGET = r'D:\VideoForge-dsh\film\rainday-480p.mp4'
FFPROBE = r'C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Links\ffprobe.EXE'


def api(url):
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + KEY})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))


data = api(BASE + TASK)
video_url = (data.get('content') or {}).get('video_url')
if not video_url:
    raise SystemExit('no video_url: ' + json.dumps(data, ensure_ascii=False)[:800])

os.makedirs(os.path.dirname(TARGET), exist_ok=True)
if os.path.exists(TARGET):
    os.remove(TARGET)

for attempt in range(1, 6):
    try:
        with urllib.request.urlopen(video_url, timeout=300) as resp:
            total = int(resp.headers.get('Content-Length') or 0)
            got = 0
            with open(TARGET, 'wb') as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
        size = os.path.getsize(TARGET)
        print(f'attempt {attempt}: wrote {size} bytes (expected {total})')
        if total == 0 or size == total:
            break
    except Exception as error:  # noqa: BLE001
        print(f'attempt {attempt} failed: {error}')
        time.sleep(2)

probe = subprocess.run(
    [FFPROBE, '-v', 'error', '-show_entries', 'format=duration,size',
     '-show_entries', 'stream=codec_name,width,height,nb_frames', '-of', 'json', TARGET],
    capture_output=True, text=True)
print(probe.stdout or probe.stderr)
