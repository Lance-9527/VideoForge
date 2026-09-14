# -*- coding: utf-8 -*-
"""真测硅基流动的「指令式图像编辑」（Qwen-Image-Edit-2509）= 真正的后期AI再修改。

流程：造一张图 → 用自然语言要求修改 → 看输出是否真的改了、且保留了该保留的。
费用约 ¥0.2/张。
"""
import base64
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from imageio_ffmpeg import get_ffmpeg_exe       # noqa: E402

FF = get_ffmpeg_exe()
O = urllib.request.build_opener(urllib.request.ProxyHandler({}))
KEY = os.environ.get("SF_KEY", "")
assert KEY, "需要 SF_KEY"

W = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_edit_test")
os.makedirs(W, exist_ok=True)


def make_base_image(path):
    """造一张可辨识的"角色设定图"：深色背景 + 白色人形剪影 + 上方标题块。

    用 ffmpeg 画，保证我们清楚原图长什么样，才能判断"改了什么、保留了什么"。
    """
    subprocess.run([
        FF, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=0x1B2A3A:s=768x1024",
        "-vf",
        # 头 + 身体（简单剪影），再在上方加一条亮色标题块
        "drawbox=x=334:y=180:w=100:h=100:color=0xE8D8C0:t=fill,"      # 头
        "drawbox=x=294:y=300:w=180:h=300:color=0x6B7A8A:t=fill,"      # 身体
        "drawbox=x=0:y=0:w=768:h=60:color=0x2ECC71:t=fill",           # 顶部标题条
        "-frames:v", "1", path], capture_output=True)


def edit(image_path, prompt, model="Qwen/Qwen-Image-Edit-2509",
         image2=None, image_size=""):
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(image_path)[1].lstrip(".").lower() or "png"
    body = {
        "model": model,
        "prompt": prompt,
        "image": f"data:image/{ext};base64,{b64}",
    }
    if image2:
        with open(image2, "rb") as f:
            b64b = base64.b64encode(f.read()).decode()
        ext2 = os.path.splitext(image2)[1].lstrip(".").lower() or "png"
        body["image2"] = f"data:image/{ext2};base64,{b64b}"
    if image_size:
        body["image_size"] = image_size
    req = urllib.request.Request(
        "https://api.siliconflow.cn/v1/images/generations",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json",
                 "X-Enable-Watermark": "0"})
    t0 = time.time()
    try:
        with O.open(req, timeout=300) as r:
            d = json.loads(r.read().decode())
        return {"ok": True, "data": d, "elapsed": round(time.time() - t0, 1)}
    except urllib.error.HTTPError as e:
        return {"ok": False,
                "error": f"HTTP {e.code}: {e.read(500).decode('utf-8','replace')}",
                "elapsed": round(time.time() - t0, 1)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "elapsed": round(time.time() - t0, 1)}


def download(url, path):
    with O.open(url, timeout=120) as r:
        data = r.read()
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


base = os.path.join(W, "base.png")
make_base_image(base)
print(f"原图: {base}  {os.path.getsize(base)} 字节  (深蓝底 + 灰蓝人形 + 绿色顶条)")

print("\n" + "═" * 76)
print("① 真图生图：要求「把衣服改成灰蓝色粗布军装，加绑腿；脸和背景保持」")
r1 = edit(base, "把人物的衣服换成洗得发白的灰蓝色粗布军装，裤脚加布制绑腿；"
                "保持人物轮廓、脸部位置和深色背景不变")
if r1["ok"]:
    imgs = (r1["data"].get("images") or [])
    print(f"   ✅ HTTP 200 · {r1['elapsed']}s · 返回 {len(imgs)} 张")
    if imgs:
        u = imgs[0].get("url") or ""
        out = os.path.join(W, "edited1.png")
        n = download(u, out)
        print(f"   已下载: {out}  {n} 字节")
        print(f"   seed={r1['data'].get('seed')}")
    print("   原始响应:", json.dumps(r1["data"], ensure_ascii=False)[:300])
else:
    print("   ❌", r1["error"])

print("\n" + "═" * 76)
print("② 多图参考（image2，仅 2509 支持）：把第二张图的配色套过去")
base2 = os.path.join(W, "ref2.png")
subprocess.run([
    FF, "-y", "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "color=c=0x8B2E2E:s=768x1024",
    "-vf", "drawbox=x=0:y=0:w=768:h=1024:color=0x8B2E2E:t=fill",
    "-frames:v", "1", base2], capture_output=True)
r2 = edit(base, "把人物的衣服改成参考图里的那种暗红色调", image2=base2)
if r2["ok"]:
    imgs = r2["data"].get("images") or []
    print(f"   ✅ HTTP 200 · {r2['elapsed']}s · {len(imgs)} 张")
    if imgs:
        out = os.path.join(W, "edited2.png")
        n = download(imgs[0].get("url") or "", out)
        print(f"   已下载: {out}  {n} 字节")
else:
    print("   ❌", r2["error"])

print("\n" + "═" * 76)
print("③ 契约确认：Qwen-Image-Edit 不支持 image_size（传了应该报错）")
r3 = edit(base, "测试", image_size="1024x1024")
print(f"   传 image_size → {'❌ 竟然接受了' if r3['ok'] else '✅ 按文档拒绝：' + r3['error'][:150]}")

print("\n" + "═" * 76)
print("④ 多图版（Qwen-Image-Edit，非 2509）不支持 image2")
r4 = edit(base, "把衣服改成暗红", model="Qwen/Qwen-Image-Edit", image2=base2)
print(f"   → {'✅ 接受了（文档说只有 2509 支持，实际也接受）' if r4['ok'] else '✅ 拒绝：' + r4['error'][:160]}")

print("\n产物目录:", W)
