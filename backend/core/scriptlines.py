# -*- coding: utf-8 -*-
"""VideoForge · 剧本台词 → 分镜回填（"第一镜没声音"的真实根因）

★★ 用户实测反馈（2026-09-14）：
    「我点击后还是单独的那个AI女音，**且第一个镜头没有声音（剧本是有对话的）**」

查真实数据后确认，第一镜没声音**不是**合成失败，而是：

    剧本（`scripts.scenes` / `script_scene_details`）里那一场**写着对话**，
    但生成出来的分镜 `layer2_timeline` **根本没有 `dialogue` 字段** ——
    时间轴只有 action / expression / camera 三项。
    （实测 caa9904f：shot#1 的 3 段全无 dialogue，而 shot#2~#5 每段都有。）

于是"配音"这条链在第一环就断了：没有台词 → 没东西可念 → 静音，
而界面只淡淡说一句"这一镜没有台词"，用户看到的是"剧本明明有对话"。

为什么不能"按场号直接把剧本台词塞进去"：
    同一个项目的剧本**被改过**。实测 caa9904f 的当前细纲是
    「铁王座前的对峙：琼恩 / 瑟曦 / 丹妮莉丝」，而分镜是**旧剧本**
    （「废墟中的铁王座：布兰 / 无面者刺客」）生成的，画面里根本没有那三个人。
    按场号硬塞 = 给画面配错台词，比静音更糟（这正是参考库里
    `F-DIALOGUE-VISUALIZED` 那类"音画不符"的失败）。

所以本模块的规矩是**证据优先**：
    只有"这句台词的说话人**确实出现在这一镜里**"（镜头文本提到 / 在镜头角色表里）
    才回填；否则**如实报告跳过**，绝不硬猜。

纯函数，不碰数据库、不碰网络：数据由 `main.py` 从 db 取好传进来。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


logger = logging.getLogger("videoforge.scriptlines")


# ──────────── 小工具 ────────────


def _as_list(v: Any) -> List[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            o = json.loads(v)
        except Exception:
            return []
        return o if isinstance(o, list) else []
    return []


def _as_dict(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            o = json.loads(v)
        except Exception:
            return {}
        return o if isinstance(o, dict) else {}
    return {}


def _norm(s: Any) -> str:
    """归一化名字：去掉姓名分隔点、空白，便于"布兰"匹配"布兰·史塔克"。"""
    return re.sub(r"[\s·・\.\-—_]+", "", str(s or ""))


def _mentions(text: str, name: str) -> bool:
    """`name` 是否出现在 `text` 里（双向包含，长度 ≥2 才认，避免单字误命中）。"""
    t, n = _norm(text), _norm(name)
    if len(n) < 2 or not t:
        return False
    return n in t or (len(t) >= 2 and t in n)


def shot_text(shot: Dict[str, Any]) -> str:
    """把一镜里所有**能证明"谁在这场戏里"**的文字拼起来。

    包含：layer1_overview + 时间轴每段的 action/expression/camera +
    layer3 的 must_appear/must_keep（角色常只写在这里）。
    """
    parts: List[str] = [str(shot.get("layer1_overview") or "")]
    tl = shot.get("layer2_timeline")
    if isinstance(tl, str):
        try:
            tl = json.loads(tl)
        except Exception:
            tl = []
    for seg in _as_list(tl):
        if not isinstance(seg, dict):
            continue
        for k in ("action", "expression", "camera", "dialogue"):
            v = seg.get(k)
            if isinstance(v, dict):
                parts.append(str(v.get("text") or ""))
                parts.append(str(v.get("character") or ""))
            else:
                parts.append(str(v or ""))
    l3 = shot.get("layer3_constraints")
    if isinstance(l3, str):
        try:
            l3 = json.loads(l3)
        except Exception:
            l3 = {}
    d3 = _as_dict(l3)
    for k in ("must_appear", "must_keep", "must_happen"):
        for v in _as_list(d3.get(k)):
            parts.append(str(v or ""))
    return "\n".join(p for p in parts if p)


# ──────────── 收集剧本里的"场 → 台词" ────────────


def collect_scene_sources(script: Optional[Dict[str, Any]],
                          scene_details: Optional[Iterable[Dict[str, Any]]] = None,
                          ) -> List[Dict[str, Any]]:
    """把项目里**所有**剧本版本里"有台词的场"收集成候选来源。

    返回每项：
      `{"source", "scene_number", "title", "location", "characters",
        "dialogues": [{"character", "text", "emotion", "timing"}]}`

    ★ 同时收 `scripts.scenes`（生成分镜时用的那版）与 `script_scene_details`
      （剧本工作台里"当前这一版"）。两版可能**不是同一稿** —— 谁匹配得上由
      `recover_shot_lines` 按镜头内容判定，这里不做取舍。
    """
    out: List[Dict[str, Any]] = []

    def _push(src: str, scene: Dict[str, Any]) -> None:
        dlg: List[Dict[str, Any]] = []
        for d in _as_list(scene.get("dialogues")) or _as_list(scene.get("dialogue")):
            if not isinstance(d, dict):
                continue
            text = str(d.get("text") or "").strip()
            if not text:
                continue
            item = {"character": str(d.get("character") or "").strip(),
                    "text": text,
                    "emotion": str(d.get("emotion") or "").strip()}
            try:
                if d.get("timing") is not None:
                    item["timing"] = float(d["timing"])
            except Exception:
                pass
            dlg.append(item)
        if not dlg:
            return
        out.append({
            "source": src,
            "scene_number": scene.get("scene_number"),
            "title": str(scene.get("title") or "").strip(),
            "location": str(scene.get("location") or "").strip(),
            "characters": [str(c) for c in _as_list(scene.get("characters"))],
            "dialogues": dlg,
        })

    sc = _as_dict(script or {})
    for s in _as_list(sc.get("scenes")):
        if isinstance(s, dict):
            _push("剧本场景", s)
    for s in _as_list(_as_dict(sc.get("outline")).get("scenes")):
        if isinstance(s, dict):
            _push("剧本大纲", s)
    for row in (scene_details or []):
        d = _as_dict((row or {}).get("detail"))
        if not d:
            continue
        if d.get("scene_number") is None:
            d["scene_number"] = (row or {}).get("scene_number")
        _push("剧本细纲", d)
    return out


# ──────────── 给一镜挑"对得上"的台词 ────────────


def recover_shot_lines(shot: Dict[str, Any],
                       sources: Optional[List[Dict[str, Any]]] = None,
                       *, scene_name: str = "",
                       character_names: Optional[Iterable[str]] = None,
                       order_index: Optional[int] = None,
                       ) -> Dict[str, Any]:
    """从剧本里给这一镜挑出**能站得住**的台词。

    判定（全部要满足，宁可少填也不错填）：
      ① 这一场里**每一句**台词的说话人，都必须"在这一镜里出现过" ——
         做法是：镜头文本（`shot_text`）提到该说话人，或该说话人在镜头的
         角色表里（`character_names`，调用方按 `character_ids` 传进来）；
      ② 与镜头的场景名对得上（`scene_name`）**加分**、场号接近也加分，
         用来在多个版本/多场里挑最像的那一场。

    返回：
      `{"lines": [...], "source": "...", "scene_title": "...", "score": n,
        "skipped": [{"character","text","reason"}]}`
    `lines` 为空 = **不硬填**，理由写在 `skipped`/`reason` 里，调用方如实上报。
    """
    text = shot_text(shot)
    known = {_norm(n) for n in (character_names or []) if _norm(n)}
    if not sources:
        return {"lines": [], "source": "", "scene_title": "", "score": 0,
                "skipped": [], "reason": "项目里没有可用的剧本台词"}

    best: Optional[Dict[str, Any]] = None
    best_score = -10 ** 9
    skipped_all: List[Dict[str, Any]] = []
    for src in sources:
        dlg = src.get("dialogues") or []
        ok_lines: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for d in dlg:
            who = str(d.get("character") or "")
            if not who:
                skipped.append({"character": "", "text": d.get("text", ""),
                                "reason": "剧本这句没写说话人"})
                continue
            if _norm(who) in known or _mentions(text, who):
                ok_lines.append(d)
            else:
                skipped.append({"character": who, "text": d.get("text", ""),
                                "reason": f"「{who}」没出现在这一镜里（画面/角色表都没有）"})
        if not ok_lines:
            skipped_all.extend(skipped)
            continue
        if skipped:
            # 同一场里有的对得上、有的对不上 → 只填对得上的，但要留痕
            pass
        score = len(ok_lines) * 10 - len(skipped) * 2
        loc = str(src.get("location") or "")
        if scene_name and loc and (_mentions(loc, scene_name) or _mentions(scene_name, loc)):
            score += 8
        try:
            sn = int(src.get("scene_number"))
            if order_index is not None:
                score += max(0, 4 - abs(sn - (int(order_index) + 1)))
        except Exception:
            pass
        if score > best_score:
            best_score, best = score, {"lines": ok_lines,
                                       "source": src.get("source") or "",
                                       "scene_title": src.get("title") or "",
                                       "score": score,
                                       "skipped": skipped}
    if best is None:
        return {"lines": [], "source": "", "scene_title": "", "score": 0,
                "skipped": skipped_all[:8],
                "reason": "剧本里没有一句台词的说话人出现在这一镜里（不硬塞）"}
    return best


def inject_lines(timeline: Any, lines: List[Dict[str, Any]],
                 skip_existing: bool = True) -> Dict[str, Any]:
    """把台词填进时间轴**还没有台词的那些段**（不改已有台词）。

    · 剧本写了 `timing`（秒）→ 放进覆盖那个时刻的段；
    · 没写 → 按顺序填进空段（不够则只在空段里循环，不覆盖）。

    ★★ `skip_existing=True`（默认）会**先滤掉时间轴里已经有的台词**：
       · 同一句文本已经在 → 跳过（否则会**重复念一遍**）；
       · 同一个说话人已经在 → 跳过（他的那句已经在，要补的是**别人**那句）。
      为什么必须这样（2026-09-14 干跑实测）：分镜 `[0-4]` 有珊莎的台词、
      `[4-8]` 是空的、`[8-12]` 有领主A 的台词 —— 旧逻辑看到"有一个空段"就把
      剧本的第一句（还是珊莎那句）**又填了一次**，于是这一镜把同一句话念了两遍，
      而本该补的位置反而空着。有台词的时候，回填只该做"补缺"，不该做"重念"。

    返回 `{"timeline": [...], "added": n, "placed": [...], "skipped": [...]}`。
    """
    tl = _as_list(timeline)
    if not tl:
        return {"timeline": tl, "added": 0, "placed": [], "skipped": []}
    out = [dict(seg) if isinstance(seg, dict) else {} for seg in tl]
    empty = [i for i, s in enumerate(out) if not (s.get("dialogue") or {})]

    have_texts: set = set()
    have_who: set = set()
    for s in out:
        d = s.get("dialogue") or {}
        if d.get("text"):
            have_texts.add(_norm(d.get("text")))
        if d.get("character"):
            have_who.add(_norm(d.get("character")))

    added, placed, skipped = 0, [], []
    used: set = set()
    for ln in lines:
        _txt, _who = _norm(ln.get("text")), _norm(ln.get("character"))
        if skip_existing and _txt and _txt in have_texts:
            skipped.append({"character": ln.get("character"), "text": ln.get("text"),
                            "reason": "这一镜里已经有这句台词了（不重复念）"})
            continue
        if skip_existing and _who and _who in have_who:
            skipped.append({"character": ln.get("character"), "text": ln.get("text"),
                            "reason": f"「{ln.get('character')}」这一镜已经在说话了"
                                      f"（要补的是没开口的那个人）"})
            continue
        idx = None
        try:
            if ln.get("timing") is not None:
                t = float(ln["timing"])
                for i in empty:
                    if i in used:
                        continue
                    st = float(out[i].get("start") or 0)
                    en = float(out[i].get("end") or 0)
                    if st <= t < en or (t < st and i == empty[0]):
                        idx = i
                        break
        except Exception:
            idx = None
        if idx is None:
            for i in empty:
                if i not in used:
                    idx = i
                    break
        if idx is None:
            skipped.append({"character": ln.get("character"), "text": ln.get("text"),
                            "reason": "这一镜没有空的时间段可放了"})
            break
        seg = dict(out[idx])
        seg["dialogue"] = {"character": ln.get("character") or "",
                           "text": ln.get("text") or "",
                           "emotion": ln.get("emotion") or "",
                           "recovered_from_script": True}
        out[idx] = seg
        used.add(idx)
        placed.append({"segment": idx, "character": seg["dialogue"]["character"],
                       "text": seg["dialogue"]["text"]})
        added += 1
        if _txt:
            have_texts.add(_txt)
        if _who:
            have_who.add(_who)
    return {"timeline": out, "added": added, "placed": placed, "skipped": skipped}


def recover_and_inject(shot: Dict[str, Any],
                       sources: Optional[List[Dict[str, Any]]] = None,
                       *, scene_name: str = "",
                       character_names: Optional[Iterable[str]] = None,
                       order_index: Optional[int] = None,
                       ) -> Dict[str, Any]:
    """一步到位：挑台词 → 填进时间轴。返回 `inject_lines` 的结果 + 来源说明。

    `skipped` 里同时包含两类拒绝原因，都要如实上报：
      · 说话人不在这一镜里（选台词阶段拒绝，不硬塞）；
      · 这句话/这个说话人已经在时间轴里了（注入阶段拒绝，不重复念）。
    """
    r = recover_shot_lines(shot, sources, scene_name=scene_name,
                           character_names=character_names,
                           order_index=order_index)
    inj = inject_lines(shot.get("layer2_timeline"), r.get("lines") or [])
    _sk = [{"character": s.get("character"), "text": s.get("text"),
            "reason": f"说话人不在这一镜里：{s.get('reason')}"}
           for s in (r.get("skipped") or [])]
    _sk += list(inj.get("skipped") or [])
    inj.update({"source": r.get("source") or "",
                "scene_title": r.get("scene_title") or "",
                "candidates": len(r.get("lines") or []),
                "skipped": _sk,
                "reason": r.get("reason") or ""})
    return inj
