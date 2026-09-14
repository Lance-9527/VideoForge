# -*- coding: utf-8 -*-
"""Batch 3 验证：图像 providers / 本地图片服务 / 生成返回字段 / 换模型接口"""
import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8899"


def get(path):
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, r.read()


def jget(path):
    st, body = get(path)
    return json.loads(body.decode("utf-8"))


def post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


ok_all = True


def check(name, cond, detail=""):
    global ok_all
    mark = "PASS" if cond else "FAIL"
    if not cond:
        ok_all = False
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


# ── 1. 图像模型列表 ──
prov = jget("/api/image/providers")["data"]
names = [p["name"] for p in prov["providers"]]
check("图像模型列表非空", len(names) > 0, str(names))
check("含 minimax", "minimax" in names)
check("含 qwen(通义万相)", "qwen" in names or "wanx" in names)
check("每个 provider 有 display_name 与 default_model",
      all(p.get("display_name") and p.get("default_model") for p in prov["providers"]),
      json.dumps(prov["providers"], ensure_ascii=False))
check("每个 provider 有可选尺寸", all(p.get("sizes") for p in prov["providers"]))

# ── 2. 项目与资产 ──
projects = jget("/api/projects")["data"]["projects"]
check("有项目", len(projects) > 0, f"{len(projects)} 个")
pid = None
for p in projects:
    chars = jget(f"/api/projects/{p['id']}/characters")["data"].get("characters") or []
    if chars and any(c.get("reference_image_path") for c in chars):
        pid, pname = p["id"], (p.get("name") or p.get("title") or p["id"])
        break
check("找到已有形象的项目", pid is not None, f"{pname if pid else '—'}")

if pid:
    chars = jget(f"/api/projects/{pid}/characters")["data"]["characters"]
    scenes = jget(f"/api/projects/{pid}/scenes")["data"].get("scenes") or []
    withimg = [c for c in chars if c.get("reference_image_path")]
    check("角色含 reference_image_path", len(withimg) > 0,
          f"{len(withimg)}/{len(chars)} 个角色已有图")
    withimg_s = [s for s in scenes if s.get("reference_image_path")]
    check("场景含 reference_image_path", len(withimg_s) > 0,
          f"{len(withimg_s)}/{len(scenes)} 个场景已有图")

    # ── 3. 本地图片服务（前端 <img> 走这里） ──
    c0 = withimg[0]
    q = urllib.parse.quote(c0["reference_image_path"], safe="")
    st, body = get(f"/api/local-image?path={q}")
    check("GET /api/local-image 返回图片", st == 200 and len(body) > 1024,
          f"HTTP {st}, {len(body)} bytes")
    check("返回内容为 JPEG/PNG",
          body[:3] == b"\xff\xd8\xff" or body[:8] == b"\x89PNG\r\n\x1a\n",
          body[:4].hex())

    # 越权路径必须被拒绝
    outside = urllib.parse.quote(r"C:\Windows\System32\drivers\etc\hosts", safe="")
    try:
        st2, _ = get(f"/api/local-image?path={outside}")
        blocked = False
    except urllib.error.HTTPError as e:
        st2, blocked = e.code, e.code in (403, 404)
    check("越权路径被拒绝", blocked, f"HTTP {st2}")

    # ── 4. 换视频模型（PATCH /api/shots/{sid}） ──
    shots = jget(f"/api/projects/{pid}/shots")["data"].get("shots") or []
    if shots:
        sid = shots[0]["id"]
        st3, r3 = post("/api/shots/__x__", {})  # 占位，实际用 PATCH
        req = urllib.request.Request(
            f"{BASE}/api/shots/{sid}",
            data=json.dumps({"model_provider": "wanx", "model_name": "wanx2.1-t2v-turbo"}).encode(),
            headers={"Content-Type": "application/json"}, method="PATCH")
        with urllib.request.urlopen(req, timeout=20) as r:
            d3 = json.loads(r.read().decode())["data"]
        shot3 = d3.get("shot", d3)
        check("PATCH /api/shots 换模型成功",
              shot3.get("model_provider") == "wanx", json.dumps(shot3, ensure_ascii=False)[:160])
        # 换回
        req = urllib.request.Request(
            f"{BASE}/api/shots/{sid}",
            data=json.dumps({"model_provider": shots[0]["model_provider"],
                             "model_name": shots[0].get("model_name") or ""}).encode(),
            headers={"Content-Type": "application/json"}, method="PATCH")
        urllib.request.urlopen(req, timeout=20).read()
    else:
        print("[SKIP] 该项目无分镜，跳过换模型测试")

    # ── 5. 生成接口返回字段（filename / size_bytes） ──
    target = [c for c in chars if not c.get("reference_image_path")]
    if target:
        st5, r5 = post(
            f"/api/projects/{pid}/characters/{target[0]['id']}/generate-image",
            {"provider": "minimax", "prompt": "一位青年男性的正面肖像，简约灰色背景，证件照风格，高清"})
        d5 = r5.get("data") or {}
        if st5 == 200:
            check("生成图片返回 filename", bool(d5.get("filename")), str(d5.get("filename")))
            check("生成图片返回 size_bytes", (d5.get("size_bytes") or 0) > 1024,
                  f"{d5.get('size_bytes')} bytes")
            st6, body6 = get(f"/api/local-image?path={urllib.parse.quote(d5['path'], safe='')}")
            check("刚生成的图片可被 local-image 读取", st6 == 200 and len(body6) > 1024,
                  f"HTTP {st6}, {len(body6)} bytes")
        else:
            check("生成图片（可能被内容审核/额度拦截，但错误文案须可读）",
                  bool(r5.get("message")), f"HTTP {st5}: {r5.get('message', '')[:120]}")
    else:
        print("[SKIP] 该项目所有角色都已有形象，跳过生成字段测试")

print()
print("=" * 55)
print("RESULT:", "ALL PASS" if ok_all else "HAS FAILURES")
sys.exit(0 if ok_all else 1)
