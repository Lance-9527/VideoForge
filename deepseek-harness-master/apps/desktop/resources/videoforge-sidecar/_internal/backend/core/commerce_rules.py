# -*- coding: utf-8 -*-
"""商品图 / 商品视频 · 合规规则引擎（可批量校验）

═══════════════════════════════════════════════════════════════════
这是什么
═══════════════════════════════════════════════════════════════════
「生成得好看」已经免费了（稿定 ¥0.08/张、Pic Copilot 免费），
**「能不能过审」没人管** —— 而一张图被判违规导致降权/下架，
损失是几千，图本身成本才几毛钱。

这个模块把**平台规范 + AI 标识法规**变成**可执行的校验**：
出图/出片后跑一遍，告诉你哪条不过、为什么不过、怎么改、要不要阻断。

═══════════════════════════════════════════════════════════════════
为什么规则来源分三档
═══════════════════════════════════════════════════════════════════
· regulation —— **法规原文**（网信办《人工智能生成合成内容标识办法》，2025-09-01 施行）
               可信度最高，直接是法律要求
· official   —— 平台官方文档
· community  —— 行业共识值（各平台/类目可能不同，**请按自己的类目核对**）

`confidence=low/medium` 的规则**不要**当法律依据，只当默认闸门。

═══════════════════════════════════════════════════════════════════
能自动查什么、不能自动查什么（重要，别误以为它万能）
═══════════════════════════════════════════════════════════════════
能自动查（有明确机器判据）：
    尺寸 / 比例 / 格式 / 体积 / 纯白背景 / AI 角标是否存在 / 文件元数据是否含 AI 标识
只能标人工（机器不可靠）：
    是不是"别的平台"的水印 / 文字占比 / 商品占比 / 类目专属要求
→ 人工项**不阻断**，但会出现在报告里让你确认。

═══════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════
    from core.commerce_rules import check_file, check_batch, load_rules
    r = check_file("out/main.jpg", platform="pdd")
    r["ok"], r["errors"], r["warnings"], r["manual"], r["fixed_hint"]
    batch = check_batch(["a.jpg", "b.jpg"], platform="taobao")
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # PIL 可能不在（打包时是有的，但别硬依赖）
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore

_RULES_CACHE: Optional[List[Dict[str, Any]]] = None
_RULES_PATH = Path(__file__).resolve().parent / "data" / "commerce_rules.json"

SEV_ORDER = {"error": 0, "warn": 1, "manual": 2}


# ──────────────────────── 规则加载 ────────────────────────


def load_rules(force: bool = False) -> List[Dict[str, Any]]:
    """读规则集（带缓存）。"""
    global _RULES_CACHE
    if _RULES_CACHE is not None and not force:
        return _RULES_CACHE
    rules: List[Dict[str, Any]] = []
    try:
        data = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
        rules = list(data.get("rules") or [])
    except Exception:
        rules = []
    _RULES_CACHE = rules
    return rules


def rules_for(platform: str, scope: Optional[str] = None) -> List[Dict[str, Any]]:
    """取某个平台适用的规则。

    platform 用小写；"cn_all" 视为**所有国内平台通用**。
    """
    p = (platform or "").strip().lower()
    out = []
    for r in load_rules():
        plats = [str(x).lower() for x in (r.get("platforms") or [])]
        if p and p not in plats and "cn_all" not in plats:
            continue
        # scope='file' 的规则（元数据隐式标识）对**所有**文件类型都适用，
        # 不能因为 scope 被过滤成 'image'/'video' 就丢掉。
        rs = r.get("scope")
        if scope and rs and rs != scope and rs != "file":
            continue
        out.append(r)
    return out


# ──────────────────────── 图片探测 ────────────────────────


def _probe_image(path: str) -> Dict[str, Any]:
    """取图片的基本信息（尺寸/格式/体积/四角像素）。"""
    info: Dict[str, Any] = {"ok": False, "path": path}
    try:
        info["bytes"] = os.path.getsize(path)
    except Exception:
        return info
    if Image is None:
        return info
    try:
        with Image.open(path) as im:
            info["ok"] = True
            info["w"], info["h"] = im.size
            info["format"] = (im.format or "").lower()
            info["mode"] = im.mode
            rgb = im.convert("RGB")
            w, h = rgb.size
            corners = [(2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3)]
            info["corner_px"] = [rgb.getpixel(c) for c in corners]
            info["center_px"] = rgb.getpixel((w // 2, h // 2))
            # 白底判定：四角 + 中心接近纯白且饱和度低
            samples = list(info["corner_px"]) + [info["center_px"]]
            white = 0
            for (r, g, b) in samples:
                mx, mn = max(r, g, b), min(r, g, b)
                sat = 0 if mx == 0 else (mx - mn) / mx
                if mn >= 240 and sat <= 0.12:
                    white += 1
            info["white_ratio"] = white / len(samples)
            # 右下角区域是否可能有角标（与四角底色不同的小块）
            reg = rgb.crop((int(w * 0.72), int(h * 0.82), w, h))
            px = list(reg.getdata())
            bg = info["corner_px"][3]
            diff = 0
            for (r, g, b) in px:
                if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) > 42:
                    diff += 1
            info["corner_marker_ratio"] = (diff / len(px)) if px else 0.0
    except Exception:
        pass
    return info


def _probe_file_meta(path: str) -> Dict[str, Any]:
    """读文件元数据里的文本（PNG tEXt / JPEG COM / MP4 常见 tag）。"""
    out: Dict[str, str] = {}
    p = Path(path)
    try:
        if p.suffix.lower() == ".png":
            if Image is not None:
                with Image.open(path) as im:
                    for k, v in (getattr(im, "text", None) or {}).items():
                        out[str(k)] = str(v)
        elif p.suffix.lower() in (".jpg", ".jpeg"):
            # ★ JPEG 的 COM 段标记是 0xFFFE（不是 ASCII "COM"）；
            #   APPn 段（PIL 里的 im.app）是 EXIF 类信息，COM 在 im.info['comment']。
            #   原来只找 ASCII b"COM" -> 永远读不到自己写的标识。
            try:
                if Image is not None:
                    with Image.open(path) as im:
                        info = getattr(im, "info", None) or {}
                        for k in ("comment", "description", "software", "author", "title", "xml"):
                            if info.get(k):
                                out[str(k)] = str(info[k])
                        app = getattr(im, "app", None) or {}
                        for k, v in list(app.items())[:12]:
                            out["APP%d" % int(k)] = str(v)[:300]
            except Exception:
                pass
            data = p.read_bytes()
            i = data.find(b"\xff\xfe")
            if i > 0:
                seg = data[i + 4: i + 4 + 400]
                try:
                    out["COM"] = seg.decode("utf-8", "ignore")
                except Exception:
                    pass
        elif p.suffix.lower() in (".mp4", ".mov", ".m4v"):
            data = p.read_bytes()
            for tag in (b"AI", b"XMP", b"description", b"comment", b"title", b"author", b"software", b"creation_time"):
                j = data.find(tag)
                if j > 0:
                    out[tag.decode("ascii", "ignore")] = data[j:j + 200].decode("utf-8", "ignore")
    except Exception:
        pass
    return out


# ──────────────────────── 单条规则校验 ────────────────────────


def _check_one(rule: Dict[str, Any], img: Dict[str, Any], meta: Dict[str, str],
               is_video: bool) -> Dict[str, Any]:
    """跑一条规则。返回 {id, severity, title, passed, detail, fix, why, source}"""
    chk = rule.get("check") or {}
    kind = chk.get("kind")
    params = chk.get("params") or {}
    passed = True
    detail = ""

    if kind == "min_size":
        mw, mh = int(params.get("min_w", 0)), int(params.get("min_h", 0))
        w, h = img.get("w") or 0, img.get("h") or 0
        if w < mw or h < mh:
            passed = False
            detail = f"当前 {w}x{h}，要求 ≥ {mw}x{mh}"

    elif kind == "aspect":
        w, h = img.get("w") or 0, img.get("h") or 0
        if w and h:
            ratio = w / h
            tol = float(params.get("tolerance", 0.02))
            if abs(ratio - 1.0) > tol:
                passed = False
                detail = f"当前 {w}x{h}（{ratio:.3f}），要求 1:1（容差 ±{tol}）"

    elif kind == "file":
        fmts = [str(x).lower() for x in (params.get("formats") or [])]
        fmt = img.get("format") or ""
        if fmts and fmt and fmt not in fmts:
            passed = False
            detail = f"当前格式 {fmt.upper()}，要求 {'/'.join(fmts).upper()}"
        mb = params.get("max_bytes")
        nb = params.get("min_bytes")
        b = img.get("bytes") or 0
        if mb and b > int(mb):
            passed = False
            detail = f"当前 {b/1048576:.2f}MB，超上限 {int(mb)/1048576:.0f}MB"
        if nb and 0 < b < int(nb):
            passed = False
            detail = f"当前 {b} 字节，小于下限 {int(nb)}"

    elif kind == "background_white":
        wr = float(img.get("white_ratio") or 0)
        need = float(params.get("min_white_ratio", 0.55))
        if wr < need:
            passed = False
            detail = f"白底采样通过率 {wr:.0%}，要求 ≥ {need:.0%}"

    elif kind == "ai_marker":
        # 2026-10-27 修正: 原来用"右下角与底色不同的像素比例",
        # 结果**纯色背景图也判 PASS**(背景本身就和角点像素不同)。
        # 这类误判很危险: 用户以为合规了。
        # 现在改成**保守策略**: 机器只给"疑似", 一律交给人工确认,
        # 绝不自动判过。
        cov = float(img.get("corner_marker_ratio") or 0)
        detail = (f"需人工确认: 右下角是否有显著的『AI生成』标识角标"
                  f"(启发式覆盖率 {cov:.2%}, 仅供参考, 不可作为合规结论)")

    elif kind == "ai_marker_video":
        # 视频：这里只做流程提示（真要判首帧需抽帧，成本高）
        passed = True
        detail = "需人工确认：视频起始画面是否有『AI生成』显著标识"

    elif kind == "metadata_ai_label":
        blob = " ".join(f"{k} {v}" for k, v in meta.items()).lower()
        keys = [str(x).lower() for x in (params.get("keys") or [])]
        hit = [k for k in keys if k in blob]
        if not hit:
            passed = False
            detail = ("元数据里没找到 AI 标识关键词（生成属性 / 工具名 / 内容编号）。"
                      "导出时应写入 tEXt / COM / meta")

    elif kind == "manual":
        passed = True
        detail = "需人工确认：" + "、".join(str(x) for x in (params.get("look_for") or [params.get("typical_limit", "见平台规则")]))

    elif kind == "process_checklist":
        passed = True
        detail = "流程项：" + "；".join(str(x) for x in (params.get("items") or []))

    else:
        passed = True
        detail = "（无对应校验实现）"

    src = rule.get("source") or {}
    return {
        "id": rule.get("id"),
        "severity": rule.get("severity", "warn"),
        "title": rule.get("title"),
        "scope": rule.get("scope"),
        "passed": passed,
        "detail": detail,
        "fix": rule.get("fix"),
        "why": rule.get("why"),
        "source_type": src.get("type"),
        "source_url": src.get("url"),
        "source_clause": src.get("clause"),
        "confidence": src.get("confidence"),
    }


# ──────────────────────── 对外接口 ────────────────────────


def check_file(path: str, platform: str = "", scope: str = "") -> Dict[str, Any]:
    """校验单个文件（图片或视频）。

    Returns:
        {"path", "platform", "ok", "errors", "warnings", "manual", "checks", "scanned"}
        ok = 没有 error 级问题（warn/manual 不阻断）
    """
    p = str(path)
    ext = Path(p).suffix.lower()
    is_video = ext in (".mp4", ".mov", ".m4v", ".webm")
    scope = scope or ("video" if is_video else "image")

    img = _probe_image(p) if not is_video else {"ok": True, "path": p, "bytes": _safe_size(p)}
    meta = _probe_file_meta(p)
    rules = rules_for(platform, scope=scope)

    checks = [_check_one(r, img, meta, is_video) for r in rules]
    errors = [c for c in checks if not c["passed"] and c["severity"] == "error"]
    warns = [c for c in checks if not c["passed"] and c["severity"] == "warn"]
    manual = [c for c in checks if c["severity"] == "manual"]
    return {
        "path": p,
        "platform": platform,
        "scope": scope,
        "ok": not errors,
        "errors": errors,
        "warnings": warns,
        "manual": manual,
        "checks": checks,
        "scanned": len(checks),
        "probe": {"w": img.get("w"), "h": img.get("h"), "format": img.get("format"),
                  "bytes": img.get("bytes"), "white_ratio": img.get("white_ratio"),
                  "corner_marker_ratio": img.get("corner_marker_ratio"),
                  "meta_keys": sorted(meta.keys())[:10]},
    }


def check_batch(paths: List[str], platform: str = "") -> Dict[str, Any]:
    """批量校验（商品图 200 张这种场景）。"""
    items = [check_file(p, platform=platform) for p in paths]
    bad = [i for i in items if not i["ok"]]
    return {
        "platform": platform,
        "total": len(items),
        "passed": len(items) - len(bad),
        "failed": len(bad),
        "items": items,
        "summary": _summarize(items),
    }


def _summarize(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按规则聚合（告诉你『哪条规则最常不过』）。"""
    agg: Dict[str, Dict[str, Any]] = {}
    for it in items:
        for c in it["checks"]:
            if c["passed"]:
                continue
            key = c["id"]
            a = agg.setdefault(key, {"id": key, "title": c["title"], "severity": c["severity"],
                                    "count": 0, "fix": c["fix"], "why": c["why"],
                                    "source_url": c.get("source_url"), "confidence": c.get("confidence")})
            a["count"] += 1
    return sorted(agg.values(), key=lambda x: (SEV_ORDER.get(x["severity"], 3), -x["count"]))


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def apply_ai_metadata(src_path: str, out_path: str = "",
                      tool: str = "VideoForge", content_id: str = "",
                      label: str = "AI生成") -> Optional[str]:
    """给图片写入 **隐式标识**（文件元数据）。

    ★《人工智能生成合成内容标识办法》第五条：
      应在生成合成内容的**文件元数据**中添加隐式标识，包含
      生成合成内容属性信息、服务提供者名称或者编码、内容编号等制作要素信息；
      并鼓励添加数字水印。
      —— 第十条禁止恶意删除/篡改/伪造/隐匿标识，所以这是**必做**动作，不是可选。

    写入位置：PNG → tEXt；JPEG → COM 段；两者都会带
      "AI生成 / Content-Type: ai-generated / Content-Provider: <tool> / Content-ID: <id>"

    Returns: 输出路径；失败返回 None
    """
    if Image is None:
        return None
    out = out_path or src_path
    try:
        from PIL import Image as _I, PngImagePlugin
        with _I.open(src_path) as im:
            im.load()
            fmt = (im.format or "").upper()
            # ★ 按**输出**后缀决定实际写入格式。
            #   原来按输入格式决定：输入 PNG、输出 .jpg 时，
            #   会把 PNG 内容写进 .jpg 文件 —— 扩展名与真实格式不符，
            #   后面按 .jpg 读就取不到 EXIF/COM 里的 AI 标识（批量校验全军覆没）。
            out_fmt = (os.path.splitext(out)[1].lstrip(".").lower() or fmt.lower()).upper()
            if out_fmt == "PNG":
                # ★ PNG 的 tEXt **键和值都只能 latin-1**（PIL add_text 会
                #   UnicodeEncodeError）。所以隐式标识用**国际通用的 ASCII 键值**
                #   （平台/工具链都认），中文的『AI生成』走**像素层显式标识**
                #   （apply_ai_label_corner）—— 两条标识要求分别满足，不冲突。
                info = PngImagePlugin.PngInfo()
                try:
                    for k, v in (getattr(im, "text", None) or {}).items():
                        info.add_text(str(k), str(v))
                except Exception:
                    pass
                info.add_text("Comment", "AI-generated content")
                info.add_text("Title", label)
                info.add_text("Software", tool)
                info.add_text("Description",
                             "Content-Type: ai-generated; Content-Provider: %s; Content-ID: %s"
                             % (tool, content_id or ""))
                info.add_text("Author", tool)
                im.save(out, "PNG", pnginfo=info)
                return out
            # JPEG / 其它：走 EXIF + COM
            exif = im.getexif()
            try:
                exif[0x010E] = label            # ImageDescription
                exif[0x0131] = tool             # Software
                exif[0x9286] = "ai-generated"   # UserComment
                exif[0x010F] = "VideoForge " + (content_id or "")
            except Exception:
                pass
            if out_fmt in ("JPEG", "JPG"):
                im.save(out, "JPEG", quality=94, exif=exif,
                         comment=(f"{label} | Content-Type: ai-generated | "
                                  f"Content-Provider: {tool} | Content-ID: {content_id}").encode("utf-8"))
            elif out_fmt == "WEBP":
                im.save(out, "WEBP", quality=94, exif=exif)
            else:
                im.save(out, exif=exif)
            return out
    except Exception:
        return None


def apply_ai_label_corner(image_path: str, out_path: str = "",
                          text: str = "AI生成", font_size: int = 28) -> Optional[str]:
    """给图片加 AI 标识角标（导出前必做，否则违反标识办法）。

    ★ 这不是"去水印"，是**加**标识 —— 《办法》第十条明确禁止恶意删除、
      篡改、伪造、隐匿标识。

    Returns: 输出路径；失败返回 None
    """
    if Image is None:
        return None
    try:
        from PIL import ImageDraw, ImageFont
        with Image.open(image_path) as im:
            rgb = im.convert("RGB")
            w, h = rgb.size
            draw = ImageDraw.Draw(rgb, "RGBA")
            fs = max(14, min(font_size, w // 14))
            font = None
            for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
                        r"C:\Windows\Fonts\arial.ttf"):
                if os.path.exists(cand):
                    try:
                        font = ImageFont.truetype(cand, fs)
                        break
                    except Exception:
                        continue
            tw = int(len(text) * fs * 0.85) + 16
            th = int(fs * 1.6)
            x0, y0 = w - tw - 8, h - th - 8
            draw.rectangle([x0, y0, x0 + tw, y0 + th], fill=(0, 0, 0, 140))
            draw.text((x0 + 8, y0 + th // 2), text, fill=(255, 255, 255, 255),
                      font=font, anchor="lm")
            out = out_path or (str(image_path).rsplit(".", 1)[0] + "_labeled.jpg")
            rgb.save(out, "JPEG", quality=94)
            return out
    except Exception:
        return None
