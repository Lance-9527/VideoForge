# -*- coding: utf-8 -*-
"""VideoForge · 生成条件一致性（Consistency）

═══════════════════════════════════════════════════════════════════
为什么这是"拼起来一眼AI"的第一现场
═══════════════════════════════════════════════════════════════════
用眼睛看线上真实素材（`tests/_visual/seams.png`）得到的事实：

    第 1 镜：3D 卡通风（蓝色马甲、圆眼镜、雕塑门框）——明显是 stylized
    第 2 镜：写实真人（两个穿西装/僧袍的男人，宫殿前）
    第 3 镜：暗红砖拱门（低照度、暖调）
    第 4 镜：冷调古典街道（青蓝）

**它们不是同一个世界。** 查库得到原因（`tests/audit_shot_consistency.py`）：

    项目 19636f68 的 6 个分镜用了 **3 种不同模型**：
      hailuo/video-01 × 2 · hailuo/MiniMax-Hailuo-02 × 2 · hailuo/kling-1.6 × 2

不同模型 = 不同的画风 / 色彩科学 / 运动风格 / 构图习惯。
**任何后期手段都补不回来** —— 这就像把两部不同电影的镜头剪在一起。
9 个项目里 6 个存在这种不一致。

═══════════════════════════════════════════════════════════════════
本模块做什么
═══════════════════════════════════════════════════════════════════
把"整部片子应该长什么样"变成**项目级的一份声明**（canonical），
然后：
  1. `diff_shots()`   —— 列出哪些分镜偏离了这份声明（看得见）
  2. `plan_unify()`   —— 给出把这些分镜拉回一致的**具体改法**
  3. `severity()`     —— 给偏离分级：哪些必须改（模型/画幅）、哪些建议改（分辨率）

刻意**不做**的事：
  · 不自动改写用户的分镜。改模型/分辨率会改变出片效果，必须用户点头。
  · 不把"跨场景"当问题。一部片子本来就该有多个场景 ——
    要统一的是**生成条件**（谁来画、画多大），不是**内容**（画什么）。
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.consistency")

# 偏离的严重程度：
#   "fatal"   —— 不改就一定会"两个世界"：模型变了（画风/色彩科学不同）
#   "major"   —— 一定会跳：画幅/宽高比变了（拼接要裁，构图也变了）
#   "minor"   —— 建议改：分辨率变了（清晰度不一致，但画风还是同一个）
LEVEL_ORDER = {"ok": 0, "minor": 1, "major": 2, "fatal": 3}
LEVEL_ZH = {"ok": "一致", "minor": "建议统一", "major": "必须统一", "fatal": "必须统一"}


def canonical_from_shots(shots: List[Dict[str, Any]], settings: Optional[dict] = None) -> Dict[str, Any]:
    """从现有分镜里推一份"主流生成条件"当基准。

    用**多数派**而不是第一个：用户可能先试了一个模型、后来统一换成另一个，
    取第一个会把已经被淘汰的旧值当标准。
    平票时优先取"有视频产物的那些分镜"用的条件 —— 那些是真正跑通过、
    用户看过的，比没出过片的设置更可信。
    """
    if not shots:
        s = settings or {}
        return {
            "model_provider": s.get("default_model_provider") or "",
            "model_name": s.get("default_model_name") or "",
            "resolution": s.get("default_resolution") or "",
            "aspect_ratio": s.get("default_aspect_ratio") or "",
        }

    def pick(field: str) -> str:
        vals = [str(x.get(field) or "") for x in shots]
        vals = [v for v in vals if v]
        if not vals:
            return ""
        counts = Counter(vals)
        top = max(counts.values())
        tied = [v for v, c in counts.items() if c == top]
        if len(tied) == 1:
            return tied[0]
        # 平票：优先"有视频产物"的那些分镜用过的值
        withvid = [str(x.get(field) or "") for x in shots
                   if x.get("candidates") and str(x.get("candidates")).strip()
                   not in ("", "[]", "null", "None")]
        for t in tied:
            if withvid.count(t) == max(withvid.count(x) for x in tied):
                return t
        return sorted(tied)[0]

    return {
        "model_provider": pick("model_provider"),
        "model_name": pick("model_name"),
        "resolution": pick("resolution"),
        "aspect_ratio": pick("aspect_ratio"),
    }


def severity(shot: Dict[str, Any], canon: Dict[str, Any]) -> Tuple[str, List[str]]:
    """这个分镜偏离基准到什么程度，以及为什么。

    ★ 分级依据是"后期还能不能救"：
      · 模型不同 → **救不了**。不同模型的画风/色彩科学/运动风格是模型权重决定的，
        调色和转场只能让接缝不那么刺眼，不可能让它变成"同一个世界"。
      · 画幅不同 → 能救但代价大（要裁掉一多半画面），所以算 major。
      · 分辨率不同 → 能救（缩放到同尺寸），只是清晰度不齐，算 minor。
    """
    why: List[str] = []
    lvl = "ok"
    prov = str(shot.get("model_provider") or "")
    name = str(shot.get("model_name") or "")
    if canon.get("model_provider") and prov and prov != canon["model_provider"]:
        why.append(f"厂商不同（{prov} ≠ {canon['model_provider']}）")
        lvl = "fatal"
    if canon.get("model_name") and name and name != canon["model_name"]:
        why.append(f"模型不同（{name} ≠ {canon['model_name']}）")
        lvl = "fatal"
    ar = str(shot.get("aspect_ratio") or "")
    if canon.get("aspect_ratio") and ar and ar != canon["aspect_ratio"]:
        why.append(f"画幅不同（{ar} ≠ {canon['aspect_ratio']}）")
        lvl = max(lvl, "major", key=lambda x: LEVEL_ORDER[x])
    res = str(shot.get("resolution") or "")
    if canon.get("resolution") and res and res != canon["resolution"]:
        why.append(f"分辨率不同（{res} ≠ {canon['resolution']}）")
        lvl = max(lvl, "minor", key=lambda x: LEVEL_ORDER[x])
    return lvl, why


def diff_shots(shots: List[Dict[str, Any]],
               canon: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """项目内一致性体检：返回基准、偏离清单、汇总。

    这是 `tests/audit_shot_consistency.py` 的产品化版本 ——
    审计脚本只能事后看，这个要在**生成之前**就拦住。
    """
    canon = canon or canonical_from_shots(shots)
    items: List[Dict[str, Any]] = []
    worst = "ok"
    by_level: Dict[str, int] = Counter()
    for i, s in enumerate(sorted(shots, key=lambda x: x.get("order_index") or 0)):
        lvl, why = severity(s, canon)
        by_level[lvl] += 1
        if LEVEL_ORDER[lvl] > LEVEL_ORDER[worst]:
            worst = lvl
        if lvl != "ok":
            items.append({
                "shot_id": s.get("id"),
                "index": (s.get("order_index") or 0) + 1,
                "level": lvl,
                "reasons": why,
                "current": {
                    "model_provider": s.get("model_provider") or "",
                    "model_name": s.get("model_name") or "",
                    "resolution": s.get("resolution") or "",
                    "aspect_ratio": s.get("aspect_ratio") or "",
                },
                "has_video": bool(s.get("candidates") and str(s.get("candidates")).strip()
                                  not in ("", "[]", "null", "None")),
            })
    # 有视频产物的偏离最要紧：那些片段已经在成片里了，不重生成就永远不一致
    items.sort(key=lambda x: (-LEVEL_ORDER[x["level"]], not x["has_video"], x["index"]))
    return {
        "canonical": canon,
        "worst_level": worst,
        "counts": {k: by_level.get(k, 0) for k in ("ok", "minor", "major", "fatal")},
        "consistent": worst == "ok",
        "deviations": items,
        "total_shots": len(shots),
    }


def plan_unify(shots: List[Dict[str, Any]],
               canon: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给出"把项目拉回一致"的具体改法与代价 —— **不自动执行**。

    返回的 `actions` 每一项都能直接喂给 `PATCH /api/shots/{id}`。
    同时如实说明代价：已经出过片的分镜改了条件就要**重新生成**，
    否则成片里那一段还是旧画风。
    """
    canon = canon or canonical_from_shots(shots)
    rep = diff_shots(shots, canon)
    actions: List[Dict[str, Any]] = []
    need_regen = 0
    for d in rep["deviations"]:
        patch: Dict[str, Any] = {}
        cur = d["current"]
        for k in ("model_provider", "model_name", "resolution", "aspect_ratio"):
            if canon.get(k) and cur.get(k) and cur[k] != canon[k]:
                patch[k] = canon[k]
        if not patch:
            continue
        actions.append({
            "shot_id": d["shot_id"], "index": d["index"],
            "level": d["level"], "patch": patch,
            "reasons": d["reasons"],
            "must_regenerate": bool(d["has_video"]),
        })
        if d["has_video"]:
            need_regen += 1

    warns: List[str] = []
    fatal = [d for d in rep["deviations"] if d["level"] == "fatal"]
    if fatal:
        warns.append(
            f"有 {len(fatal)} 个分镜用了**和项目其他镜头不同的模型**。"
            f"不同模型等于不同的画风与色彩科学 —— 拼在一起会明显像两个片子，"
            f"**后期调色和转场补不回来**。建议统一成「{canon.get('model_name')}」。")
    if need_regen:
        warns.append(
            f"其中 {need_regen} 个分镜**已经生成过视频**：改了生成条件必须重新生成，"
            f"否则保留的还是旧画风那一段。")
    maj = [d for d in rep["deviations"] if d["level"] == "major"]
    if maj:
        warns.append(
            f"有 {len(maj)} 个分镜的画幅和项目其他镜头不同（"
            f"{'、'.join(sorted({d['current']['aspect_ratio'] for d in maj}))}）。"
            f"拼接时必须裁掉多余部分，构图会变 —— 建议按 {canon.get('aspect_ratio')} 重新生成。")
    if not warns:
        warns.append("所有分镜的生成条件一致，不会出现「两个世界」的接缝。")

    return {
        "canonical": canon, "actions": actions,
        "action_count": len(actions), "need_regenerate": need_regen,
        "warnings": warns, "report": rep,
    }


def consistency_brief(shots: List[Dict[str, Any]]) -> str:
    """一句话总结，给界面/日志用。"""
    rep = diff_shots(shots)
    c = rep["canonical"]
    if rep["consistent"]:
        return (f"生成条件一致（{c.get('model_name') or '默认模型'} · "
                f"{c.get('resolution') or '默认分辨率'} · "
                f"{c.get('aspect_ratio') or '默认画幅'}）")
    n = rep["counts"]
    return (f"生成条件**不一致**：{n['fatal']} 个换了模型、{n['major']} 个画幅不同、"
            f"{n['minor']} 个分辨率不同 —— 拼接会出现「两个世界」的接缝")


def default_shot_conditions(settings: Dict[str, Any],
                            project_shots: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """新分镜默认该用什么生成条件。

    ★ 第一优先级是**项目内已有镜头的主流条件**，而不是全局设置：
      否则用户改了全局默认模型之后，新加的分镜会用新模型，
      和项目里已有的镜头立刻变成"两个世界"。
    """
    if project_shots:
        canon = canonical_from_shots(project_shots)
        if canon.get("model_provider") or canon.get("model_name"):
            return canon
    s = settings or {}
    return {
        "model_provider": s.get("default_model_provider") or "",
        "model_name": s.get("default_model_name") or "",
        "resolution": s.get("default_resolution") or "1080P",
        "aspect_ratio": s.get("default_aspect_ratio") or "16:9",
    }
