"""从后端日志里把那次 seedance 任务的签名下载 URL 抠出来，直接重下这段 480P 片子。

背景：generate_final 已经把片子生成出来了（网关任务完成、日志里记录了下 signed URL），
但那个一次性 python 进程在落盘后异常退出，seg_01.mp4 只有一半（moov 缺失）。
签名 URL 有效期 24h，所以直接从日志里取回并重新下载，避免再花一次钱。
"""
import os
import re
import urllib.request

LOG = os.path.join(os.environ['USERPROFILE'], '.videoforge', 'videoforge.log')
OUT_DIR = r'D:\VideoForge-dsh\film'
os.makedirs(OUT_DIR, exist_ok=True)

text = open(LOG, encoding='utf-8', errors='replace').read()
# 日志里 httpx 会打印完整 URL（可能被换行折断），先去掉换行再匹配
flat = text.replace('\r', '').replace('\n', '')
matches = re.findall(r"GET (https://ark-acg-cn-beijing[^ ]+?\.mp4\?[^ ]+?) \"HTTP/1\.1 200", flat)
if not matches:
    matches = re.findall(r"(https://ark-acg-cn-beijing[^\s\"]+?\.mp4\?[^\s\"]+)", flat)
print('found urls:', len(matches))
if not matches:
    raise SystemExit('日志里没有找到签名 URL')

url = matches[-1]
target = os.path.join(OUT_DIR, 'rainday-480p.mp4')
print('downloading ->', target)
urllib.request.urlretrieve(url, target)
size = os.path.getsize(target)
print('downloaded bytes:', size)
