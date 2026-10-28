#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
vf-check — AI 商品素材合规检查器（VideoForge 出品）

一句话：告诉电商运营「这批商品图能不能过审，缺什么，怎么改」。

用法
────────────────────────────────────────────────────────────
  vf-check <文件或目录> --platform pdd
  vf-check ./商品图 --platform all --report 报告.md
  vf-check ./商品图 --platform taobao --fix        # 自动补 AI 标识
  vf-check ./商品图 --platform pdd --json          # 机器可读
  vf-check --init-rules .                          # 导出成可发给客户的独立版

平台别名
────────────────────────────────────────────────────────────
  pdd / 拼多多        taobao / tb / 淘宝    jd / 京东
  douyin / dy / 抖音  amazon / amz         shopify
  xiaohongshu / xhs / 小红书                all（全部平台都查）

退出码
────────────────────────────────────────────────────────────
  0 = 全部通过   1 = 存在 error 级问题（不可发布）   2 = 参数/环境错误

依据
────────────────────────────────────────────────────────────
  《人工智能生成合成内容标识办法》国信办通字〔2025〕2 号，2025-09-01 施行
  第四条（显式标识）第五条（文件元数据隐式标识）第九条/第十条（不得隐匿标识）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Windows 控制台 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

__version__ = "0.1.0"

PLATFORM_ALIASES = {
    "pdd": "pdd", "pinduoduo": "pdd", "拼多多": "pdd",
    "taobao": "taobao", "tb": "taobao", "淘宝": "taobao", "天猫": "taobao",
    "jd": "jd", "jingdong": "jd", "京东": "jd",
    "douyin": "douyin", "dy": "douyin", "抖音": "douyin", "tiktok": "douyin",
    "amazon": "amazon", "amz": "amazon",
    "shopify": "shopify",
    "xiaohongshu": "xiaohongshu", "xhs": "xiaohongshu", "小红书": "xiaohongshu",
    "all": "all", "全部": "all", "cn": "all",
}

PLATFORM_LABEL = {
    "pdd": "拼多多", "taobao": "淘宝/天猫", "jd": "京东", "douyin": "抖音",
    "amazon": "Amazon", "shopify": "Shopify", "xiaohongshu": "小红书",
    "all": "全平台",
}

SEV_ORDER = {"error": 0, "warn": 1, "manual": 2}
SEV_ICON = {"error": "✗", "warn": "!", "manual": "?"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}


# ────────────────────────── 定位后端引擎 ──────────────────────────

def _candidate_backends() -> List[Path]:
    cands: List[Path] = []
    env = os.environ.get("VF_BACKEND", "").strip()
    if env:
        cands.append(Path(env))
    here = Path(__file__).resolve().parent
    # ★ 独立分发版的目录结构：<DIR>/vf_check.py + <DIR>/core/commerce_rules.py
    cands.append(here / "core")
    cands.append(here / "backend" / "core")
    cands.append(here / "backend")
    cands.append(here.parent / "backend")
    cands.append(here / "..")
    for up in list(here.parents)[:6]:
        cands.append(up / "backend")
    # 已知安装位置
    cands.append(Path(r"D:\VideoForge-dev\backend"))
    cands.append(Path(r"D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\resources"
                      r"\videoforge-sidecar\_internal\backend"))
    out, seen = [], set()
    for c in cands:
        try:
            r = c.resolve()
        except Exception:
            continue
        if r in seen:
            continue
        seen.add(r)
        # engine 文件可能在 <dir>/commerce_rules.py，也可能在 <dir>/core/commerce_rules.py
        if (r / "commerce_rules.py").is_file() or (r / "core" / "commerce_rules.py").is_file():
            out.append(r)
    return out


def load_engine():
    """加载 commerce_rules 引擎。返回 (module, rules_json_path)。"""
    for backend in _candidate_backends():
        core = backend / "core" if (backend / "core" / "commerce_rules.py").is_file() else backend
        if str(core) not in sys.path:
            sys.path.insert(0, str(core))
        try:
            import commerce_rules  # type: ignore
            return commerce_rules, core / "data" / "commerce_rules.json"
        except Exception:
            continue
    raise SystemExit(
        "[vf-check] 找不到规则引擎。\n"
        "  请用 --init-rules 导出独立版，或设置环境变量 VF_BACKEND 指向 backend 目录。\n"
        "  搜索过的位置：" + ", ".join(str(p) for p in _candidate_backends()[:4])
    )


# ────────────────────────── 收集文件 ──────────────────────────

def collect_files(paths: List[str]) -> List[Path]:
    out: List[Path] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            print(f"[vf-check] 跳过不存在的路径：{raw}", file=sys.stderr)
            continue
        if p.is_file():
            if p.suffix.lower() in IMAGE_EXT | VIDEO_EXT:
                out.append(p)
            continue
        for f in sorted(p.rglob("*")):
            if f.is_file() and f.suffix.lower() in IMAGE_EXT | VIDEO_EXT:
                out.append(f)
    # 去重（同一文件可能通过多条路径进来）
    seen, uniq = set(), []
    for f in out:
        k = str(f.resolve()).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def resolve_platform(raw: str) -> str:
    p = (raw or "all").strip().lower()
    return PLATFORM_ALIASES.get(p, p)


# ────────────────────────── 渲染 ──────────────────────────

def _wrap(text: str, width: int = 78, indent: str = "      ") -> List[str]:
    out, line = [], ""
    for ch in text:
        line += ch
        if len(line) >= width and ch in "，。；、）】":
            out.append(indent + line)
            line = ""
    if line:
        out.append(indent + line)
    return out


def render_console(res: Dict[str, Any], show_pass: bool = False) -> None:
    plat = res["platform"]
    label = PLATFORM_LABEL.get(plat, plat)
    W = 74
    print("=" * W)
    print(f" 商品素材合规检查 · {label}".ljust(W))
    print("=" * W)
    print(f" 扫描 {res['total']} 个文件  →  {res['passed']} 通过 / {res['failed']} 不通过")
    if plat == "all":
        print(" ⚠ 全部平台模式：按最严标准判定")
    print("-" * W)

    bad_items = [i for i in res["items"] if not i["ok"]]
    for it in bad_items:
        name = Path(it["path"]).name
        print(f"\n✗ {name}")
        probe = it.get("probe") or {}
        bits = [f"{probe.get('w')}×{probe.get('h')}" if probe.get("w") else "",
                str(probe.get("format") or ""),
                f"{round((probe.get('bytes') or 0)/1024)}KB" if probe.get("bytes") else ""]
        bits = [b for b in bits if b]
        if bits:
            print("      " + " · ".join(bits))
        for c in sorted(it.get("errors", []), key=lambda x: x["id"]):
            print(f"   {SEV_ICON.get(c['severity'],'*')} [{c['id']}] {c.get('title','')}")
            for ln in _wrap(f"为什么：{c.get('why','')}"):
                print(ln)
            for ln in _wrap(f"怎么改：{c.get('fix','')}"):
                print(ln)
        for c in it.get("warnings", []):
            print(f"   ! [{c['id']}] {c.get('title','')} — {c.get('fix','')}")

    # 需人工确认
    manual_map: Dict[str, Dict[str, Any]] = {}
    for it in res["items"]:
        for c in it.get("manual", []):
            e = manual_map.setdefault(c["id"], {"c": c, "n": 0})
            e["n"] += 1
    if manual_map:
        print("\n" + "-" * W)
        print(" 需人工确认（机器判不了，需你或运营看一眼）")
        for cid, e in sorted(manual_map.items(), key=lambda x: -x[1]["n"]):
            c = e["c"]
            print(f"   ? [{cid}] {c.get('title','')}  （{e['n']} 个文件涉及）")
            for ln in _wrap(f"确认要点：{c.get('fix','')}"):
                print(ln)

    # 规则聚合
    if res.get("summary"):
        print("\n" + "-" * W)
        print(" 最常不过的规则（按出现次数）")
        for s in res["summary"][:6]:
            icon = SEV_ICON.get(s["severity"], "*")
            print(f"   {icon} {s['count']:>3}×  [{s['id']}] {s.get('title','')}")
            print(f"        怎么改：{s.get('fix','')}")

    if show_pass:
        for it in res["items"]:
            if it["ok"]:
                print(f"   ✓ {Path(it['path']).name}")

    print("\n" + "=" * W)
    if res["failed"] == 0:
        print(" 结论：全部通过，可以发布。")
    else:
        print(f" 结论：{res['failed']} 个文件存在不可发布问题，先修再传。")
    print("=" * W)


def render_report_html(res: Dict[str, Any], out_path: Path) -> None:
    """生成**可直接发微信/浏览器打开**的 HTML 报告（含缩略图 + 红黄绿状态）。

    比 Markdown 报告更适合当成交付物：客户手机上点开就能看图看问题。
    """
    import base64
    import html as _h

    plat = res.get("platform", "")
    label = PLATFORM_LABEL.get(plat, plat)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    esc = _h.escape

    def thumb(path: str) -> str:
        """缩略图转 base64 data URI（宽度上限 240px，控制体积）。"""
        try:
            from PIL import Image
            import io as _io
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((240, 240))
                buf = _io.BytesIO()
                im.save(buf, "JPEG", quality=72)
                b64 = base64.b64encode(buf.getvalue()).decode()
            return f"data:image/jpeg;base64,{b64}"
        except Exception:
            return ""

    bad = [i for i in res["items"] if not i["ok"]]
    okc = len(res["items"]) - len(bad)
    L: List[str] = []
    A = L.append

    A("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>商品图合规巡检报告</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f1115;color:#e8eaed;font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;padding:0 0 60px}
.wrap{max-width:900px;margin:0 auto;padding:0 18px}
header{padding:34px 0 24px;border-bottom:1px solid #262b36;margin-bottom:26px}
h1{font-size:23px;letter-spacing:-.01em}
.meta{color:#9aa3b2;font-size:13.5px;margin-top:8px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:22px 0 30px}
.stat{background:#171a21;border:1px solid #262b36;border-radius:12px;padding:16px}
.stat b{display:block;font-size:30px;font-weight:700;letter-spacing:-.02em}
.stat span{font-size:13px;color:#9aa3b2}
.r{color:#f87171}.g{color:#4ade80}.y{color:#fbbf24}
h2{font-size:17px;margin:30px 0 14px;padding-bottom:9px;border-bottom:1px solid #262b36}
.card{background:#171a21;border:1px solid #262b36;border-radius:12px;padding:16px;margin-bottom:12px}
.card.bad{border-color:#5a2626}.card.ok{border-color:#1f5133}
.row{display:flex;gap:14px;align-items:flex-start}
.thumb{flex:0 0 96px;width:96px;height:96px;border-radius:8px;object-fit:cover;background:#0b0d11;border:1px solid #262b36}
.name{font-weight:600;font-size:15px;word-break:break-all}
.sub{color:#8b94a3;font-size:12.5px;margin-top:3px}
.badge{display:inline-block;font-size:12px;padding:2px 9px;border-radius:99px;font-weight:600;margin-left:8px}
.b-r{background:#3b1a1a;color:#fca5a5}.b-g{background:#14321f;color:#86efac}.b-y{background:#3a2f10;color:#fcd34d}
.iss{margin-top:11px;padding-top:11px;border-top:1px solid #232833}
.iss div{margin-bottom:7px;font-size:14px}
.rid{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:#7c8698;margin-left:6px}
.lab{display:inline-block;min-width:42px;font-weight:700}
.lab.e{color:#f87171}.l.w{color:#fbbf24}.l.m{color:#60a5fa}
.why{color:#8b94a3;font-size:13px;margin-top:2px}
.fix{color:#cbd5e1;font-size:13.5px;margin-top:3px;padding-left:10px;border-left:2px solid #3a4354}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:12px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #262b36}
th{color:#9aa3b2;font-weight:600;font-size:12.5px}
td.c{text-align:center;width:58px}
.ok2{color:#4ade80}.no{color:#5b6472}
.note{background:#1a1710;border-left:3px solid #fbbf24;padding:13px 16px;border-radius:0 10px 10px 0;color:#e3d9bf;font-size:13.5px;margin-top:16px}
.note.g{background:#101a13;border-color:#4ade80;color:#d5e8da}
footer{color:#6b7482;font-size:12.5px;margin-top:34px;padding-top:18px;border-top:1px solid #262b36}
@media(max-width:640px){.stats{grid-template-columns:1fr}.row{flex-direction:column}.thumb{width:100%;height:150px;flex:none}}
</style></head><body><div class="wrap">""")

    A(f'<header><h1>商品图合规巡检报告</h1>')
    A(f'<div class="meta">平台：{esc(label)}（{esc(plat)}）　·　检查时间：{esc(now)}　·　文件数：{res["total"]}</div></header>')

    A(f'<div class="stats">')
    A(f'<div class="stat"><b>{res["total"]}</b><span>检查文件数</span></div>')
    A(f'<div class="stat"><b class="g">{okc}</b><span>通过</span></div>')
    A(f'<div class="stat"><b class="r">{len(bad)}</b><span>需整改</span></div>')
    A('</div>')

    A('<div class="note g">')
    if bad:
        A(f'<b>结论：{len(bad)} 个文件存在会被驳回的项</b>，已逐条列出问题、依据与改法。先按"怎么改"处理，处理后可免费复检一次。')
    else:
        A('<b>结论：全部通过，可发布。</b>仍有几项需人工确认（机器判不了），见下方"需人工确认"。')
    A('</div>')

    if bad:
        A('<h2>一、需整改（会被驳回）</h2>')
        for it in bad:
            A('<div class="card bad"><div class="row">')
            t = thumb(it["path"])
            if t:
                A(f'<img class="thumb" src="{t}" alt="">')
            A('<div style="min-width:0">')
            A(f'<div class="name">{esc(Path(it["path"]).name)}<span class="badge b-r">需整改</span></div>')
            pr = it.get("probe") or {}
            bits = []
            if pr.get("w"):
                bits.append(f'{pr.get("w")}×{pr.get("h")}')
            if pr.get("format"):
                bits.append(str(pr["format"]))
            if pr.get("bytes"):
                bits.append(f'{round(pr["bytes"]/1024)}KB')
            A(f'<div class="sub">{" · ".join(bits)}</div>')
            A('</div></div>')
            A('<div class="iss">')
            for c in it.get("errors", []):
                A(f'<div><span class="lab e">✗</span><b>{esc(c.get("title",""))}</b><span class="rid">{esc(c.get("id",""))}</span>')
                A(f'<div class="why">依据：{esc(c.get("why",""))}</div>')
                A(f'<div class="fix">怎么改：{esc(c.get("fix",""))}</div></div>')
            for c in it.get("warnings", []):
                A(f'<div><span class="lab w">!</span><b>{esc(c.get("title",""))}</b><span class="rid">{esc(c.get("id",""))}</span>')
                A(f'<div class="fix">建议：{esc(c.get("fix",""))}</div></div>')
            A('</div></div>')

    # 需人工确认
    manual_map: Dict[str, Dict[str, Any]] = {}
    for it in res["items"]:
        for c in it.get("manual", []):
            e = manual_map.setdefault(c["id"], {"c": c, "n": 0})
            e["n"] += 1
    if manual_map:
        A('<h2>二、需人工确认（机器判不了，请运营过一眼）</h2>')
        A('<div class="card">')
        for cid, e in sorted(manual_map.items(), key=lambda x: -x[1]["n"]):
            c = e["c"]
            A(f'<div><span class="lab m">?</span><b>{esc(c.get("title",""))}</b>'
              f'<span class="rid">{esc(cid)}</span> · {e["n"]} 个文件</div>')
            A(f'<div class="fix">确认要点：{esc(c.get("fix",""))}</div>')
        A('</div>')

    if res.get("summary"):
        A('<h2>三、问题分布</h2>')
        A('<table><thead><tr><th>次数</th><th>规则</th><th>说明</th><th>怎么改</th></tr></thead><tbody>')
        for s in res["summary"]:
            A(f'<tr><td class="c">{s["count"]}</td>'
              f'<td><span class="rid">{esc(s["id"])}</span></td>'
              f'<td>{esc(s.get("title",""))}</td>'
              f'<td style="color:#cbd5e1">{esc(s.get("fix",""))}</td></tr>')
        A('</tbody></table>')

    A('<h2>四、规则依据</h2><div class="card">')
    seen = set()
    for s in res.get("summary") or []:
        sid = s["id"]
        if sid in seen:
            continue
        seen.add(sid)
        A(f'<div style="margin-bottom:7px"><span class="rid">{esc(sid)}</span> {esc(s.get("title",""))}'
          f'<div class="why">{esc(s.get("why",""))}</div></div>')
    A('</div>')

    A('<footer>本报告由 VideoForge 合规规则引擎自动生成。规则集随平台细则更新，'
      '最终以平台实际审核结果为准。本报告为合规性判断，不构成法律意见。</footer>')
    A('</div></body></html>')

    out_path.write_text("\n".join(L), encoding="utf-8")


def render_report(res: Dict[str, Any], rule_sources: Dict[str, Dict[str, Any]],
                  out_path: Path) -> None:
    plat = res["platform"]
    label = PLATFORM_LABEL.get(plat, plat)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    L: List[str] = []
    A = L.append

    A(f"# 商品素材合规检查报告")
    A("")
    A(f"- **平台**：{label}（`{plat}`）")
    A(f"- **检查时间**：{now}")
    A(f"- **文件数**：{res['total']}")
    verdict = ("✅ 全部通过，可发布" if res["failed"] == 0
               else "❌ {} 个文件存在不可发布问题".format(res["failed"]))
    A("- **结论**：{}".format(verdict))
    A("")

    bad = [i for i in res["items"] if not i["ok"]]
    if bad:
        A("## 一、不可发布（必须修）")
        A("")
        for it in bad:
            A(f"### `{Path(it['path']).name}`")
            probe = it.get("probe") or {}
            if probe.get("w"):
                A(f"尺寸 {probe.get('w')}×{probe.get('h')} · {probe.get('format')} · "
                  f"{round((probe.get('bytes') or 0)/1024)}KB")
                A("")
            for c in it.get("errors", []):
                A(f"- **{c.get('title','')}** `[{c['id']}]`")
                A(f"  - 依据：{c.get('why','')}")
                A(f"  - 修复：{c.get('fix','')}")
            A("")

    manual_map: Dict[str, Dict[str, Any]] = {}
    for it in res["items"]:
        for c in it.get("manual", []):
            e = manual_map.setdefault(c["id"], {"c": c, "n": 0})
            e["n"] += 1
    if manual_map:
        A("## 二、需人工确认")
        A("")
        A("机器无法可靠判定以下项，请人工过一眼：")
        A("")
        for cid, e in sorted(manual_map.items(), key=lambda x: -x[1]["n"]):
            c = e["c"]
            A(f"- **{c.get('title','')}** `[{cid}]`（{e['n']} 个文件）— {c.get('fix','')}")
        A("")

    if res.get("summary"):
        A("## 三、问题分布")
        A("")
        A("| 次数 | 级别 | 规则 | 说明 |")
        A("|---|---|---|---|")
        for s in res["summary"]:
            A(f"| {s['count']} | {s['severity']} | `{s['id']}` | {s.get('title','')} |")
        A("")

    A("## 四、规则依据")
    A("")
    seen = set()
    for s in res.get("summary") or []:
        sid = s["id"]
        if sid in seen:
            continue
        seen.add(sid)
        src = rule_sources.get(sid) or {}
        t = src.get("title", "")
        u = src.get("url", "")
        c = src.get("clause", "")
        if t:
            A(f"- `{sid}` — {t}" + (f" · {c}" if c else "") + (f" · {u}" if u else ""))
    A("")
    A("---")
    A("")
    A("*本报告由 VideoForge 合规规则引擎自动生成。规则集随平台细则更新，"
      "最终以平台实际审核结果为准。*")
    out_path.write_text("\n".join(L), encoding="utf-8")


def export_rules(engine_mod, dest: Path) -> None:
    """导出**完整自包含**的独立版：可直接打包发给客户离线运行。

    目录结构（与引擎内部路径解析保持一致）：
        DIR/vf_check.py
        DIR/core/commerce_rules.py
        DIR/core/data/commerce_rules.json
    """
    dest = dest.resolve()
    (dest / "core" / "data").mkdir(parents=True, exist_ok=True)

    # 1) 规则集（完整保留顶层 platforms / note）
    try:
        raw = json.loads((Path(engine_mod.__file__).parent / "data" / "commerce_rules.json")
                         .read_text(encoding="utf-8"))
    except Exception:
        raw = {"version": "1.0", "rules": engine_mod.load_rules(force=True)}
    if not isinstance(raw, dict):
        raw = {"version": "1.0", "rules": raw}
    raw["rules"] = engine_mod.load_rules(force=True)
    (dest / "core" / "data" / "commerce_rules.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2) 引擎源码
    shutil.copyfile(Path(engine_mod.__file__), dest / "core" / "commerce_rules.py")

    # 3) CLI 本体
    shutil.copyfile(Path(__file__).resolve(), dest / "vf_check.py")

    print(f"[vf-check] 独立版已导出到：{dest}")
    print(f"  vf_check.py")
    print(f"  core/commerce_rules.py")
    print(f"  core/data/commerce_rules.json  （{len(raw['rules'])} 条规则）")
    print(f"  客户只需有 Python + Pillow，把整个目录发过去即可离线运行：")
    print(f"      python vf_check.py <图目录> --platform pdd --report 报告.md")


# ────────────────────────── 主流程 ──────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="vf-check",
        description="AI 商品素材合规检查器：告诉运营这批图能不能过审、缺什么、怎么改",
    )
    ap.add_argument("paths", nargs="*", help="文件或目录（目录会递归查找图片/视频）")
    ap.add_argument("-p", "--platform", default="all",
                    help="平台：pdd/taobao/jd/douyin/amazon/shopify/xiaohongshu/all（默认 all）")
    ap.add_argument("--fix", action="store_true",
                    help="自动修复可自动处理的问题（补 AI 显式角标 + 文件元数据隐式标识）")
    ap.add_argument("--report", default="", help="额外输出 Markdown 报告到指定路径")
    ap.add_argument("--html", default="", help="输出 HTML 报告（含缩略图，适合直接发客户）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    ap.add_argument("--show-pass", action="store_true", help="同时列出通过的文件")
    ap.add_argument("--init-rules", default="", metavar="DIR",
                    help="把规则集导出到 DIR（生成可独立分发的版本）")
    ap.add_argument("--version", action="version", version=f"vf-check {__version__}")
    args = ap.parse_args(argv)

    try:
        engine, rules_path = load_engine()
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2

    if args.init_rules:
        export_rules(engine, Path(args.init_rules))
        return 0

    if not args.paths:
        ap.print_help()
        return 2

    plat = resolve_platform(args.platform)
    files = collect_files(args.paths)
    if not files:
        print(f"[vf-check] 没找到图片/视频。输入路径：{args.paths}", file=sys.stderr)
        return 2

    if args.fix:
        fixed, failed = 0, []
        for f in files:
            try:
                # 1) 加显式角标（引擎产出 _labeled 副本）→ 2) 在副本上写隐式元数据
                #    → 3) 原子替换回原文件（不留副本，目录文件数不变）
                labeled = engine.apply_ai_label_corner(str(f))
                if not labeled:
                    failed.append((f.name, "角标写入失败"))
                    continue
                if not engine.apply_ai_metadata(labeled):
                    failed.append((f.name, "元数据写入失败"))
                    continue
                os.replace(labeled, str(f))
                fixed += 1
            except Exception as e:
                failed.append((f.name, str(e)[:60]))
        print(f"[vf-check] 已补 AI 标识（角标 + 元数据）：{fixed}/{len(files)} 个文件")
        for n, e in failed:
            print(f"   ! {n}: {e}")
        print()

    targets = [plat] if plat != "all" else \
        ["pdd", "taobao", "jd", "douyin", "amazon", "shopify", "xiaohongshu"]

    per_platform: Dict[str, Any] = {}
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for p in targets:
        r = engine.check_batch([str(f) for f in files], platform=p)
        per_platform[p] = {"passed": r["passed"], "failed": r["failed"], "total": r["total"]}
        for it in r["items"]:
            key = it["path"]
            if key not in merged:
                merged[key] = {"path": it["path"], "ok": True, "errors": [], "warnings": [],
                               "manual": [], "checks": [], "probe": it.get("probe") or {},
                               "failed_on": [], "checked_on": []}
                order.append(key)
            m = merged[key]
            m["checked_on"].append(p)
            if not it["ok"]:
                m["ok"] = False
                m["failed_on"].append(p)
            # 同一条规则在多个平台命中时只保留一次（按 id 去重）
            seen_ids = {c["id"] for c in m["errors"]}
            for c in it.get("errors", []):
                if c["id"] not in seen_ids:
                    m["errors"].append(c)
                    seen_ids.add(c["id"])
            seen_w = {c["id"] for c in m["warnings"]}
            for c in it.get("warnings", []):
                if c["id"] not in seen_w:
                    m["warnings"].append(c)
                    seen_w.add(c["id"])
            seen_m = {c["id"] for c in m["manual"]}
            for c in it.get("manual", []):
                if c["id"] not in seen_m:
                    m["manual"].append(c)
                    seen_m.add(c["id"])

    all_items = [merged[k] for k in order]

    # summary：按**文件数**统计（每文件同一规则只计一次），这是"最常不过"最合理的口径
    agg: Dict[str, Dict[str, Any]] = {}
    for m in all_items:
        for c in m["checks"]:
            if c.get("passed"):
                continue
            a = agg.setdefault(c["id"], {"id": c["id"], "title": c.get("title", ""),
                                        "severity": c.get("severity", "error"),
                                        "count": 0, "fix": c.get("fix", ""),
                                        "why": c.get("why", ""),
                                        "source_url": (c.get("source") or {}).get("url")})
            a["count"] += 1
    all_summary = sorted(agg.values(),
                         key=lambda x: (SEV_ORDER.get(x["severity"], 3), -x["count"]))

    res = {
        "platform": plat,
        "total": len(files),
        "passed": sum(1 for i in all_items if i["ok"]),
        "failed": sum(1 for i in all_items if not i["ok"]),
        "items": all_items,
        "summary": all_summary,
        "per_platform": per_platform,
    }

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        render_console(res, show_pass=args.show_pass)
        if plat == "all" and per_platform:
            print("\n 各平台口径对比（同一批图）：")
            for p, v in per_platform.items():
                mark = "✗" if v["failed"] else "✓"
                print(f"   {mark} {PLATFORM_LABEL.get(p,p):<10} {v['passed']}/{v['total']} 通过")

    if args.report or args.html:
        rp_parent = Path(args.report or args.html).parent
        rp_parent.mkdir(parents=True, exist_ok=True)

    if args.report:
        rp = Path(args.report)
        try:
            raw = json.loads(rules_path.read_text(encoding="utf-8"))
            rule_list = raw.get("rules", raw if isinstance(raw, list) else [])
        except Exception:
            rule_list = []
        sources = {}
        for r in rule_list:
            src = r.get("source") or {}
            sources[r.get("id")] = src
        render_report(res, sources, rp)
        print(f"\n[vf-check] Markdown 报告已生成：{rp}")

    if args.html:
        hp = Path(args.html)
        render_report_html(res, hp)
        print(f"[vf-check] HTML 报告已生成：{hp}")

    return 0 if res["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
