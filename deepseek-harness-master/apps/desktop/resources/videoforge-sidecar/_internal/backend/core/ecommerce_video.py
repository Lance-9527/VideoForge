# -*- coding: utf-8 -*-
"""VideoForge · 电商商品视频（product video）功能引擎

═══════════════════════════════════════════════════════════════════
这个功能点解决什么
═══════════════════════════════════════════════════════════════════
电商商品视频 ≠ 剧情短剧。它有三条硬约束是短剧没有的：

  ① **平台规格是硬指标**（时长上限 / 画幅比例 / 分辨率 / 首帧），
     超出就被平台裁切或压缩，成片白做；
  ② **结构是按品类走的转化漏斗**（钩子→痛点→产品出场→卖点演示→
     证据→价格→行动），不是"起承转合"；
  ③ **合规是红线**（广告法绝对化用语、功效宣称），一句"全网第一"
     就能让整条素材下架。

所以这里不写"要专业一点"，而是把三条约束都变成**可执行的数据 + 可查的检查**。

═══════════════════════════════════════════════════════════════════
★ 数据在哪（单一真相源）
═══════════════════════════════════════════════════════════════════
  `skills_builtin/ecommerce-product-video/rules.json`
      ├─ platforms         平台规格表（时长/比例/分辨率/首帧）
      ├─ categories        品类结构模板（每段的秒数区间 + 必须出现什么 + 不许出现什么）
      ├─ compliance        合规禁用词与红线
      └─ ai_generation     用 AI 生成商品视频的已知坑与规避

**本模块不硬编码任何平台数字**，全部从那个 rules.json 读
（与 `cinematic-film-style` 的做法一致：数值只有一份，代码只读不抄）。
读不到就如实返回 `unknown`，**绝不编一套默认规格冒充是平台规则**。

═══════════════════════════════════════════════════════════════════
与 VideoForge 其余部分的关系
═══════════════════════════════════════════════════════════════════
`plan_video()` 产出的 `scenes[]` **沿用 VideoForge 的剧本 schema**
（scene_number / title / location / actions / dialogues / duration_seconds /
beat / prev_link / exit_state），所以可以直接喂给既有的
`core/scriptaudit.py` 体检、`core/agent_tools_stages.run_stage` 推进，
不需要另造一条流水线。

纯函数、零网络、零付费。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.ecommerce")

SKILL_NAME = "ecommerce-product-video"

#: 中文口播语速（与 scriptaudit / dialogue_natural 同源，别各写一份）
CHARS_PER_SECOND = 4.0

#: 未知平台的统一返回（不编造规格）
def _unknown(reason: str) -> Dict[str, Any]:
    return {"state": "unknown", "reason": reason}


# ══════════════════════════════════════════════════════════════════
# 数据装载
# ══════════════════════════════════════════════════════════════════

_RULES_CACHE: Optional[Dict[str, Any]] = None


def rules() -> Dict[str, Any]:
    """读内置 skill 的 rules.json（带缓存）。读不到返回 {}。"""
    global _RULES_CACHE
    if _RULES_CACHE is None:
        try:
            from .skills import builtin_rules
            _RULES_CACHE = builtin_rules(SKILL_NAME) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("读 %s 规则失败：%s", SKILL_NAME, e)
            _RULES_CACHE = {}
    return _RULES_CACHE


def reload_rules() -> Dict[str, Any]:
    """强制重读（开发/测试用）。"""
    global _RULES_CACHE
    _RULES_CACHE = None
    return rules()


def rules_status() -> Dict[str, Any]:
    """自检：规则文件在不在、覆盖了几个平台/品类。"""
    r = rules()
    return {
        "skill": SKILL_NAME,
        "loaded": bool(r),
        "platforms": sorted((r.get("platforms") or {}).keys()),
        "categories": sorted((r.get("categories") or {}).keys()),
        "compliance_lists": sorted((r.get("compliance") or {}).keys()),
        "skill_version": str(r.get("skill_version") or ""),
    }


# ══════════════════════════════════════════════════════════════════
# 平台规格
# ══════════════════════════════════════════════════════════════════


def list_platforms() -> List[Dict[str, Any]]:
    """列出已收录的平台（名字 + 时长 + 比例）。

    ★ 按 key 排序：字典顺序来自 JSON 插入顺序，前端下拉框的默认选中项
      会跟着它变 —— 不排序的话「默认平台」是个偶然值（实测前端默认落到
      douyin_cart，而用户以为在抖店主图）。排序后行为可预测。
    """
    out = []
    for key in sorted((rules().get("platforms") or {}).keys()):
        p = (rules().get("platforms") or {}).get(key)
        if not isinstance(p, dict):
            continue
        out.append({
            "id": key,
            "name": str(p.get("name_zh") or key),
            "duration": p.get("duration_sec"),
            "aspect": p.get("aspect_ratio"),
            "resolution": p.get("resolution"),
            "basis": p.get("basis", ""),
            "source": str(p.get("source") or "")[:160],
        })
    # 把最常用的抖店主图排到第一（其余保持字典序）
    out.sort(key=lambda x: (0 if x["id"] == "douyin_shop_main" else 1, x["id"]))
    return out


def platform_spec(platform: str) -> Dict[str, Any]:
    """取一个平台的完整规格。没收录 → 明确 unknown，**不编**。"""
    key = str(platform or "").strip()
    if not key:
        return _unknown("没给平台")
    plats = rules().get("platforms") or {}
    p = plats.get(key)
    if p is None:
        # 名字模糊匹配（用户可能给中文名）
        for k, v in plats.items():
            if not isinstance(v, dict):
                continue
            if key in (str(v.get("name_zh") or ""), k) or key.lower() == k.lower():
                p, key = v, k
                break
    if not isinstance(p, dict):
        return _unknown("rules.json 未收录平台 %r；已收录：%s"
                        % (platform, "、".join(sorted(plats.keys())) or "（无）"))
    out = dict(p)
    out["id"] = key
    out["state"] = "ok"
    return out


def check_against_platform(*, platform: str, duration_sec: float = 0,
                           width: int = 0, height: int = 0) -> Dict[str, Any]:
    """把「你打算做的成片参数」和平台规格对表。

    返回 checks[]：每条 {id, title, level(pass|fail|unknown), detail}
    """
    spec = platform_spec(platform)
    checks: List[Dict[str, Any]] = []
    if spec.get("state") != "ok":
        return {"state": "unknown", "reason": spec.get("reason"), "checks": []}

    dur = spec.get("duration_sec") or {}
    if isinstance(dur, dict):
        lo, hi = dur.get("min"), dur.get("max")
    else:
        lo = hi = None
    if duration_sec and (lo or hi):
        ok = True
        if lo and duration_sec < lo:
            ok = False
        if hi and duration_sec > hi:
            ok = False
        checks.append({"id": "E1", "title": "时长必须在平台区间内", "level": "pass" if ok else "fail",
                       "detail": "你 %.1fs / 平台 %s–%ss" % (duration_sec, lo or 0, hi or "∞")})
    elif duration_sec:
        checks.append({"id": "E1", "title": "时长必须在平台区间内", "level": "unknown",
                       "detail": "rules.json 没给这个平台的时长区间"})

    asp = spec.get("aspect_ratio")
    if width and height and asp:
        try:
            want = float(width) / float(height)
        except Exception:  # noqa: BLE001
            want = 0.0
        # ★ 平台比例常常是**多值**（"1:1 / 3:4 / 9:16"），必须逐个比、任一匹配即通过。
        #   2026-10-24 修：原来只解析单值，遇到多值就返回 unknown ——
        #   等于所有真实平台都核对不了（由 verify_ecommerce.py 抓出来）。
        allowed = _parse_aspects(asp)
        if allowed:
            hits = [a for a in allowed if abs(want - a) / max(a, 1e-6) < 0.03]
            ok = bool(hits)
            checks.append({"id": "E2", "title": "画幅比例必须匹配",
                           "level": "pass" if ok else "fail",
                           "detail": "你 %dx%d (%.3f) / 平台允许 %s" %
                                     (width, height, want, asp)})
        else:
            checks.append({"id": "E2", "title": "画幅比例必须匹配", "level": "unknown",
                           "detail": "平台比例 %r 解析不出来" % asp})
    elif asp:
        checks.append({"id": "E2", "title": "画幅比例必须匹配", "level": "unknown",
                       "detail": "没给成片宽高，无法比对（平台要求 %s）" % asp})

    return {"state": "ok", "platform": spec.get("name_zh") or spec.get("id"),
            "allowed_aspects": _parse_aspects(asp) if asp else [],
            "checks": checks}


def _parse_aspects(s: str) -> List[float]:
    """把 '1:1 / 3:4 / 9:16' 或 '16:9' 解析成**一组**比值。

    平台规格里比例几乎都是多选（淘宝主图 = 3:4 / 1:1 / 16:9），
    所以核对时必须逐个比，任一匹配就算通过。
    """
    out: List[float] = []
    for part in re.split(r"[/、,，|]|或", str(s or "")):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+(?:\.\d+)?)\s*[:：]\s*(\d+(?:\.\d+)?)$", part)
        if not m:
            continue
        try:
            a, b = float(m.group(1)), float(m.group(2))
            if b:
                out.append(a / b)
        except Exception:  # noqa: BLE001
            continue
    return out


def _parse_aspect(s: str) -> float:
    """兼容旧接口：取第一个比值（内部已改用 `_parse_aspects`）。"""
    a = _parse_aspects(s)
    return a[0] if a else 0.0


# ══════════════════════════════════════════════════════════════════
# 品类结构
# ══════════════════════════════════════════════════════════════════


def list_categories() -> List[Dict[str, Any]]:
    """列出已收录的品类。

    ★ 按 key 排序（理由同 `list_platforms`）：不排序的话前端下拉框默认选中
      会是「美妆/护肤」，然后用户填了件防晒衣、方案里蹦出「素颜特写」——
      实测踩到过。排序只保证行为可预测，不替用户做选择（前端默认空选项）。
    """
    out = []
    for key in sorted((rules().get("categories") or {}).keys()):
        c = (rules().get("categories") or {}).get(key)
        if not isinstance(c, dict):
            continue
        out.append({
            "id": key,
            "name": str(c.get("name_zh") or key),
            "beats": [b.get("beat") for b in (c.get("beats") or []) if isinstance(b, dict)],
            "duration_hint": c.get("duration_hint_sec"),
            "basis": c.get("basis", ""),
        })
    return out


def category_structure(category: str) -> Dict[str, Any]:
    key = str(category or "").strip()
    cats = rules().get("categories") or {}
    c = cats.get(key)
    if c is None:
        for k, v in cats.items():
            if isinstance(v, dict) and key in (str(v.get("name_zh") or ""), k):
                c, key = v, k
                break
    if not isinstance(c, dict):
        return _unknown("rules.json 未收录品类 %r；已收录：%s"
                        % (category, "、".join(sorted(cats.keys())) or "（无）"))
    out = dict(c)
    out["id"] = key
    out["state"] = "ok"
    return out


# ══════════════════════════════════════════════════════════════════
# 合规检查
# ══════════════════════════════════════════════════════════════════


def compliance_terms() -> Dict[str, List[Dict[str, Any]]]:
    """取合规词表（分类 → [{term, why, severity, source}]）。"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for k, v in (rules().get("compliance") or {}).items():
        if isinstance(v, list):
            items = []
            for x in v:
                if isinstance(x, dict) and x.get("term"):
                    items.append({
                        "term": str(x.get("term")),
                        "why": str(x.get("why") or "")[:140],
                        "severity": str(x.get("severity") or "warn"),
                        "source": str(x.get("source") or "")[:120],
                    })
            out[k] = items
    return out


def audit_copy(text: str) -> Dict[str, Any]:
    """给一段文案/台词做电商合规体检。

    查：绝对化用语（广告法第九条）、功效/疗效宣称、虚假承诺、平台违禁话术。
    返回 {ok, score, errors, warnings, hits[], note}
    """
    t = str(text or "")
    if not t.strip():
        return {"ok": False, "error": "文案为空", "score": 0, "errors": 0, "warnings": 0, "hits": []}
    terms = compliance_terms()
    hits: List[Dict[str, Any]] = []
    for cat, items in terms.items():
        for it in items:
            term = it["term"]
            if not term:
                continue
            # 英文/数字用词边界会误伤中文，这里统一用子串匹配（中文没有词边界）
            if term in t:
                idx = t.find(term)
                hits.append({
                    "category": cat, "term": term, "severity": it["severity"],
                    "why": it["why"], "position": idx,
                    "context": t[max(0, idx - 12): idx + len(term) + 12],
                    "source": it["source"],
                })
    errs = sum(1 for h in hits if h["severity"] == "error")
    warns = sum(1 for h in hits if h["severity"] != "error")
    score = max(0, 100 - errs * 15 - warns * 5)
    return {
        "ok": errs == 0,
        "score": score, "errors": errs, "warnings": warns, "hits": hits,
        "checked_categories": sorted(terms.keys()),
        "note": ("合规词表来自 %s/rules.json；依据《中华人民共和国广告法》第九条等。"
                 "**这不是法律意见**，上线前请让法务/平台运营复核。" % SKILL_NAME),
    }


# ══════════════════════════════════════════════════════════════════
# 商品视频方案生成
# ══════════════════════════════════════════════════════════════════


def _scale_beats(beats: List[Dict[str, Any]], total: float) -> List[Dict[str, Any]]:
    """把「比例式」节拍表换算成具体秒数。"""
    if not beats:
        return []
    ratios: List[float] = []
    for b in beats:
        try:
            r = float(b.get("ratio") or 0)
        except Exception:  # noqa: BLE001
            r = 0.0
        ratios.append(r)
    s = sum(ratios)
    if s <= 0:
        ratios = [1.0 / len(beats)] * len(beats)
        s = 1.0
    out, acc = [], 0.0
    for i, b in enumerate(beats):
        if i == len(beats) - 1:
            # 最后一段吃掉余量，保证总和 == total（否则 scriptaudit 的 S3 会判失败）
            sec = max(1.0, total - acc)
        else:
            sec = max(1.0, round(total * ratios[i] / s, 1))
        nb = dict(b)
        # ★ 顺序要紧：先落 start（= 已累计的 acc），再 end，最后才累加。
        #   2026-10-24 修：原来先 `acc += sec` 再取 start，导致每段 start 都往后偏一位、
        #   总时长比目标少 2s（30s 目标只算出 28s）—— 由 selftest 抓出来。
        nb["start"] = round(acc, 1)
        nb["end"] = round(acc + sec, 1)
        nb["duration"] = round(sec, 1)
        out.append(nb)
        acc = nb["end"]
    return out


def plan_video(*, product: str, platform: str = "", category: str = "",
               selling_points: Optional[List[str]] = None,
               audience: str = "", duration_sec: float = 0,
               price_line: str = "", brand: str = "",
               ai_label: str = "plan") -> Dict[str, Any]:
    """给一个商品，产出**可直接进 VideoForge 流水线**的视频方案。

    产出 `scenes[]` 沿用 VideoForge 剧本 schema，所以能直接喂 scriptaudit / 分镜。
    """
    prod = str(product or "").strip()
    if not prod:
        return {"ok": False, "error": "必须给 product（商品名）"}
    sps_raw = [str(x).strip() for x in (selling_points or []) if str(x).strip()]
    warnings: List[str] = []

    # ★ 卖点先过「形容词 → 可拍摄画面」翻译。
    #   理由：调研 §3.4 三铁律之二「能用镜头表达的，就不要用嘴说」；
    #   直接拿「质量特别好」当台词等于没卖点。这里把空泛词换成可拍的动作/数字，
    #   并**原样保留用户原话**（不偷偷改掉用户的意思）。
    rw = rewrite_selling_points(sps_raw)
    sps = [str(x.get("rewritten") or x.get("original") or "").strip()
           for x in rw["items"] if (x.get("rewritten") or x.get("original"))]
    if rw["changed"]:
        warnings.append("有 %d 个卖点是空泛形容词，已改写成可拍摄的表述"
                        "（原话与改写对照见 selling_point_rewrite）" % rw["changed"])
    if rw.get("still_vague"):
        warnings.append("这些卖点无法自动改写，建议人工补一个可验证的事实：%s"
                        % "、".join(rw["still_vague"][:4]))

    spec = platform_spec(platform) if platform else _unknown("没指定平台")
    cat = category_structure(category) if category else _unknown("没指定品类")
    if spec.get("state") != "ok":
        warnings.append(str(spec.get("reason") or "平台规格未知"))
    if cat.get("state") != "ok":
        warnings.append(str(cat.get("reason") or "品类结构未知"))

    # 时长：用户没给就用平台推荐值，再没有就用 30s（并标注这是兜底）
    total = float(duration_sec or 0)
    if not total:
        d = (spec.get("duration_sec") or {}) if spec.get("state") == "ok" else {}
        if isinstance(d, dict):
            total = float(d.get("recommended") or d.get("max") or 0)
        if not total:
            total = 30.0
            warnings.append("时长没给、平台也没推荐值 → 按 30s 兜底（请确认）")

    beats_src = (cat.get("beats") if cat.get("state") == "ok" else None) or []
    if not beats_src:
        beats_src = [b for b in (rules().get("default_beats") or []) if isinstance(b, dict)]
        if beats_src:
            warnings.append("品类结构未收录 → 用通用节拍表")
    beats = _scale_beats(beats_src, total)
    if not beats:
        return {"ok": False, "error": "既没有品类结构也没有通用节拍表（rules.json 缺 default_beats）"}

    # 卖点分配：按 beat 顺序轮转，保证每段都有具体信息
    scenes: List[Dict[str, Any]] = []
    prev_link = "开场即入题，不做前情铺垫"
    for i, b in enumerate(beats):
        sp = sps[i % len(sps)] if sps else ""
        beat_name = str(b.get("beat") or "")
        must = [str(x) for x in (b.get("must") or [])]
        must_not = [str(x) for x in (b.get("must_not") or [])]
        lines: List[Dict[str, Any]] = []
        cap = max(6, int(b["duration"] * CHARS_PER_SECOND * 1.25))
        if beat_name in ("钩子", "hook") and sp:
            txt = _clip_sentence(sp, cap)
            lines.append({"character": "旁白", "text": txt})
        elif beat_name in ("价格/优惠", "price", "行动指令", "cta") and (price_line or sps):
            txt = _clip_sentence(price_line or (sps[-1] if sps else ""), cap)
            if txt:
                lines.append({"character": "旁白", "text": txt})
        elif sp:
            txt = _clip_sentence(sp, cap)
            if txt:
                lines.append({"character": "旁白", "text": txt})
        exit_state = "情绪/注意力落在：%s" % (sp or beat_name)
        scenes.append({
            "scene_number": i + 1,
            "title": ("%s｜%s" % (beat_name, prod))[:24],
            "location": str(b.get("shot_hint") or "商品所在环境"),
            "beat": beat_name,
            "duration_seconds": b["duration"],
            "actions": [str(b.get("visual") or ("呈现%s" % prod))] + must,
            "dialogues": lines,
            "subtitle": _clip_sentence(sp, 18) if sp else "",
            "prev_link": prev_link,
            "exit_state": exit_state,
            "product": prod,
            "must_not": must_not,
        })
        prev_link = "接上一段的%s" % beat_name

    script_text = "\n".join(
        str(d.get("text") or "") for s in scenes for d in (s.get("dialogues") or []))
    audit = audit_copy(script_text + "\n" + " ".join(sps) + "\n" + str(price_line))
    plat_check = check_against_platform(platform=platform, duration_sec=total) \
        if platform else {"state": "unknown", "checks": []}

    return {
        "ok": True,
        "product": prod,
        "brand": brand,
        "audience": audience,
        "platform": spec,
        "category": cat,
        "target_duration": total,
        "beats": [{"beat": b.get("beat"), "start": b.get("start"),
                   "end": b.get("end"), "duration": b.get("duration")} for b in beats],
        "scenes": scenes,
        "selling_points": sps,
        "selling_points_raw": sps_raw,
        "selling_point_rewrite": rw,
        "compliance": audit,
        "platform_check": plat_check,
        "ai_label": ai_label_policy(ai_label),
        "ai_labeling": ai_labeling_checklist(),
        "batch_risk": batch_upload_risk(),
        "warnings": warnings,
        "note": ("scenes 用的是 VideoForge 剧本 schema，可直接送 scriptaudit 体检 / 分镜规划。"
                 "节拍比例与秒数来自 %s/rules.json。" % SKILL_NAME),
    }


def _clip_sentence(s: str, cap: int) -> str:
    """把一句卖点裁到该镜能念完的长度（按 4 字/秒 × 1.25 容差）。"""
    t = re.sub(r"\s+", "", str(s or ""))
    if not t:
        return ""
    if len(t) <= cap:
        return t
    return t[:max(4, cap - 1)] + "…"


def ai_generation_pitfalls() -> List[Dict[str, str]]:
    """AI 生成商品视频的已知坑（从 rules.json 读）。"""
    out = []
    for x in (rules().get("ai_generation") or []):
        if isinstance(x, dict) and x.get("pitfall"):
            out.append({"pitfall": str(x.get("pitfall"))[:140],
                        "mitigation": str(x.get("mitigation") or "")[:160]})
    return out


# ══════════════════════════════════════════════════════════════════
# AI 内容标识（法定义务，不是平台自律）
# ══════════════════════════════════════════════════════════════════


def ai_labeling_requirements() -> Dict[str, Any]:
    """AI 生成内容标识的法定要求（《人工智能生成合成内容标识办法》+ GB 45438-2025）。

    ★ 为什么必须内建在电商商品视频功能里
    ────────────────────────────────────────────────────────────
    调研（2026-10-24）指出：该办法 **2025-09-01 已施行**，要求
    起始画面显著提示 + 播放周边提示 + 元数据隐式标识 + 用户主动声明。
    商品视频正是复用 AI 生成最多的场景，漏掉这一步用户侧直接违规。

    本工具**只做提示与检查，不提供任何去标/隐藏标识能力**。
    """
    lab = rules().get("ai_labeling") or {}
    return {
        "mandatory": bool(lab.get("mandatory", True)),
        "effective_from": str(lab.get("effective_from") or ""),
        "basis": str(lab.get("basis_zh") or ""),
        "requirements": [dict(x) for x in (lab.get("requirements") or [])
                         if isinstance(x, dict)],
        "source": str(lab.get("source") or ""),
        "note": str(lab.get("note_zh") or ""),
    }


def ai_labeling_checklist() -> Dict[str, Any]:
    """出片前必过的 AI 标识清单（给用户/流程看的勾选项）。"""
    lab = ai_labeling_requirements()
    return {
        "ok": True,
        "mandatory": lab["mandatory"],
        "effective_from": lab["effective_from"],
        "must_do": [str(x.get("detail_zh") or x.get("title_zh") or "")
                    for x in lab["requirements"]],
        "note": ("AI 生成的商品视频**必须**做这四步再发布；本工具不提供去标功能。"
                 + (lab.get("note") or "")),
    }


#: AI 标识的三种用户选择
#:   `off`  = 不加（工具不烧、不写；如实提示法律风险）
#:   `plan` = 只给方案（默认：不动文件，告诉你会烧什么）
#:   `apply`= 真烧（对渲染产物调用 apply_ai_label）
AI_LABEL_CHOICES = ("off", "plan", "apply")


def ai_label_policy(choice: str = "plan") -> Dict[str, Any]:
    """把「用户对 AI 标识的选择」变成一份带诚实提示的策略说明。

    ★ 这是**用户可选项**（Lance 2026-10-24：「做成可选项吧，用户可以选择
      真烧进还是不加」）。工具不强制，但必须把「不加的后果」说清楚 ——
      否则等于帮用户踩雷还不吭声。
    """
    c = str(choice or "plan").strip().lower()
    if c not in AI_LABEL_CHOICES:
        c = "plan"
    legal = ("《人工智能生成合成内容标识办法》+ 强制性国标 GB 45438-2025 "
             "自 2025-09-01 施行：AI 生成内容需在起始画面/播放周边给出显著提示、"
             "写入元数据隐式标识，发布者还需主动声明。")
    if c == "off":
        return {
            "choice": "off",
            "enabled": False,
            "burnt": False,
            "text": "你选择了**不加** AI 标识：工具不会往画面烧任何提示、也不写元数据。",
            "warning": ("⚠ 不加标识可能构成违规。%s "
                        "本工具尊重你的选择，但不会替你承担合规责任。" % legal),
            "manual_steps": [],
        }
    if c == "apply":
        return {
            "choice": "apply",
            "enabled": True,
            "burnt": None,   # 还没烧；出片后调 apply_ai_label
            "text": ("你选择了**真烧进画面**：出片后调 `apply_ai_label(成片路径)`，"
                     "会在起始画面烧提示、全程烧角标、写元数据 + 旁挂 JSON。"),
            "warning": "",
            "next_step": "渲染完成后：apply_ai_label(rendered_path, mode='start_and_badge')",
            "manual_steps": [{"layer": "L4", "kind": "用户发布时主动声明",
                              "manual": True,
                              "note": "**这一步只能你自己做**"}],
        }
    return {
        "choice": "plan",
        "enabled": True,
        "burnt": False,
        "text": "默认只给**方案**（不动文件）。要真烧把 choice 设成 `apply`。",
        "warning": "",
        "manual_steps": [{"layer": "L4", "kind": "用户发布时主动声明",
                          "manual": True, "note": "**这一步只能你自己做**"}],
    }



def batch_upload_risk() -> Dict[str, Any]:
    """批量出片的平台违规风险（抖音「批量投稿」二级违规）。"""
    d = rules().get("platform_batch_risk") or {}
    dy = d.get("douyin_batch_upload") or {}
    return {
        "risk": str(dy.get("rule_zh") or ""),
        "mitigation": str(dy.get("mitigation_zh") or ""),
        "note": str(d.get("note_zh") or ""),
    }


# ══════════════════════════════════════════════════════════════════
# AI 标识「实际落标」（不是提示清单，是真的烧进画面 + 写进元数据）
# ══════════════════════════════════════════════════════════════════
#
# ★ 为什么要有这一段
# ────────────────────────────────────────────────────────────────
# 上一版只有 `ai_labeling_checklist()` —— 它告诉你「要做什么」，
# 但没有一行代码真的去做。而《AI 生成合成内容标识办法》+ GB 45438-2025
# 是**法定义务**（2025-09-01 施行），漏了用户侧直接违规。
#
# 这里用 ffmpeg 真实落地三件可自动化的：
#   L1 起始画面显著提示  → drawbox + drawtext，限定在开头 N 秒
#   L2 播放周边提示      → 全程角标（可选）
#   L3 元数据隐式标识    → -metadata comment/description + 旁挂 JSON
# 剩下 L4（用户发布时主动声明）**只能由用户自己做**，本函数如实返回
# `manual_steps` 提醒，不假装替用户完成。
#
# ⚠ 本模块**不提供**去标/隐藏/篡改标识的能力（办法第十条明文禁止）。

#: 本机可用的中文字体候选（按优先级）
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

DEFAULT_START_TEXT = "本视频由 AI 生成"
DEFAULT_BADGE_TEXT = "AI 生成"


def _find_font(font_path: str = "") -> str:
    """找一个能用中文的字体文件。找不到返回空串（**不编**）。"""
    if font_path and os.path.isfile(font_path):
        return font_path
    for c in FONT_CANDIDATES:
        if os.path.isfile(c):
            return c
    return ""


def _ffmpeg() -> str:
    """找 ffmpeg（与 video_remake 同一套查找逻辑）。"""
    try:
        from . import video_remake as VR
        return VR._which("ffmpeg") or ""
    except Exception:  # noqa: BLE001
        import shutil
        return shutil.which("ffmpeg") or ""


def _probe_video(path: str) -> Dict[str, Any]:
    try:
        from . import video_remake as VR
        return VR.probe(path)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:120]}


def _esc_drawtext(s: str) -> str:
    """转义 drawtext 的 text= 参数（冒号、单引号、百分号、反斜杠）。"""
    t = str(s or "")
    t = t.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
    t = t.replace("%", r"\%")
    return t


def _esc_path(p: str) -> str:
    """ffmpeg 滤镜里的文件路径：Windows 反斜杠要换成 / ，冒号要转义。"""
    q = str(p or "").replace("\\", "/")
    return q.replace(":", r"\:")


def ai_label_plan(*, video_path: str = "", mode: str = "start_and_badge",
                  start_seconds: float = 3.0,
                  start_text: str = DEFAULT_START_TEXT,
                  badge_text: str = DEFAULT_BADGE_TEXT) -> Dict[str, Any]:
    """**只出方案不执行**：告诉你会往画面上烧什么、烧在哪、多长。

    让你在真跑前先看一眼，避免把片子烧坏。
    """
    m = str(mode or "start_and_badge")
    steps: List[Dict[str, Any]] = []
    if m in ("start_and_badge", "start_only", "start"):
        steps.append({
            "layer": "L1", "kind": "起始画面显著提示",
            "text": start_text, "time_range": [0.0, float(start_seconds or 3.0)],
            "position": "画面中部偏上（居中）", "style": "深色底框 + 白色粗体",
            "note": "前 %.1f 秒可见；这是「起始画面显著提示」" % float(start_seconds or 3.0),
        })
    if m in ("start_and_badge", "badge_only", "badge"):
        steps.append({
            "layer": "L2", "kind": "全程角标",
            "text": badge_text, "time_range": [0.0, None],
            "position": "左下角（避开平台右侧按钮区与底部字幕带）",
            "style": "半透明黑底 + 白字小号",
            "note": "全程可见；这是「播放周边显著提示」的画面内部分",
        })
    steps.append({
        "layer": "L3", "kind": "元数据隐式标识",
        "detail": "写入 mp4 的 comment / description 标签 + 旁挂 .ai-label.json",
        "note": "机器可读，用于审计追溯",
    })
    steps.append({
        "layer": "L4", "kind": "用户发布时主动声明",
        "detail": "发布环节勾选「内容由 AI 生成」",
        "manual": True,
        "note": "**这一步只能用户自己做**，工具不能代替",
    })
    font = _find_font()
    return {
        "ok": True,
        "mode": m,
        "video": video_path,
        "font": font or "（没找到中文字体）",
        "font_available": bool(font),
        "steps": steps,
        "manual_steps": [s for s in steps if s.get("manual")],
        "warnings": ([] if font else ["没找到中文字体，烧字会变成方块/英文 —— 请用 font_path 指定"]),
        "note": "这是方案，没有动文件。要真烧传 apply=True。",
    }


def apply_ai_label(video_path: str, *, out_path: str = "",
                   mode: str = "start_and_badge",
                   start_seconds: float = 3.0,
                   start_text: str = DEFAULT_START_TEXT,
                   badge_text: str = DEFAULT_BADGE_TEXT,
                   font_path: str = "", crf: int = 20,
                   timeout: int = 1800) -> Dict[str, Any]:
    """**真的**把 AI 标识烧进画面 + 写进元数据（法定义务 L1/L2/L3）。

    L4（发布时主动声明）只能用户自己做，这里如实返回提醒。

    返回：
        {"ok", "out_path", "layers_done": [...], "metadata": {...},
         "sidecar", "manual_steps": [...], "error", "note"}
    """
    import subprocess
    src = str(video_path or "").strip()
    if not src or not os.path.isfile(src):
        return {"ok": False, "error": "视频不存在：%s" % (src or "(空)"),
                "manual_steps": ai_label_plan()["manual_steps"]}
    ff = _ffmpeg()
    if not ff:
        return {"ok": False, "error": "找不到 ffmpeg", "manual_steps": []}
    font = _find_font(font_path)
    if not font:
        return {"ok": False,
                "error": "找不到可用的中文字体（试过 simhei/msyh/simsun）。"
                         "请用 font_path 显式指定一个 .ttf/.ttc。",
                "manual_steps": ai_label_plan()["manual_steps"]}
    pr = _probe_video(src)
    w = int(pr.get("width") or 0) or 1080
    h = int(pr.get("height") or 0) or 1920
    dur = float(pr.get("duration") or 0)

    if not out_path:
        base, ext = os.path.splitext(src)
        out_path = base + ".ailabel" + (ext or ".mp4")
    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    m = str(mode or "start_and_badge")
    # 起始提示：字号约 1/22 画面宽，居中偏上
    fs_main = max(18, int(w / 22))
    # 角标：字号约 1/40 画面宽，左下角
    fs_badge = max(12, int(w / 40))
    filters: List[str] = []
    layers: List[str] = []

    if m in ("start_and_badge", "start_only", "start"):
        t = _esc_drawtext(start_text)
        y_main = int(h * 0.34)
        # 深色底框 + 白字，居中
        filters.append(
            "drawbox=x=0:y=%d:w=iw:h=%d:color=black@0.55:t=fill:enable='between(t,0,%.2f)'"
            % (y_main - int(fs_main * 0.5), int(fs_main * 1.9), float(start_seconds)))
        filters.append(
            "drawtext=fontfile='%s':text='%s':fontcolor=white:fontsize=%d:"
            "x=(w-text_w)/2:y=%d:borderw=2:bordercolor=black@0.8:"
            "enable='between(t,0,%.2f)'"
            % (_esc_path(font), t, fs_main, y_main, float(start_seconds)))
        layers.append("L1")

    if m in ("start_and_badge", "badge_only", "badge"):
        t2 = _esc_drawtext(badge_text)
        # 左下角，避开底部字幕带（距底 22%）与右侧平台按钮区
        y_badge = int(h * 0.78)
        filters.append(
            "drawtext=fontfile='%s':text='%s':fontcolor=white@0.92:fontsize=%d:"
            "x=%d:y=%d:box=1:boxcolor=black@0.45:boxborderw=%d"
            % (_esc_path(font), t2, fs_badge, max(8, int(w * 0.03)), y_badge,
               max(4, int(fs_badge * 0.25))))
        layers.append("L2")

    if not filters:
        return {"ok": False, "error": "mode 不认识：%r（可选 start_only/badge_only/start_and_badge）" % mode,
                "manual_steps": ai_label_plan()["manual_steps"]}

    meta_comment = "AI生成内容标识: 本视频由人工智能生成合成 (依据《人工智能生成合成内容标识办法》/GB 45438-2025)"
    cmd = [ff, "-y", "-loglevel", "error", "-i", src,
           "-vf", ",".join(filters),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
           "-pix_fmt", "yuv420p",
           "-c:a", "copy",
           "-metadata", "comment=%s" % meta_comment,
           "-metadata", "description=%s" % meta_comment,
           "-metadata", "ai_generated=1",
           out_path]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffmpeg 超时（%ss）" % timeout,
                "manual_steps": ai_label_plan()["manual_steps"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "manual_steps": ai_label_plan()["manual_steps"]}
    if p.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) < 1000:
        return {"ok": False,
                "error": "ffmpeg 失败（rc=%s）：%s" % (p.returncode, (p.stderr or "")[:400]),
                "manual_steps": ai_label_plan()["manual_steps"]}

    # L3 旁挂 JSON（机器可读审计证据）
    sidecar = ""
    try:
        side = os.path.splitext(out_path)[0] + ".ai-label.json"
        payload = {
            "labeled_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
            "source": src, "output": out_path,
            "layers_done": layers + ["L3"],
            "start_text": start_text if "L1" in layers else "",
            "badge_text": badge_text if "L2" in layers else "",
            "start_seconds": float(start_seconds) if "L1" in layers else 0,
            "metadata_comment": meta_comment,
            "basis": "《人工智能生成合成内容标识办法》+ GB 45438-2025",
            "manual_pending": ["L4 用户发布时主动声明『内容由 AI 生成』"],
            "note": "本文件是 AI 标识的审计证据，**不要删除**（办法第十条禁止删除/篡改标识）。",
        }
        with open(side, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        sidecar = side
    except Exception as e:  # noqa: BLE001
        logger.warning("写 AI 标识旁挂 JSON 失败（忽略）：%s", e)

    plan = ai_label_plan(mode=mode, start_seconds=start_seconds,
                         start_text=start_text, badge_text=badge_text)
    return {
        "ok": True,
        "out_path": out_path,
        "size_kb": round(os.path.getsize(out_path) / 1024.0, 1),
        "source_duration": dur,
        "size": [w, h],
        "layers_done": layers + ["L3"],
        "metadata": {"comment": meta_comment, "ai_generated": "1"},
        "sidecar": sidecar,
        "manual_steps": plan["manual_steps"],
        "font": font,
        "error": "",
        "note": ("L1/L2/L3 已真烧进文件；**L4「发布时主动声明」只能你自己做**。"
                 "旁挂 JSON 是审计证据，不要删。"),
    }


# ══════════════════════════════════════════════════════════════════
# 卖点 → 可拍摄画面（把形容词翻译成动作或数字）
# ══════════════════════════════════════════════════════════════════
#
# ★ 为什么要有这一段
# ────────────────────────────────────────────────────────────────
# `selling_point_translation.table` 只有 5 条对照，且上一版 `plan_video()`
# 只是把卖点原样塞进 dialogues —— 等于「对照表写了但没被用上」。
# 而「形容词 → 可拍摄动作/可读数字」是**电商视频与品牌片最大的方法论差异**
# （调研 §3.4 三铁律之二：能用镜头表达的，就不要用嘴说）。
#
# 这里做两件事：
#   ① 精确命中对照表 → 直接给映射
#   ② 命中不了但含空泛形容词 → 按**问题类型**给可拍摄的改写模板
#      （不硬编码商品知识，只给「怎么改成能拍的」这个方向）

#: 空泛形容词 / 自夸词（出现就要改写）
VAGUE_ADJECTIVES = (
    "特别好", "非常好", "超级好", "超好", "很好", "极好", "绝了", "很牛", "超牛",
    "质量好", "品质好", "品质高", "高档", "高级", "高级感", "档次高",
    "很划算", "特别划算", "超级划算", "性价比高", "超值", "很值",
    "效果明显", "效果很好", "立竿见影", "用了都说好", "很多人都买", "爆款",
    "很舒服", "很柔软", "很结实", "很耐用", "很轻", "很快",
    "全网最好", "最好用", "最划算", "最强", "顶级", "第一",
)

#: 改写模板：按「这个卖点其实在说什么」分类
REWRITE_TEMPLATES = {
    "quality": {
        "match": ("质量", "品质", "结实", "耐用", "做工", "材质好", "不掉色", "不起球"),
        "how": "拍「时间/次数」的实际结果，而不是形容词",
        "template": "拍 {n} 次/{t} 之后的实拍（同机位对比新的），画面标出 {n} 次/{t}",
        "examples": ["拍洗 30 次后的实拍", "连续使用 100 天后的特写", "拉扯 500 次不变形"],
    },
    "value": {
        "match": ("划算", "性价比", "超值", "便宜", "省钱", "值"),
        "how": "算成单位成本或使用时长，写在画面上",
        "template": "同规格按单位折算 / 摊到每天 {n} 元，数字上屏",
        "examples": ["一箱 12 卷，折算每卷 1.8 元", "用一整年，每天不到 0.5 元"],
    },
    "effect": {
        "match": ("效果", "明显", "有用", "改善", "见效", "立竿见影"),
        "how": "前后同机位对比，画面标时间",
        "template": "{before} vs {after}，同机位同光位，画面标「{t} 后」",
        "examples": ["使用前 vs 使用 7 天后，同机位同光位", "左脸 vs 右脸，画面标 14 天"],
    },
    "social": {
        "match": ("很多人", "都说好", "爆款", "销量", "口碑", "复购"),
        "how": "晒真实评价原话和复购数据（只用后台真实数字，别编）",
        "template": "真实评价截图 + 复购率数字上屏（必须来自后台真实数据）",
        "examples": ["晒 3 条真实评价原话", "复购率 XX%（后台真实数字）"],
    },
    "feel": {
        "match": ("舒服", "柔软", "轻", "凉", "暖", "透气"),
        "how": "用手/身体做出可感知的动作，让观众「看到」触感",
        "template": "手部动作把 {trait} 演出来（捏/揉/贴/甩/扇），配一个数字或对比",
        "examples": ["手捏一下回弹，同时标克重 90g", "贴着皮肤扇两下，标透气率"],
    },
}


#: 程度副词（匹配空泛词前先剥掉，否则「效果很明显」会漏掉「效果明显」）
FILLER_WORDS = ("特别", "非常", "超级", "超", "极其", "十分", "挺", "蛮", "很", "极", "特", "巨", "相当")


def _strip_filler(s: str) -> str:
    t = str(s or "")
    for w in sorted(FILLER_WORDS, key=len, reverse=True):
        t = t.replace(w, "")
    return t


def rewrite_selling_point(sp: str) -> Dict[str, Any]:
    """把一个卖点翻译成**可拍摄的画面**。

    返回：
        {"original", "rewritten", "kind", "why", "shootable", "source", "changed"}

    `kind` 取值：
      · `table`            命中 rules.json 的卖点翻译对照表
      · `already_measurable` 已含数字 —— 本来就是可读的，不改
      · `claim_needs_proof` 是**主张**（含动作/验证词但没有数字）→ 建议补一个数字
      · `quality/value/effect/social/feel` 空泛形容词 → 给可拍摄改写
      · `unknown`          判断不了 → 建议人工补
    """
    from_s = re.sub(r"\s+", "", str(sp or ""))
    if not from_s:
        return {"original": "", "rewritten": "", "kind": "empty",
                "why": "空卖点", "shootable": False, "source": "", "changed": False}

    # ① 精确命中对照表（数据在 rules.json，优先用它的口径）
    table = (rules().get("selling_point_translation") or {}).get("table") or []
    for row in table:
        if not isinstance(row, dict):
            continue
        key = re.sub(r"\s+", "", str(row.get("from") or ""))
        if key and (key == from_s or key in from_s):
            return {"original": str(sp), "rewritten": str(row.get("to") or ""),
                    "kind": "table", "why": "命中 rules.json 的卖点翻译对照表",
                    "shootable": True, "source": "rules.json:selling_point_translation",
                    "changed": True}

    has_number = bool(re.search(r"\d", from_s))
    # ② 已含数字 → 本来就是可读的，不做无谓加工
    if has_number:
        return {"original": str(sp), "rewritten": from_s, "kind": "already_measurable",
                "why": "已含具体数字，本身就是可读的事实 —— 不需要改",
                "shootable": True, "source": "", "changed": False}

    # ③ 是「主张」：有验证动作但没有数字 → 建议补一个可测的数
    concrete = any(w in from_s for w in (
        "洗", "拍", "测", "对比", "演示", "实拍", "同框", "标出", "计时", "拉", "摔",
        "泡", "烤", "压", "刮", "剪", "缝", "充", "放电", "续航", "防", "抗", "耐",
    ))
    has_vague = bool([w for w in VAGUE_ADJECTIVES if w in _strip_filler(from_s)])
    if concrete and not has_vague:
        return {"original": str(sp), "rewritten": from_s,
                "kind": "claim_needs_proof",
                "why": "这是一句**主张**（有验证动作但没数字）。主张要变成证据，"
                       "建议补一个可测的数（次数/时长/百分比/单位）",
                "template": "「%s」→ 「%s + 具体数字」" % (from_s, from_s),
                "alternatives": ["补上次数（洗 30 次后）", "补上时长（用 100 天后）",
                                 "补上实测值（缩水率 <2%）"],
                "shootable": False, "source": "", "changed": False}

    # ④ 空泛形容词 → 按类型给改写模板（先剥程度副词再匹配）
    norm = _strip_filler(from_s)
    hit_vague = [w for w in VAGUE_ADJECTIVES if w in from_s or w in norm]
    if not hit_vague:
        return {"original": str(sp), "rewritten": from_s, "kind": "unknown",
                "why": "没命中对照表也没命中空泛词 —— 无法判断，建议人工补一个可验证的事实",
                "shootable": False, "source": "", "changed": False}

    # 按模板分类找最贴近的
    best_kind, best_hits = "", 0
    for kind, spec in REWRITE_TEMPLATES.items():
        n = sum(1 for k in spec["match"] if k in from_s or k in norm)
        if n > best_hits:
            best_kind, best_hits = kind, n
    if not best_kind:
        best_kind = "quality"    # 兜底：绝大多数自夸都该用「次数/时间」来证明

    spec = REWRITE_TEMPLATES[best_kind]
    example = spec["examples"][0]
    return {
        "original": str(sp),
        "rewritten": example,
        "kind": best_kind,
        "why": "「%s」是空泛形容词，观众无法验证 → %s" % ("/".join(hit_vague[:2]), spec["how"]),
        "template": spec["template"],
        "alternatives": spec["examples"],
        "shootable": True,
        "source": "rewrite_templates:" + best_kind,
        "changed": True,
    }


def rewrite_selling_points(points: List[Any]) -> Dict[str, Any]:
    """批量改写，并给出体检结论。"""
    items = [rewrite_selling_point(str(x)) for x in (points or [])]
    changed = [x for x in items if x.get("changed")]
    unsolved = [x for x in items if not x.get("shootable")]
    return {
        "ok": True,
        "count": len(items),
        "changed": len(changed),
        "items": items,
        "still_vague": [x["original"] for x in unsolved],
        "note": ("形容词必须变成可拍摄的动作或可读的数字（调研 §3.4 三铁律之二："
                 "能用镜头表达的，就不要用嘴说）。"
                 "rewritten 是**建议**，落地时要换成你真实能拍/能测的内容，别照抄示例。"),
    }


def selftest() -> Dict[str, Any]:
    """自检：数据是否可读、合规检查是否真的抓得住、方案是否能生成。"""
    cases = []
    st = rules_status()
    cases.append({"case": "rules_loaded", "passed": bool(st["loaded"]),
                  "platforms": len(st["platforms"]), "categories": len(st["categories"])})

    # 合规：绝对化用语必须被抓到
    bad = "全网最低价，国家级品质，销量第一，绝对有效"
    a = audit_copy(bad)
    cases.append({"case": "compliance_catches_absolute_terms",
                  "passed": a["errors"] >= 2, "errors": a["errors"],
                  "hits": [h["term"] for h in a["hits"]][:6]})

    # 合规：正常文案不该误报
    good = "这件羊毛衫洗后不缩水，袖口做了双层，穿两年不起球。"
    b = audit_copy(good)
    cases.append({"case": "compliance_no_false_positive",
                  "passed": b["errors"] == 0, "errors": b["errors"],
                  "hits": [h["term"] for h in b["hits"]][:6]})

    # 平台：未知平台要如实 unknown
    u = platform_spec("不存在的平台xyz")
    cases.append({"case": "unknown_platform_is_honest",
                  "passed": u.get("state") == "unknown", "reason": str(u.get("reason"))[:80]})

    # 方案：能不能产出符合 schema 的 scenes
    plat = st["platforms"][0] if st["platforms"] else ""
    cat = st["categories"][0] if st["categories"] else ""
    p = plan_video(product="测试羊毛衫", platform=plat, category=cat,
                   selling_points=["洗后不缩水", "双层袖口", "两年不起球"],
                   duration_sec=0)
    ok_schema = bool(p.get("ok")) and all(
        {"scene_number", "title", "location", "actions", "dialogues",
         "duration_seconds", "beat", "prev_link", "exit_state"} <= set(s.keys())
        for s in (p.get("scenes") or []))
    total = round(sum(float(s.get("duration_seconds") or 0) for s in (p.get("scenes") or [])), 1)
    cases.append({"case": "plan_video_produces_vf_schema",
                  "passed": ok_schema and len(p.get("scenes") or []) > 0,
                  "scenes": len(p.get("scenes") or []),
                  "total_seconds": total,
                  "target": p.get("target_duration")})

    # AI 标识必须被内建
    lab = ai_labeling_checklist()
    cases.append({"case": "ai_labeling_builtin",
                  "passed": bool(lab.get("mandatory")) and len(lab.get("must_do") or []) >= 4,
                  "mandatory": lab.get("mandatory"),
                  "effective_from": lab.get("effective_from"),
                  "items": len(lab.get("must_do") or [])})

    # AI 标识要有**真的落标能力**，不能只有清单
    plan_lab = ai_label_plan()
    cases.append({"case": "ai_label_apply_available",
                  "passed": bool(plan_lab.get("ok")) and plan_lab.get("font_available")
                            and len(plan_lab.get("steps") or []) >= 4,
                  "font": plan_lab.get("font"),
                  "layers": [s.get("layer") for s in plan_lab.get("steps") or []],
                  "manual": [s.get("kind") for s in plan_lab.get("manual_steps") or []]})

    # 卖点改写：空泛形容词必须被改写
    r1 = rewrite_selling_point("质量特别好")
    r2 = rewrite_selling_point("UPF50+")
    r3 = rewrite_selling_point("效果很明显")
    cases.append({"case": "selling_point_rewrite",
                  "passed": bool(r1.get("changed")) and r2.get("kind") == "already_measurable"
                            and bool(r3.get("changed")),
                  "vague": r1.get("kind"), "numeric": r2.get("kind"),
                  "filler_stripped": r3.get("kind")})

    # 品类结构必须能换算成秒，且总和必须等于目标（scriptaudit S3 会核对）
    if cat:
        cs = category_structure(cat)
        beats = cs.get("beats") or []
        tot_ratio = sum(float(b.get("ratio") or 0) for b in beats)
        cases.append({"case": "category_beats_ratio_sum_to_one",
                      "passed": abs(tot_ratio - 1.0) < 0.02,
                      "category": cat, "ratio_sum": round(tot_ratio, 3),
                      "beats": len(beats)})
        gaps = [abs(float(s.get("end") or 0) - float((p.get("beats") or [])[i].get("end") or 0))
                for i, s in enumerate(p.get("scenes") or [])][:5]
        cases.append({"case": "plan_total_equals_target",
                      "passed": abs(total - float(p.get("target_duration") or 0)) <= 0.6,
                      "sum": total, "target": p.get("target_duration")})

    return {"passed": all(c["passed"] for c in cases), "cases": cases}
