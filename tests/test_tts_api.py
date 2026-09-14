# -*- coding: utf-8 -*-
"""通过**运行中的后端 API**验证：填了 TTS Key 之后音色真的可用、真能合成。

用法：先设环境变量 SF_KEY，再跑。
    $env:SF_KEY='sk-...'; python tests\test_tts_api.py
（Key 不写进文件，避免密钥散进代码库）
"""
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8766"
O = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SF_KEY = os.environ.get("SF_KEY", "")
if not SF_KEY:
    print("请先设环境变量 SF_KEY（硅基流动 API Key）")
    raise SystemExit(2)
ok = fail = 0


def req(method, path, body=None, timeout=180, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with O.open(r, timeout=timeout) as resp:
            b = resp.read()
            return (resp.status, b) if raw else json.loads(b.decode("utf-8"))
    except urllib.error.HTTPError as e:
        b = e.read()
        return (e.code, b) if raw else {"_http": e.code,
                                        "_body": b.decode("utf-8", "replace")[:400]}
    except Exception as e:
        return (None, str(e).encode()) if raw else {"_err": f"{type(e).__name__}: {e}"}


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  ❌ {name} — {detail}")


print("═" * 74)
print("① 保存前：音色清单里的可用性标注")
d = req("GET", "/api/voice/voices")
vs = (d.get("data") or {}).get("voices") or []
check("音色接口 200", d.get("code") == "000000", f"{len(vs)} 个音色")
byprov = {}
for v in vs:
    byprov.setdefault(v["provider"], []).append(v)
for p, lst in byprov.items():
    usable = sum(1 for v in lst if v.get("usable"))
    print(f"     {p:12s} {len(lst):2d} 个 · 可用 {usable} · "
          f"provider={lst[0].get('provider_display')}")
check("每条都带 provider/usable 标注",
      all("provider" in v and "usable" in v for v in vs))
sf_before = [v for v in vs if v["provider"] == "siliconflow"]
check("填 Key 前 siliconflow 标为不可用",
      all(v["usable"] is False for v in sf_before) and bool(sf_before),
      f"{len(sf_before)} 个均不可用")
check("不可用时给了可读原因",
      all(v.get("unusable_reason") for v in sf_before))
check("edge 永远可用（免费）",
      all(v["usable"] for v in byprov.get("edge", [])))

print("\n" + "═" * 74)
print("② 设置接口能不能存下 tts_api_keys（这是之前的硬伤）")
cur = req("GET", "/api/settings")
check("读设置成功", cur.get("code") == "000000")
save = req("PUT", "/api/settings", {"tts_api_keys": {"siliconflow": SF_KEY}})
check("PUT 保存成功", save.get("code") == "000000",
      json.dumps(save, ensure_ascii=False)[:120])
back = req("GET", "/api/settings")
tk = (back.get("data") or {}).get("tts_api_keys") or {}
check("tts_api_keys 真的存进去了", tk.get("siliconflow") == SF_KEY,
      f"存回的值长度={len(tk.get('siliconflow') or '')}")

print("\n" + "═" * 74)
print("③ 保存后：siliconflow 音色应变为可用")
d = req("GET", "/api/voice/voices")
vs = (d.get("data") or {}).get("voices") or []
sf = [v for v in vs if v["provider"] == "siliconflow"]
check("siliconflow 音色全部可用", all(v["usable"] for v in sf), f"{len(sf)} 个")
check("音色总数变多（含新 Provider）", len(vs) > 30, f"{len(vs)} 个")
for gp in ("minimax", "dashscope"):
    g = [v for v in vs if v["provider"] == gp]
    print(f"     新增 {gp}: {len(g)} 个音色"
          + (f"（例：{g[0]['display_name']}）" if g else ""))

print("\n" + "═" * 74)
print("④ 真合成：/api/voice/test（这就是设置页那个测试按钮调的接口）")
for prov in ("siliconflow", "edge", "minimax", "dashscope"):
    r = req("POST", "/api/voice/test", {"provider": prov}, timeout=180)
    dd = r.get("data") or {}
    if dd.get("ok"):
        print(f"     ✅ {prov:12s} {dd.get('elapsed')}s · {dd.get('size_bytes')} 字节 · "
              f"{dd.get('voice_id')}")
    else:
        print(f"     ❌ {prov:12s} {str(dd.get('error'))[:110]}")
        if dd.get("hint"):
            print(f"        提示：{dd['hint'][:90]}")
    globals()['ok'] = globals()['ok'] + (1 if dd.get("ok") else 0)
    globals()['fail'] = globals()['fail'] + (0 if dd.get("ok") else 1) if prov in ("siliconflow",) else globals()['fail']

print("\n" + "═" * 74)
print("⑤ 试听接口（GET 版，前端 <audio src> 直链）真的返回音频")
for v in (sf[:2] or []):
    st, b = req("GET", f"/api/voice/preview?voice_id={urllib.parse.quote(v['voice_id'])}"
                       f"&text={urllib.parse.quote('这是音色试听。')}", raw=True)
    head = b[:4].hex() if isinstance(b, bytes) else str(b)[:40]
    is_mp3 = isinstance(b, bytes) and (b[:3] == b"ID3" or b[:2] in (b"\xff\xfb", b"\xff\xf3"))
    check(f"试听 {v['voice_id'].rsplit(':', 1)[-1]}",
          st == 200 and is_mp3, f"HTTP {st} · {len(b) if isinstance(b, bytes) else 0} 字节 · {head}")

print("\n" + "═" * 74)
print("⑥ 图像模型：Key 状态标注 + 试生成接口")
d = req("GET", "/api/image/providers")
ps = (d.get("data") or {}).get("providers") or []
check("图像 providers 带 has_key/usable",
      all("has_key" in p and "usable" in p for p in ps), f"{len(ps)} 个")
for p in ps:
    print(f"     {p['name']:10s} {p['display_name']:14s} "
          f"可用={p['usable']!s:5s} 尺寸={p.get('default_size')} "
          f"| {str(p.get('best_for'))[:34]}")
usable = [p for p in ps if p["usable"]]
if usable:
    r = req("POST", "/api/image/test", {"provider": usable[0]["name"]}, timeout=180)
    dd = r.get("data") or {}
    print(f"     试生成 {usable[0]['name']}: ok={dd.get('ok')} "
          f"{('· ' + str(dd.get('elapsed')) + 's · ' + str(dd.get('size', ''))) if dd.get('ok') else ('· ' + str(dd.get('error'))[:100])}")
else:
    print("     没有可用的图像模型（都没配 Key）")

print("\n" + "═" * 74)
print(f"结果: {ok} 通过 / {fail} 失败")
