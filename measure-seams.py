"""用库里持久化的尾帧，算 4 镜的接缝连续性（不再花钱，只做度量）。"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'D:\VideoForge-dev\backend')
os.environ.setdefault('VIDEOFORGE_DATA_DIR', os.path.expandvars(r'%LOCALAPPDATA%\VideoForge\data'))

from core.db import Database  # noqa: E402
from core import shot_prompts as sp  # noqa: E402

FFMPEG = r'C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Links\ffmpeg.EXE'
PID = '3ad3f169-fd41-4ed1-a034-a013eb066407'
db = Database(os.path.join(os.environ['VIDEOFORGE_DATA_DIR'], 'videoforge.db'))
shots = sorted(db.list_shots(PID), key=lambda s: s.get('order_index') or 0)
scale = 'scale=864:496:force_original_aspect_ratio=increase,crop=864:496'
out_dir = r'D:\VideoForge-dsh\exp'
os.makedirs(out_dir, exist_ok=True)

for a, b in zip(shots, shots[1:]):
    pa = sp.stored_prompts(a)
    pb = sp.stored_prompts(b)
    last = str(pa.get('last_frame') or '')
    # 下一镜的视频（分段文件）
    seg_dir = os.path.join(str(db.get_all_settings().get('output_dir') or ''), PID, str(b.get('id')))
    seg = os.path.join(seg_dir, 'seg_01.mp4')
    if not last or not os.path.isfile(seg):
        print(f"镜{(a.get('order_index') or 0) + 1}→{(b.get('order_index') or 0) + 1}: 缺尾帧或缺片")
        continue
    first = os.path.join(out_dir, f"seam-{a.get('order_index')}-{b.get('order_index')}.png")
    subprocess.run([FFMPEG, '-y', '-v', 'error', '-ss', '0.2', '-i', seg, '-frames:v', '1', first], check=False)
    if not os.path.exists(first):
        continue
    r = subprocess.run(
        [FFMPEG, '-v', 'info', '-i', last, '-i', first,
         '-filter_complex', f'[0:v]{scale}[a];[1:v]{scale}[b];[a][b]ssim', '-f', 'null', '-'],
        capture_output=True, text=True)
    ssim = ''
    for line in r.stderr.splitlines():
        if 'SSIM' in line:
            ssim = line.strip().split('All:')[-1].strip()
    print(f"镜{(a.get('order_index') or 0) + 1}→{(b.get('order_index') or 0) + 1}"
          f"（{pb.get('chain_mode')}）SSIM={ssim or 'n/a'}")
