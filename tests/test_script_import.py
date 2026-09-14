# -*- coding: utf-8 -*-
"""剧本导入验收：真实 PDF 剧本 → 解析 → LLM 结构化 → 落库"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("VF_BASE", "http://127.0.0.1:8899")
PDF = os.environ.get(
    "VF_PDF",
    r"E:\其他\xwechat_files\wxid_59zkrzsbtr3721_310c\msg\file\2026-09"
    r"\40集农村微短剧剧本：寒潮抢收玉米（全集分集台词+镜头）.pdf")
ok = True

# ⚠ 必须绕过系统代理：本机若配了代理，urllib 会把大 body 的 POST 交给代理，
#   代理对 localhost 返回 503，表现为"接口突然不可用"，而服务端日志里什么都没有。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_OPENER)


def check(name, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, (" — " + str(detail)) if detail else ""))


def jcall(method, path, payload=None, timeout=900):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"message": "?"}


def multipart(path, filepath, fields=None, timeout=900):
    boundary = "----vf" + uuid.uuid4().hex
    fn = os.path.basename(filepath)
    body = b""
    for k, v in (fields or {}).items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, k, v)).encode("utf-8")
    body += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n" % (boundary, fn)).encode("utf-8")
    body += open(filepath, "rb").read() + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(
        BASE + path, data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"message": "?"}


print("=" * 62)
print("剧本导入验收")
print("=" * 62)
check("测试 PDF 存在", os.path.exists(PDF), PDF.split("\\")[-1])

# 1) 建一个干净项目
st, r = jcall("POST", "/api/projects", {"title": "【导入验收】寒潮抢收玉米", "description": "导入真实剧本测试"})
pid = (r.get("data") or {}).get("id") or (r.get("data") or {}).get("project", {}).get("id")
check("创建项目", bool(pid), pid)

# 2) 纯文本解析能力（模块级）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
try:
    from core.docimport import parse_document
    t, note = parse_document(PDF)
    check("PDF 解析出文字", len(t) > 500, "%d 字%s" % (len(t), (" | " + note) if note else ""))
except Exception as e:
    check("PDF 解析出文字", False, str(e))

# 3) 走 HTTP 导入（结构化）
t0 = time.time()
st, r = multipart("/api/projects/%s/script/import" % pid, PDF, {"structurize": "true"})
d = r.get("data") or {}
check("HTTP 导入成功", st == 200 and d.get("imported"), "HTTP %s %s" % (st, r.get("message", "")))
if d.get("imported"):
    check("解析字数 > 1000", d.get("chars", 0) > 1000, d.get("chars"))
    check("产出场次 > 0", d.get("scenes", 0) > 0, "%s 场 · %.0fs" % (d.get("scenes"), time.time() - t0))
    print("      llm_structurized =", d.get("llm_structurized"))
    if d.get("warning"):
        print("      warning:", str(d["warning"])[:200])
    if d.get("preview"):
        print("      预览:", str(d["preview"])[:120].replace("\n", " "))

# 4) 剧本能读回
st, r = jcall("GET", "/api/projects/%s/script" % pid)
_d = (r.get("data") or {})
sc = _d.get("script") or _d          # 兼容两种层级
check("剧本已落库", bool(sc and sc.get("id")), list(sc.keys())[:5] if sc else "")
check("剧本含场次", len(sc.get("scenes") or []) > 0, len(sc.get("scenes") or []))

# 5) 从导入的剧本提取角色/场景（验证整条链能接上）
st, r = jcall("POST", "/api/projects/%s/characters/from-script?force=true" % pid, None, timeout=600)
ch = (r.get("data") or {})
check("从导入剧本提取角色", (ch.get("count", 0) + ch.get("updated_count", 0)) > 0,
      "新建 %s / 更新 %s" % (ch.get("count"), ch.get("updated_count")))
st, r = jcall("POST", "/api/projects/%s/scenes/from-script?force=true" % pid, None, timeout=600)
sn = (r.get("data") or {})
check("从导入剧本提取场景", (sn.get("count", 0) + sn.get("updated_count", 0)) > 0,
      "新建 %s / 更新 %s" % (sn.get("count"), sn.get("updated_count")))

# 展示几个角色，检查是否"按剧本设定"
st, r = jcall("GET", "/api/projects/%s/characters" % pid)
chars = (r.get("data") or {}).get("characters") or []
print("\n  提取到的角色（前 4 个）：")
for c in chars[:4]:
    print("   【%s】%s · %s" % (c.get("name"), c.get("age") or "", (c.get("description") or "")[:70]))
withdesc = sum(1 for c in chars if c.get("description") and len(c["description"]) > 8)
check("角色带具体外形设定（不是空壳）", withdesc >= max(1, len(chars) // 2),
      "%d/%d 个有外形描述" % (withdesc, len(chars)))

# 6) 分镜生成 + 与角色/场景建立引用
st, r = jcall("POST", "/api/projects/%s/shots/from-script" % pid, None, timeout=300)
lk = (r.get("data") or {}).get("linkage") or {}
check("生成分镜并建立引用", (lk.get("total", 0) > 0),
      "场景 %s/%s · 角色 %s/%s" % (lk.get("scene_linked"), lk.get("total"),
                                    lk.get("char_linked"), lk.get("total")))

print()
print("=" * 62)
print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
