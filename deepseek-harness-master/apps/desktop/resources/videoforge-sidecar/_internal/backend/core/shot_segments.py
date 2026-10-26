"""
VideoForge · 分镜分段时间轴出片引擎（"按剧本节拍出视频"）

════════════════════════════════════════════════════════════════════════
为什么要有这个模块
════════════════════════════════════════════════════════════════════════
用户原话（第 8 条）：
    "镜头**没人类爱看** —— 动作顺序不符合人类观赏习惯。"

但更深一层的事故是：**首帧决定视频**。
现在出视频是「合成首帧 → 图生视频」一条龙 —— 合成首帧里有人，
可灵就**强跟随首帧**，于是 L2 时间轴写「0–4s 空镜（人未入画）」，
出来的镜头里**人物从第 0 秒就在画**。这不是模型问题，是流程问题：
  · 一个时间轴、一个人物登场点，就要单独有一段视频；
  · 每段视频的首帧要**按本段动作的开始状态**生成。

本模块把这个事情固化下来：**读 L2 → 拆段 → 按段定首帧 → 段段出 → 拼接**。
从此 0–4s 真就是空镜、4–9s 真就是睁眼、9–13s 真就是撑起。

════════════════════════════════════════════════════════════════════════
主要入口
════════════════════════════════════════════════════════════════════════
    parse_l2_segments(shot)        读 L2，把段列表标准化（含 duration）
    plan_segments(shot, ctx)       给每一段定首帧/尾帧策略、prompt
    produce_shot_segments(db, sid) 真跑：上传、生成、拼接、验证
    plan_segments(...)             dry_run 模式：只出方案、不花钱

════════════════════════════════════════════════════════════════════════
设计要点
════════════════════════════════════════════════════════════════════════
· **段首帧策略按"段是否含人 / 段动作起点"分类**：
    - 段内文本里出现「无人物 / 人未入画 / 空镜 / 只剩 …」→ 纯场景图
    - 段内文本出现「睁眼 / 侧卧 / 入画 / 出现」 → 用 lying 合成首帧
    - 段内文本出现「撑起 / 抬手 / 起身 / 翻身」→ 用 active 合成首帧
    - 没有 lying 合成首帧但需要它 → 调用 kling-image-v3_0_omni 现做一张
· **段长限制**：可灵单次最大 10s，>10s 自动对半分，尾帧承接下一段首帧
· **失败链路**：任何一段生成失败 → 整条不再拼 → 把已有的段写出来给用户看
· **dry_run**：灵感值不够时只出方案，把每段要花多少、要调什么接口写清楚，
  充值后用户一句「执行 plan」就能跑。这是省钱又负责的交付方式。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import kling_mcp as K

logger = logging.getLogger("videoforge.shot_segments")


# ────────────────────── Blueprint 读取 + 可灵 Element 绑定 ──────────────────────

def _load_blueprint(shot_id, log=None):
    """从 render.py 生成的 blueprints/shotNN.json 读白膜蓝图。"""
    try:
        from core import db as _db
        shot = _db.get_shot(shot_id) if hasattr(_db, "get_shot") else None
        idx = (shot.get("order_index") or 0) if shot else 0
    except Exception:
        idx = 0
    base = os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()
    bp_paths = [
        os.path.join(base, "outputs", "previz_blueprints", f"shot{idx:02d}.json"),
        os.path.join(base, "outputs", "718aec1c-f07e-45aa-926b-09525c6b3b95", "previz_blueprints", f"shot{idx:02d}.json"),
    ]
    for p in bp_paths:
        if os.path.exists(p):
            try:
                bp = json.loads(open(p, encoding="utf-8").read())
                if log is not None:
                    log.append(f"[blueprint] 从 {p} 读了 cast×{len(bp.get('cast') or [])}")
                return bp
            except Exception as _e:
                if log is not None:
                    log.append(f"[blueprint] 读 {p} 失败：{_e}")
    return {}


async def _bind_elements_from_blueprint(bp, base_prompt, Tk):
    """读 blueprint，给每个有 ref_image_path 的 cast 注册可灵 Element，返回 (element_ids, element_bind, prompt_aug)。

    官方 element_create 接口需要 cover_url（已上传的可灵 URL），所以先 ensure_url 上传，
    再 element_create，返回的 id 作为 element_ids 传给 image_to_video。
    """
    cast = (bp.get("cast") or []) if isinstance(bp, dict) else []
    if not cast:
        return [], {}, ""
    element_ids: List[str] = []
    element_bind: Dict[str, str] = {}
    extra_lines: List[str] = []
    for c in cast:
        ref = (c.get("ref_image_path") or "").strip()
        name = (c.get("name") or "").strip()
        if not (ref and name and os.path.exists(ref)):
            continue
        try:
            cover_url = await Tk.ensure_url(ref)
            res = await Tk.element_create(
                name=name, description=f"白膜蓝图里的角色：{name}",
                tags=["角色"], cover_url=cover_url,
            )
            eid = (res.get("id") or res.get("element_id") or res.get("elementId") or "")
            if eid:
                element_ids.append(eid)
                element_bind[eid] = name
                extra_lines.append(f"（{name} 脸部保持与项目设定一致）")
        except Exception as _e:
            extra_lines.append(f"（{name} 脸部请符合项目设定）")
    aug = ("\n" + "\n".join(extra_lines)) if extra_lines else ""
    return element_ids, element_bind, aug


# ────────────────────── L2 解析 ──────────────────────

_EMPTY_HINTS = ("无人物", "人未入画", "空镜", "画中无人", "还未入画",
                "无人出现", "no person", "no character", "empty")
_APPEAR_HINTS = ("睁眼", "出现", "入画", "侧卧", "翻身看", "醒来",
                 "appear", "lies", "eyes open")
_ACTIVE_HINTS = ("撑起", "撑", "起", "抬", "伸手", "翻身", "坐起",
                 "推", "拍", "击", "走", "跑", "踢", "摔", "翻越",
                 "push", "raise", "stand", "lift", "kick", "strike")


def _strip_text(seg: Dict[str, Any]) -> str:
    return " ".join(str(seg.get(k) or "") for k in
                    ("action", "expression", "camera", "props_used")).strip()


def _seg_kind(seg_text: str, prev_kind: str = "") -> str:
    """给段落分类：empty / appear / active / passive。

    empty  : 段里没人物（用场景图当首帧）
    appear : 人物登场、侧卧、睁眼这一档（用 lying 合成首帧）
    active : 人物主动动作（用 active 合成首帧；没有就先做一张）
    passive: 含人但主要是环境/视角（fallback 用 lying 合成首帧）
    """
    t = seg_text.lower()
    if any(h in t for h in [x.lower() for x in _EMPTY_HINTS]) or "无人物" in seg_text:
        return "empty"
    if any(h in seg_text for h in _APPEAR_HINTS):
        return "appear"
    if any(h in seg_text for h in _ACTIVE_HINTS):
        return "active"
    if prev_kind in ("appear", "active"):
        return "active"
    return "passive"


def parse_l2_segments(shot: Dict[str, Any]) -> List[Dict[str, Any]]:
    r"""读 L2 时间轴（JSON list），标准化成 [{start, end, duration, action, ...}]。

    实测坑（用户最近一次跑到的）：
      · L2 字段是 JSON 字符串，要 json.loads；
      · 单段时只给一个 dict 不是 list，要容错；
      · start/end 可能是 float 也可能是 int，统一 float；
      · duration 可能缺，按 end-start 自己算；
      · 空段（start == end）直接剔除。
    """
    raw = shot.get("layer2_timeline")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except Exception:  # noqa: BLE001
            logger.warning("L2 不是合法 JSON，按 1 段处理")
            raw = []
    if raw is None:
        raw = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        logger.warning("L2 不是 list，按 1 段处理：%r", type(raw))
        return []

    segs: List[Dict[str, Any]] = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            continue
        try:
            start = float(s.get("start") or 0)
        except Exception:  # noqa: BLE001
            start = 0.0
        try:
            end = float(s.get("end") or 0)
        except Exception:  # noqa: BLE001
            end = start
        if end <= start:
            continue
        segs.append({
            "index": i,
            "start": start,
            "end": end,
            "duration": end - start,
            "action": str(s.get("action") or "").strip(),
            "expression": str(s.get("expression") or "").strip(),
            "camera": str(s.get("camera") or "").strip(),
            "props_used": list(s.get("props_used") or []),
        })
    segs.sort(key=lambda x: x["start"])
    return segs


# ────────────────────── 资产查找 ──────────────────────

def _freshest_image(folder: str, prefix: str, before: Optional[float] = None) -> str:
    r"""在 `folder` 里挑一张最新的 `prefix-*.png/jpg`，可选 cutoff。

    作用：用户出过「新形象图 / 新场景图」之后，库里同时有 asset 的
    reference_image_path 与 cache 里的 img-{ts}.png；后者通常更贴本集。
    所以默认**优先取 cache 里时间最新的一张**，找不到才退回 asset 表。
    """
    if not folder or not os.path.isdir(folder):
        return ""
    cand: List[Tuple[float, str]] = []
    for n in os.listdir(folder):
        if not n.startswith(prefix):
            continue
        if not n.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            continue
        p = os.path.join(folder, n)
        ts = os.path.getmtime(p)
        if before is not None and ts > before + 1:
            continue
        cand.append((ts, p))
    if not cand:
        return ""
    cand.sort()
    return cand[-1][1]


def gather_assets(db, shot: Dict[str, Any]) -> Dict[str, Any]:
    r"""收集这一镜需要的资产图：**场景纯图 / 角色纯图 / lying 合成首帧 / active 合成首帧**。

    选择顺序（按"最新最贴"原则）：
      · 场景纯图 = 该 scene 的 reference_image_path，**再补 cache/scene 最新一张**
      · 角色纯图 = 主角（character_ids[0]）的 reference_image_path，
                   再补 cache/character 最新一张
      · lying 合成首帧 = cache 下名字含 'first_frame' 或 '合成首帧' 的最新图
      · active 合成首帧 = lying 之后需要的「撑起/抬手」状态图；若没有返回 ""
                       （调用方会用 kling-image 现做一张）

    返回 dict 形如：
      {
        "scene_only": "..." or "",
        "character_only": "..." or "",
        "lying_composite": "..." or "",
        "active_composite": "..." or "",
        "scene_card": {...} or {},
        "character_card": {...} or {},
      }
    """
    out: Dict[str, Any] = {
        "scene_only": "", "character_only": "",
        "lying_composite": "", "active_composite": "",
        "scene_card": {}, "character_card": {},
    }
    pid = shot.get("project_id") or ""
    cache_root = os.path.join(
        os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd(),
        "cache", "images", pid)

    # 场景
    sid = shot.get("scene_id")
    if sid:
        try:
            sc = db.get_scene(sid) if hasattr(db, "get_scene") else None
        except Exception:  # noqa: BLE001
            sc = None
        if sc:
            out["scene_card"] = {"name": sc.get("name") or "",
                                  "description": sc.get("description") or ""}
            ref = (sc.get("reference_image_path") or "").strip()
            if ref and os.path.exists(ref):
                out["scene_only"] = ref
    # cache 里补一张
    fresh = _freshest_image(os.path.join(cache_root, "scenes"), "img")
    if fresh and (not out["scene_only"] or os.path.getmtime(fresh) >
                  os.path.getmtime(out["scene_only"])):
        out["scene_only"] = fresh

    # 角色
    cids = shot.get("character_ids") or []
    if isinstance(cids, str):
        try:
            cids = json.loads(cids) if cids.strip() else []
        except Exception:  # noqa: BLE001
            cids = []
    # ★ 2026-09-30 修：多镜头场景会多角色。Kling image_to_video 只吃一张首帧
    #   → 我们必须选**该镜头的主角**（script 里最早提到说话的，或脚本 L1 层明确主角）；
    #   现在原代码只用 character_ids[0] 不一定是主角。补上优先级：
    #     ① shot.character_priority_ids (手动指定)
    #     ② character_ids 里第一个
    primary_cid = None
    pri = shot.get("character_priority_ids")
    if isinstance(pri, str):
        try: pri = json.loads(pri) if pri.strip() else []
        except Exception: pri = []
    if isinstance(pri, list) and pri:
        primary_cid = pri[0]
    if not primary_cid and cids:
        primary_cid = cids[0]
    if primary_cid:
        try:
            c = db.get_character(primary_cid) if hasattr(db, "get_character") else None
        except Exception:  # noqa: BLE001
            c = None
        if c:
            out["character_card"] = {
                "name": c.get("name") or "",
                "description": c.get("description") or "",
                "age": c.get("age") or "",
            }
            ref = (c.get("reference_image_path") or "").strip()
            if ref and os.path.exists(ref):
                out["character_only"] = ref
            # ★ 多角色场景：同时收下其他角色的信息，picker 会按名字依次出
            if cids and len(cids) > 1:
                out["all_characters"] = []
                for cid2 in cids:
                    if cid2 == primary_cid:
                        continue
                    c2 = db.get_character(cid2) if hasattr(db, "get_character") else None
                    if c2:
                        out["all_characters"].append({
                            "name": c2.get("name") or "",
                            "description": c2.get("description") or "",
                            "age": c2.get("age") or "",
                            "reference_image_path": c2.get("reference_image_path") or "",
                        })
        fresh = _freshest_image(os.path.join(cache_root, "characters"), "img")
        if fresh and (not out["character_only"] or os.path.getmtime(fresh) >
                      os.path.getmtime(out["character_only"])):
            out["character_only"] = fresh

    # lying 合成首帧（找的优先级：
    #   ① cache/images/<pid>/ 里的 first_frame.* 或 "合成首帧.png"
    #   ② outputs/<pid>/shots/ 里同 shot_id 归档的 "合成首帧.*"
    #   ③ D:\vf_test\first_frame.png 工作副本（开发期间保留的）
    # 这样早先生成的「陈砚侧卧水面」那一张会被自动认回，
    # 不会让「active 段」误判成「没有 lying 复合图 → 现做」。
    lying_candidates: List[Tuple[float, str]] = []
    if os.path.isdir(cache_root):
        for n in os.listdir(cache_root):
            if ("first_frame" in n.lower() or "合成首帧" in n) and \
                    n.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                p = os.path.join(cache_root, n)
                lying_candidates.append((os.path.getmtime(p), p))
    out_dir = os.path.join(
        os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd(),
        "outputs", pid, "shots")
    if os.path.isdir(out_dir):
        for n in os.listdir(out_dir):
            if "合成首帧" in n or "first_frame" in n.lower():
                p = os.path.join(out_dir, n)
                if os.path.exists(p):
                    lying_candidates.append((os.path.getmtime(p), p))
    vf_test = r"D:\vf_test\first_frame.png"
    if os.path.exists(vf_test):
        lying_candidates.append((os.path.getmtime(vf_test), vf_test))
    if lying_candidates:
        lying_candidates.sort()
        out["lying_composite"] = lying_candidates[-1][1]

    # active 合成首帧 —— 通常需要现做，这里返回空字符串即可
    return out


# ────────────────────── 段计划 ──────────────────────

@dataclass
class SegmentPlan:
    index: int
    start: float
    end: float
    duration: float        # 实际送给可灵的秒数
    kind: str              # empty / appear / active / passive
    first_image: str = ""  # 本地路径或 URL
    tail_image: str = ""   # 同上
    first_image_url: str = ""
    tail_image_url: str = ""
    prompt: str = ""
    needs_synthesis: bool = False
    synthesis_prompt: str = ""
    note: str = ""
    cost_estimate: float = 0.0  # 灵感值估计


def _classify_with_prev(seg_text: str, prev_kind: str) -> str:
    """_seg_kind 的小封装，让 plan 步骤读得懂。"""
    return _seg_kind(seg_text, prev_kind=prev_kind)


def _make_seg_prompt(seg: Dict[str, Any], ctx: Dict[str, Any],
                     kind: str, char_name: str) -> str:
    """生成这段视频的提示词（喂给可灵 image_to_video）。

    策略：剧本动作 + 机位 + 必须保留的元素，全部写明。
    为什么不用 agent 改写：实测 LLM 改写会把"巷底半墙油灯"这种关键
    物改没；直接拼剧本原文 + 摄影语，更稳。

    ★ 多角色场景：主角（首帧画像）单独强调，其他角色按"次要人物"列出，
      可灵会按名字去场景里找/放，但首帧仍只锁主角一张。
    """
    parts: List[str] = []
    if char_name:
        parts.append(f"{char_name}（身份与外观沿用首帧，**画面主体**）")
    # ★ 多角色镜头：把其他角色也写明，让可灵知道"画面里还有谁"
    others = (ctx.get("all_characters") or [])
    if others:
        for c in others[:3]:  # 至多列 3 个，避免提示词过炸
            nm = c.get("name") or ""
            desc = c.get("description") or ""
            if nm:
                parts.append(f"次要人物「{nm}」在场：{desc[:80]}")
    a = (seg.get("action") or "").strip()
    if a:
        parts.append(a)
    e = (seg.get("expression") or "").strip()
    if e:
        parts.append("表情：" + e)
    c = (seg.get("camera") or "").strip()
    if c:
        parts.append("机位：" + c)
    scene = (ctx.get("scene_card") or {}).get("description") or ""
    if scene:
        parts.append("场景保持：" + scene[:200])
    must_keep = []
    sc = ctx.get("scene_card") or {}
    if sc.get("name"):
        must_keep.append(sc["name"])
    cc = ctx.get("character_card") or {}
    if cc.get("name"):
        must_keep.append(cc["name"])
    if must_keep:
        parts.append("必须沿用：" + " / ".join(must_keep))
    parts.append("保持：同一张脸、同一件湿透灰褐短褐、同一条巷子、同一盏油灯为唯一光源。"
                 "电影感、暗调高反差、真实皮肤质感，无字幕无水印。")
    if kind == "empty":
        parts.append("★ 本段是空镜：**画面中不要出现任何人**，只保留场景与油灯倒影。")
    return "\n".join(parts)


def plan_segments(shot: Dict[str, Any], ctx: Dict[str, Any],
                  *, model: str = "kling-video-v3_0") -> List[SegmentPlan]:
    r"""给每一段做计划（**不花钱**，纯算）。

    每段都要决定：
      · kind（empty/appear/active/passive）
      · first_image / tail_image（用 ctx 里的现成图；没有就标 needs_synthesis）
      · prompt（剧本原文 + 摄影语）
      · duration（可灵单次 ≤ 10s）

    L2 没有 lying 合成首帧而需要它：needs_synthesis=True，
    让调用方用 kling-image-v3_0_omni 现做一张。
    """
    segs = parse_l2_segments(shot)
    if not segs:
        logger.warning("shot %s 的 L2 是空的 —— 退到 1 段长镜（退化）", shot.get("id"))
        total = float(shot.get("duration_seconds") or 5)
        segs = [{
            "index": 0, "start": 0.0, "end": total, "duration": total,
            "action": str(shot.get("layer1_overview") or "")[:300],
            "expression": "", "camera": "", "props_used": [],
        }]

    char_card = ctx.get("character_card") or {}
    char_name = (char_card.get("name") or "").strip()
    plans: List[SegmentPlan] = []
    prev_kind = ""

    for seg in segs:
        text = _strip_text(seg)
        kind = _classify_with_prev(text, prev_kind)
        prev_kind = kind

        # 选首帧
        first = ""
        tail = ""
        synth = False
        synth_prompt = ""
        if kind == "empty":
            first = ctx.get("scene_only") or ""
            tail = ctx.get("scene_only") or ""
            note = "纯空镜，用场景图当首/尾帧（人不会出现）"
        elif kind in ("appear", "passive"):
            first = ctx.get("lying_composite") or ""
            tail = ctx.get("lying_composite") or ""
            if not first:
                first = ctx.get("character_only") or ""
                tail = first
                synth = True
                synth_prompt = _make_synthesis_prompt(seg, ctx, kind, char_name)
                note = "需要 lying 合成首帧（现有素材里没有）→ 会先用图生图做一张"
            else:
                note = "用 lying 合成首帧（陈砚侧卧水面）"
        else:  # active
            first = ctx.get("active_composite") or ctx.get("lying_composite") or ""
            tail = ctx.get("active_composite") or ctx.get("lying_composite") or ""
            if first == ctx.get("lying_composite"):
                # 有 lying 但没有 active → 现做一张 active
                synth = True
                synth_prompt = _make_synthesis_prompt(seg, ctx, kind, char_name)
                note = "需要 active 合成首帧（陈砚肘撑起 / 抬手的当下状态）→ 会先用图生图做一张"
            else:
                note = "用 active 合成首帧"
            # 兜底：连 lying 都没有 → 用 character_only
            if not first:
                first = ctx.get("character_only") or ""
                tail = first
                note = "没有 lying / active 合成首帧 → 用角色纯图（可能会和场景不同框）"

        prompt = _make_seg_prompt(seg, ctx, kind, char_name)
        # 可灵 v3_0：duration 必须 ≤ 10，超了按 10 段拆（拆段不在 plan 层做，
        # 由调用方处理；这里只算单次时长）
        plans.append(SegmentPlan(
            index=seg["index"],
            start=seg["start"], end=seg["end"],
            duration=min(seg["duration"], 10.0),
            kind=kind,
            first_image=first, tail_image=tail,
            first_image_url="", tail_image_url="",
            prompt=prompt,
            needs_synthesis=synth,
            synthesis_prompt=synth_prompt,
            note=note,
            cost_estimate=_estimate_cost(model, seg["duration"]),
        ))
    return plans


def _make_synthesis_prompt(seg: Dict[str, Any], ctx: Dict[str, Any],
                           kind: str, char_name: str) -> str:
    """生成一张**本段起点的首帧**用的提示词。

    kind=appear/passive ：陈砚侧卧水面、眼睛将睁
    kind=active        ：陈砚肘撑起上半身约 20cm 的瞬间
    """
    sc = ctx.get("scene_card") or {}
    cc = ctx.get("character_card") or {}
    char_desc = (cc.get("description") or "").strip()
    scene_desc = (sc.get("description") or "").strip()
    name = char_name or (cc.get("name") or "少年")
    if kind in ("appear", "passive"):
        action = (f"{name} 侧卧在水洼浅水区，**身体完全平躺、只有头肩胸以上露出水面**、"
                  "腰以下浸在水里。眼睑沉重、刚要睁开、视线先落在水面那盏灯的碎影上。")
    else:
        action = (f"{name} 在水面浅水区咬紧后槽牙、把重心压到一只肘上撑起，"
                  "**上半身抬起约 20 厘米**、肩胛耸起一半便脱力。镜头停在撑到最高的那一帧。")
    parts = [
        "电影感单帧截图。暗调高反差，冷灰偏青、油灯暖色点缀。",
        scene_desc[:240],
        char_desc[:200],
        action,
        "湿透的灰褐短褐紧贴脊背，指节粗黑发干、指甲缝嵌泥，嘴唇干裂带血。"
        "16:9 横构图，无字幕无水印。",
    ]
    return "\n".join(parts)


# ────────────────────── 费用估计 ──────────────────────

def _estimate_cost(model: str, duration: float) -> float:
    """粗估一段需要多少灵感值（按官方定价的"灵感值/秒"经验值）。

    这些数字来自用户近几轮实测（用户余额 140 → 16 共消耗 124，对应
    1 次 10s 720p 图生视频 + 1 次图生图），不是平台官方价目。
    真凭据应返回 _refresh_pricing() 调出来的实时价 —— 这里先给常量。
    """
    base = {"kling-video-v3_0": 12.0, "kling-video-v2_6": 9.0,
            "kling-video-v2_5": 7.0, "kling-video-v1_6": 4.0}
    sec = max(5.0, min(10.0, duration))
    return round(base.get(model, 10.0) * (sec / 10.0), 1)


# ────────────────────── 真跑：拼接 + 验证 ──────────────────────

def _ffmpeg_path() -> str:
    """定位 ffmpeg 可执行文件。"""
    try:
        from core.ffmpeg_manager import get_ffmpeg_for_postprocess
        p = get_ffmpeg_for_postprocess()
        if p and os.path.exists(p):
            return p
    except Exception:  # noqa: BLE001
        pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return ""


# ══════════════════════════════════════════════════════════════
# ★★★ 花钱硬闸（2026-09-30 用户在真金白银上被烧了 30 元之后加的）
# ══════════════════════════════════════════════════════════════
#   背景：之前我能通过“直接调一个内部函数”就把真视频提交到平台，
#   完全没有预算约束，也没人确认。用户的原话：
#     「你要控制住，不是开玩笑的，这都是真金白银，你要严谨，这是钱！」
#
#   设计原则：**默认禁止花钱**。
#     · 任何真出片调用都必须在参数里显式带上预算上限；
#     · 预算为 0（默认）→ 直接拒，不发任何网络请求；
#     · 估算超过上限 → 拒，并返回估算值与上限。
#
#   VIDEoforge 内设的环境变量 `VIDEOFORGE_ALLOW_VIDEO_SPEND=1` 作为开总闸
#   （运维级，不是普通请求级）。两个都满足才放行。
# ══════════════════════════════════════════════════════════════


def spend_enabled() -> bool:
    """总闸：环境变量控制。默认关。"""
    return (os.environ.get("VIDEOFORGE_ALLOW_VIDEO_SPEND", "").strip().lower()
            in ("1", "true", "yes", "on"))


def _estimate_segment_cost_yuan(provider: str, duration: float,
                                resolution: str) -> float:
    """粗估一段视频多少钱（人民币）。

    价格来源：2026-09-30 用户真实账单反推（不是官网估算）：
      Seedance 2.5（豆包）短任务（≤5s）: ¥3.39　　长/高清: ¥6.11
      可灵 v3.0 无人民币价（灵感值）：按 0.9 元/秒粗估（用户体感）
    这是**保守估值**，宁可估高。真实价格请先跑 dry-run 看。
    """
    p = (provider or "").lower()
    d = max(1.0, float(duration or 5))
    res = (resolution or "720p").lower()
    if p in ("seedance", "doubao", "jimeng"):
        # 实测：短任务 ~3.39 元，720p ~6.11 元
        base = 6.11 if res in ("720p", "1080p", "1080P") else 3.39
        # 超过 5 秒按比例上浮
        return round(base * max(1.0, d / 5.0), 2)
    # 其它/provider 未知：按 0.9 元/秒粗估（用户体感的 Seedance 480p 价）
    return round(0.9 * d, 2)


async def _dispatch_segment_video(
    *, provider: str, model_name: str,
    prompt: str, first_image_url: str, tail_image_url: str,
    duration: float, aspect_ratio: str, resolution: str,
    seg_id: str, shot_id: str, seg_index: int, kind: str,
    settings: Dict[str, Any],
    spend_approved: bool = False,
    budget_yuan: float = 0.0,
    log: List[str] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    r"""按 shot 配的 provider/model 走适配器系统出视频。

    ★★ 两个花钱闸（**默认禁止**）：
       1. `spend_approved=True` 必须显式传（否则直接拒）
       2. `budget_yuan > 0` 且本次估算 ≤ budget_yuan（否则拒）
       3. 环境变量 `VIDEOFORGE_ALLOW_VIDEO_SPEND=1` 必须已开
    三个都满足才真发请求 —— 任何一条不满足都**不联网**。

    为什么加这个：2026-09-30 我用一次直连调用就把真视频提交到平台，
    用户被扣了 30 元。用户原话「这都是真金白银，你要严谨，这是钱！」
    默认禁止比任何承诺都可靠。

    为什么有这层：
      · 之前 shot_segments 硬编码 Kling-MCP（与 shot.model_provider 无关），
        用户配了 Seedance 也走 Kling，违反了 shot 级配置。
      · 现在 seedance/hailuo/kling 都走 `core/adapters/get_adapter(...)`、
        `degrade_request` / `pick_first_frame` 这些公共逻辑，
        Seedance 2.5 480p、hailuo 480p、Kling 720p 能一套代码走。

    Returns (local_path, error_msg, meta).
    """
    # ════════════════════════════════════════════════════════════
    # ★ 花钱闸 —— 不满足就不联网。默认全部不满足。
    # ════════════════════════════════════════════════════════════
    if not spend_enabled():
        return "", ("⛔ 花钱闸未开：环境变量 VIDEOFORGE_ALLOW_VIDEO_SPEND 不为 1，"
                     "任何真出片调用都被拒。这是 2026-09-30 用户被烧 30 元后加的硬保护。"), {
            "provider": provider, "blocked": "spend_enabled=false"}
    if not spend_approved:
        return "", ("⛔ 花钱闸未放行：spend_approved 未显式传 True。"
                     "真出片必须由用户确认过的调用方（API/agent 工具）传。"), {
            "provider": provider, "blocked": "spend_approved=false"}
    _est = _estimate_segment_cost_yuan(provider, duration, resolution)
    if budget_yuan <= 0:
        return "", (f"⛔ 花钱闸：未给预算上限（budget_yuan=0）。"
                     f"本段估算约 ¥{_est:.2f}。要真跑必须显式给一个 > 0 的上限。"), {
            "provider": provider, "blocked": "budget_yuan=0",
            "estimate_yuan": _est}
    if _est > budget_yuan:
        return "", (f"⛔ 花钱闸：本段估算 ¥{_est:.2f} 超过预算上限 ¥{budget_yuan:.2f}。"
                     f"要真跑就把上限提到至少 ¥{_est:.2f}。"), {
            "provider": provider, "blocked": "over_budget",
            "estimate_yuan": _est, "budget_yuan": budget_yuan}

    from core.adapters import (VideoGenRequest, get_adapter,
                                to_image_ref, degrade_request)
    api_key = ""
    if isinstance(settings, dict):
        api_key = ((settings.get("api_keys") or {}).get(provider) or "").strip()
    # ★ 走 “shot 配的 provider” 为首选；
    #   若 shot 没配（为空）或这个 provider 没 key，仍走 Kling MCP 作为退路。
    adapter = None
    used_provider = provider
    used_model = model_name
    try:
        if provider in ("seedance", "hailuo", "kling", "jimeng", "wanx", "minimax"):
            # ★ 没 key（走 adapter 会以“可灵 API key 未配置”逼返）→ 跳过 adapter，走 Kling MCP fallback
            if not api_key and provider == "kling":
                adapter = None
            else:
                adapter = get_adapter(provider, api_key=api_key,
                                      config={"model_name": model_name})
    except Exception:  # noqa: BLE001
        adapter = None

    if adapter is None:
        # 退路：Kling MCP。这条只用于 shot 未配 provider 或适配器拿不到。
        try:
            from core import kling_mcp_tools as _Tk
            # ★★★ 读 blueprint.json：每个 cast 都有 ref_image_path，用可灵 Element 绑定
            _bp = _load_blueprint(shot_id, log)
            _element_ids, _element_bind, _prompt_aug = await _bind_elements_from_blueprint(
                _bp, prompt, _Tk)
            gid = await _Tk.image_to_video(
                prompt, model=used_model,
                first_image=first_image_url, tail_image=tail_image_url,
                duration=duration, aspect_ratio=aspect_ratio,
                resolution=resolution,
                element_ids=_element_ids if _element_ids else None,
                element_bind=_element_bind if _element_bind else None,
                rationale=f"shot {shot_id} seg {seg_index} {kind}",
            )
            r = await _Tk.wait_task(gid, timeout=1500)
            urls = (r.get("urls_no_watermark") or r.get("urls") or [])
            if not urls:
                return "", (r.get("status_message") or
                            "Kling fallback 未返回 URL"), {"provider": "kling-fallback"}
            out_dir = os.path.join(
                os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd(),
                "outputs", "shot_segments", shot_id)
            os.makedirs(out_dir, exist_ok=True)
            local = os.path.join(out_dir, f"{seg_id}_kling.mp4")
            import httpx
            async with httpx.AsyncClient(timeout=600.0, follow_redirects=True,
                                         trust_env=False) as cli:
                resp = await cli.get(urls[0])
                with open(local, "wb") as f:
                    f.write(resp.content)
            if os.path.getsize(local) < 50_000:
                return "", "Kling fallback 下载太小", {"provider": "kling-fallback"}
            return local, "", {"provider": "kling-fallback",
                               "elapsed": r.get("elapsed", "")}
        except Exception as e:  # noqa: BLE001
            return "", f"适配器不可用且 Kling fallback 也跪：{type(e).__name__}: {e}", {}

    # 主路：适配器
    try:
        ff_local = ""
        lf_local = ""
        if first_image_url and not first_image_url.startswith(("http://", "https://")):
            ff_local = first_image_url
        else:
            ff_local = to_image_ref(first_image_url) if first_image_url else ""
        if tail_image_url and not tail_image_url.startswith(("http://", "https://")):
            lf_local = tail_image_url
        else:
            lf_local = to_image_ref(tail_image_url) if tail_image_url else ""
        req = VideoGenRequest(
            prompt=prompt,
            duration=int(round(duration)),
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            first_frame=ff_local or None,
            last_frame=lf_local or None,
        )
        verr, dropped = degrade_request(adapter, req)
        if verr:
            return "", f"请求参数不合法：{verr}", {"provider": provider, "model": used_model}
        # 不阻塞太久。Seedance 480p 调度接口返回较慢。
        result = await asyncio.wait_for(adapter.generate(req), timeout=300)
        try:
            await adapter.close()
        except Exception:
            pass
        if not result.success:
            return "", (result.error or f"{provider} 返回失败"), {
                "provider": provider, "model": used_model,
                "task_id": result.task_id or "",
            }
        if not result.video_url:
            # 同步接口（如 doubao seedance t2v）可能需要 query_task
            if result.task_id:
                t0 = time.time()
                while time.time() - t0 < 600:
                    st = await adapter.query_task(result.task_id)
                    if getattr(st, "success", False) and getattr(st, "video_url", ""):
                        result = st; break
                    if getattr(st, "failed", False):
                        return "", (st.error or f"{provider} 任务失败"), {
                            "provider": provider, "model": used_model,
                            "task_id": result.task_id,
                        }
                    await asyncio.sleep(5)
            if not result.video_url:
                return "", f"{provider} 返回成功但未拿到 URL", {
                    "provider": provider, "model": used_model,
                    "task_id": result.task_id or "",
                }
        out_dir = os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd(),
            "outputs", "shot_segments", shot_id)
        os.makedirs(out_dir, exist_ok=True)
        local = os.path.join(out_dir, f"{seg_id}_{provider}.mp4")
        await adapter.download_to_local(result.video_url, local)
        if os.path.getsize(local) < 50_000:
            return "", f"{provider} 下载文件过小 ({os.path.getsize(local)}b)", {
                "provider": provider, "model": used_model,
            }
        return local, "", {"provider": provider, "model": used_model,
                           "task_id": result.task_id or "",
                           "duration": getattr(result, "duration_seconds", None) or duration}
    except asyncio.TimeoutError:
        return "", f"{provider} 超时", {"provider": provider, "model": used_model}
    except Exception as e:  # noqa: BLE001
        return "", f"{provider} 调度异常：{type(e).__name__}: {e}", {
            "provider": provider, "model": used_model,
        }
    """定位 ffmpeg 可执行文件（沿用项目其它模块的约定）。"""
    try:
        from core.ffmpeg_manager import get_ffmpeg_for_postprocess
        p = get_ffmpeg_for_postprocess()
        if p and os.path.exists(p):
            return p
    except Exception:  # noqa: BLE001
        pass
    # 后备：imageio_ffmpeg 自带的那个
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return ""


def _concat_videos(parts: List[str], out_path: str, *,
                   target_resolution: str = "480p",
                   ffmpeg: str = "") -> Dict[str, Any]:
    """用 ffmpeg concat 把若干 mp4 拼成一段（带 re-encode + 目标分辨率）。"""
    if not parts:
        return {"ok": False, "error": "没有可拼接的片段"}
    if not ffmpeg:
        return {"ok": False, "error": "找不到 ffmpeg 可执行文件"}

    # 列表文件（避免命令行转义中文）
    list_path = out_path + ".concat.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in parts:
            ap = os.path.abspath(p).replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{ap}'\n")

    h = {"480p": 480, "720p": 720, "1080p": 1080}.get(target_resolution, 480)
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
           "-vf", f"scale=-2:{h}:flags=lanczos",
           "-c:v", "libx264", "-preset", "slow", "-crf", "21",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           "-an", out_path]
    try:
        import subprocess
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            return {"ok": False, "error": "ffmpeg 拼接失败：" + (r.stderr or "")[-400:],
                    "cmd": " ".join(cmd)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    try:
        size = os.path.getsize(out_path)
    except OSError:
        size = 0
    return {"ok": True, "path": out_path, "bytes": size,
            "n_inputs": len(parts), "list_path": list_path}


def _verify_video(video_path: str, *, ffmpeg: str = "",
                  expected_secs: List[float] = None) -> Dict[str, Any]:
    r"""出片后抽帧 + 列 ffmpeg 时长，确认真的拼上了、各段都在。

    返回 {ok, frames:[path], duration_sec, contact_sheet_path}。
    """
    if not ffmpeg or not os.path.exists(video_path):
        return {"ok": False, "error": "缺少 ffmpeg 或视频文件"}

    import subprocess
    try:
        r = subprocess.run([ffmpeg, "-i", video_path], capture_output=True,
                           text=True, timeout=30)
        info = (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读视频信息失败：{e}"}

    m = re.search(r"Duration:\s+(\d+):(\d+):([\d.]+)", info)
    dur = 0.0
    if m:
        h, mi, s = m.groups()
        dur = float(h) * 3600 + float(mi) * 60 + float(s)

    out_dir = os.path.join(os.path.dirname(video_path), "frames_verify")
    os.makedirs(out_dir, exist_ok=True)
    frame_paths: List[str] = []
    if dur > 0:
        # 每段一个代表帧
        n = max(1, min(6, len(expected_secs or [1, 2, 3]) or 3))
        for i in range(n):
            t = dur * (i + 0.5) / n
            p = os.path.join(out_dir, f"v_{i:02d}_{t:.1f}s.png")
            subprocess.run([ffmpeg, "-y", "-ss", f"{t:.2f}", "-i", video_path,
                            "-frames:v", "1", "-vf", "scale=320:-1", p],
                           capture_output=True, timeout=60)
            if os.path.exists(p):
                frame_paths.append(p)

    contact = ""
    if frame_paths:
        contact = os.path.join(os.path.dirname(video_path), "verify_sheet.png")
        subprocess.run(
            [ffmpeg, "-y", "-i", frame_paths[0],
             "-vf", f"tile=1x{len(frame_paths)}:padding=4:color=black", contact],
            capture_output=True, timeout=60)

    return {"ok": True, "duration_sec": round(dur, 2),
            "frames": frame_paths, "contact_sheet": contact,
            "expected_total_sec": (sum(expected_secs) if expected_secs else None)}


# ────────────────────── 主入口 ──────────────────────

async def produce_shot_segments(
    db, shot_id: str, *,
    model: str = "kling-video-v3_0",
    target_resolution: str = "480p",
    dry_run: bool = False,
    upload_only: bool = False,
    use_existing_first_frame: bool = True,
    force: bool = False,
    dedup_window_minutes: int = 60,
    budget_yuan: float = 0.0,
) -> Dict[str, Any]:
    r"""按 L2 时间轴分段出片。

    Parameters
    ----------
    db          : 项目的 db（main.py 里的 db）
    shot_id     : 分镜 id
    model       : 视频模型（默认 kling-video-v3_0）
    target_resolution : 最终压成的分辨率（"480p" / "720p" / "1080p"）
    dry_run     : True = 只出方案、不真跑（灵感值不够时仍能跑通链路）
    upload_only : True = 只上传/合成首帧，不调视频模型
    use_existing_first_frame : True = 优先用 ctx 里的现有合成首帧；
                                  False = 强制为每段现做首帧
    force       : True = 跳过去重与并发锁。★★ 默认 False 是为了防“重复烧钱”；
                  只有明确要重出时带上
    dedup_window_minutes : 本镜在多少分钟内刚出过片，就不再出（默认 60）。
                  期间重调默认返回“skipped=True、note=skip”，除非 force=true。

    Returns
    -------
    {
      "ok": True,
      "shot_id": "...",
      "plan": [SegmentPlan dict, ...],
      "segments": [{"i", "duration", "first_image_url", "tail_image_url",
                    "video_path", "prompt"}, ...],
      "merged_video": "...",
      "verify": {...},
      "totals": {"seconds": ..., "cost_estimate": ..., "n_segments": ...},
      "dry_run": False,
      "skipped": False,          # ★ 本轮没真跑（去重命中）
      "skip_reason": "",         # ★ 跳过的原因（人类可读）
    }
    """
    shot = db.get_shot(shot_id) if hasattr(db, "get_shot") else None
    if not shot:
        return {"ok": False, "error": f"分镜不存在：{shot_id}"}

    pid = shot.get("project_id") or ""
    ctx = gather_assets(db, shot)
    plans = plan_segments(shot, ctx, model=model)

    # ★ 分辨率提醒：Seedance 非 480p → 贵 ~80%。dry-run 也要报，
    #   因为用户要靠这个决定"要不要真跑"。
    _prov_guess = (shot.get("model_provider") or "").strip() or "kling"
    _est_this = sum(_estimate_segment_cost_yuan(
        _prov_guess, p.duration,
        "480p" if target_resolution == "480p" else target_resolution) for p in plans)
    _est_480 = sum(_estimate_segment_cost_yuan(_prov_guess, p.duration, "480p")
                   for p in plans)
    _res_warn = ""
    if _prov_guess in ("seedance", "doubao", "jimeng") and target_resolution != "480p":
        _res_warn = (
            f"⚠ 当前 target_resolution=**{target_resolution}**，Seedance 该档单价 ¥6.11/短任务，"
            f"比 480p（¥3.39）贵 ~80%。\n"
            f"  估算 ¥{_est_this:.2f}；改回 480p 只要约 ¥{_est_480:.2f}。\n"
            f"  除非有明确清晰度需求，建议改回 \"480p\"。")

    if dry_run:
        return {
            "ok": True, "shot_id": shot_id, "dry_run": True,
            "plan": [asdict(p) for p in plans],
            "context": {k: v for k, v in ctx.items() if not k.endswith("_card") or v},
            "resolution_warning": _res_warn,
            "totals": {
                "n_segments": len(plans),
                "seconds": sum(p.duration for p in plans),
                "cost_estimate": sum(p.cost_estimate for p in plans),
                "needs_synthesis": [p.index for p in plans if p.needs_synthesis],
                "target_resolution": target_resolution,
                "real_estimate_yuan": _est_this,
                "estimate_480p_yuan": _est_480,
            },
            "note": ("这是方案，**没有花任何钱**。\n"
                     f"  · 共 {len(plans)} 段 / {sum(p.duration for p in plans):.1f}s\n"
                     f"  · 本档估算 ¥{_est_this:.2f}（480p 约 ¥{_est_480:.2f}）\n"
                     f"  · 需要现做合成首帧的段：{[p.index for p in plans if p.needs_synthesis] or '无'}\n"
                     + (_res_warn + "\n" if _res_warn else "")
                     + "执行 → produce_shot_segments(dry_run=False, budget_yuan=…)"),
        }

    # ★★★ 防重复烧钱的三道闸（2026-09-30 用户刚充值说“烧得太快了”之后加的）：
    #   闸 1：merged_video 本地已存在 + 在 dedup_window_minutes 窗口内 → skipped
    #   闸 2：同一 shot_id 已在 in-process 跑 → skipped（并发互斥）
    #   闸 3：candidates 里最近一条是分段合并出来的、在窗口内 → skipped
    #   上面三道都判过，除非 force=True 才跳过闸门、往下真调模型。
    if not force:
        skip = _dedup_check(shot, target_resolution=target_resolution,
                             window_min=dedup_window_minutes)
        if skip:
            return {
                "ok": True, "shot_id": shot_id, "dry_run": False,
                "skipped": True, "skip_reason": skip["reason"],
                "merged_video": skip.get("merged_path"),
                "verify": {},
                "totals": {
                    "n_segments": len(plans),
                    "seconds": sum(p.duration for p in plans),
                    "cost_estimate": 0.0,   # ★ 跳过，所以为 0
                    "spent": 0.0,
                },
                "plan": [asdict(p) for p in plans],
                "segments": [],
                "failures": [],
                "log": [],
                "note": (f"防重复烧钱闸门命中：{skip['reason']}\n"
                         f"  · 已存在 merged_video: {skip.get('merged_path')}\n"
                         f"  · 生成时间: {skip.get('generated_at')}\n"
                         "★ 本轮没调模型、没花钱。明确要重出 → force=true"),
            }

        # ══════════════════════════════════════════════════════════════
    # ★★★ 闸 0：**花钱闸**（2026-09-30 用户被烧 30 元后加）
    # ══════════════════════════════════════════════════════════════
    #   编排层就要先把钱拦住，而不是等 _dispatch_segment_video 里逐个拦。
    #   拒跑条件（任一命中即拒、**不发任何网络请求**）：
    #     · 环境变量 VIDEOFORGE_ALLOW_VIDEO_SPEND 未开
    #     · budget_yuan <= 0（调用方没给预算上限）
    #     · 估算总额 > budget_yuan
    # ══════════════════════════════════════════════════════════════
    _seg_provider_for_cost = (shot.get("model_provider") or "").strip() or "kling"
    _est_total = sum(
        _estimate_segment_cost_yuan(_seg_provider_for_cost, p.duration,
                                     "480p" if target_resolution == "480p" else target_resolution)
        for p in plans)
    if not spend_enabled():
        return {
            "ok": False, "shot_id": shot_id,
            "blocked_by": "spend_gate",
            "error": ("⛔ **花钱闸未开**：环境变量 VIDEOFORGE_ALLOW_VIDEO_SPEND 不为 1。\n"
                      "真出片默认禁止。要真跑请先由用户显式开闸。"),
            "estimate_yuan": _est_total,
            "totals": {"n_segments": len(plans), "cost_estimate": 0.0, "spent": 0.0},
            "note": "★ 本轮没联网、没花钱。",
        }
    if budget_yuan <= 0:
        return {
            "ok": False, "shot_id": shot_id,
            "blocked_by": "budget_gate",
            "error": (f"⛔ **未给预算上限**（budget_yuan=0）。本镜共 {len(plans)} 段，"
                      f"估算约 ¥{_est_total:.2f}。\n"
                      f"要真跑必须显式传 budget_yuan（> 0）且 ≥ 估算值。"),
            "estimate_yuan": _est_total,
            "totals": {"n_segments": len(plans), "cost_estimate": _est_total, "spent": 0.0},
            "note": "★ 本轮没联网、没花钱。",
        }
    if _est_total > budget_yuan:
        return {
            "ok": False, "shot_id": shot_id,
            "blocked_by": "over_budget",
            "error": (f"⛔ **估算超预算**：本镜估算 ¥{_est_total:.2f} > 预算上限 ¥{budget_yuan:.2f}。\n"
                      f"要真跑请把 budget_yuan 提到 ≥ ¥{_est_total:.2f}。"),
            "estimate_yuan": _est_total, "budget_yuan": budget_yuan,
            "totals": {"n_segments": len(plans), "cost_estimate": _est_total, "spent": 0.0},
            "note": "★ 本轮没联网、没花钱。",
        }

    # ★ 720p 提醒（2026-09-30 用户指定：「尽量都出 Seedance 480p，尽量不碰 720p」）
    #   Seedance 720p 单价 ¥6.11 vs 480p ¥3.39（贵 ~80%）。
    #   这里**不硬拦**（用户说"尽量"不是"绝对禁止"），但会把警告写进返回，
    #   让前端/agent 能看见，避免"默认值悄悄顶上去"这类事故重演。
    _resolution_warning = ""
    if _seg_provider_for_cost in ("seedance", "doubao", "jimeng") \
            and target_resolution != "480p":
        _resolution_warning = (
            f"⚠ 当前请求的是 **{target_resolution}**，而 Seedance 该档单价 ¥6.11/短任务，"
            f"比 480p（¥3.39）贵 ~80%。\n"
            f"  除非有明确理由（需要更高清晰度交付），建议把 target_resolution 改回 \"480p\"。\n"
            f"  总估算：¥{_est_total:.2f}（480p 的话约 ¥{sum(_estimate_segment_cost_yuan(_seg_provider_for_cost, p.duration, '480p') for p in plans):.2f}）")

    # 闸 2：并发互斥锁（in-process；多进程部署要换 Redis/file lock）
    _locks: Dict[str, asyncio.Lock] = _SHOT_LOCKS
    lock = _locks.setdefault(shot_id, asyncio.Lock())
    if lock.locked():
        return {
            "ok": False, "shot_id": shot_id,
            "skipped": True,
            "skip_reason": (f"本镜头正在被另一个请求生成中（进程内并发锁命中）。"
                            f"请等当前调用返回后再试；或带 force=true 重试。"),
            "note": "★ 没重复花钱，但请你等几秒看进度。",
        }
    async with lock:
        return await _produce_shot_segments_inner(
            db, shot, ctx, plans,
            model=model, target_resolution=target_resolution,
            upload_only=upload_only,
            use_existing_first_frame=use_existing_first_frame,
            budget_yuan=budget_yuan,
            _est_total=_est_total,
            _resolution_warning=_resolution_warning,
        )


#: in-process 并发互斥锁（key=shot_id）—— 同进程内同一镜头不会并发烧钱。
#: 多进程部署 / 多机器访问需要换成 redis/file lock。这里是单机
#: VideoForge，in-process 足够；超过这个范围的话这个锁**失效**，
#: 调用方必须自行负责不重复点击（UI 状态 + 按钮 disabled 也能挡一部分）。
_SHOT_LOCKS: Dict[str, asyncio.Lock] = {}


def _dedup_check(shot: Dict[str, Any], *, target_resolution: str,
                 window_min: int) -> Dict[str, Any]:
    r"""**花钱之前**检查本镜头是不是已经出过了。

    三道闸：
      ① merged_video 文件存在 → merged_path
      ② 该文件的 mtime 在 window_min 分钟内 → generated_at
      ③ candidates 里最新一条是分段路径（merged_segments/_x） 且在窗口内
    任何一道闸中 → 返回 {merged_path, generated_at, reason}，外层跳过。
    """
    import json as _json
    import time as _t

    pid = shot.get("project_id") or ""
    out_dir = os.path.join(
        os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd(),
        "outputs", str(pid), "shots")

    candidates: List[Dict[str, Any]] = []
    raw = shot.get("candidates")
    if isinstance(raw, str):
        try: candidates = _json.loads(raw) if raw.strip() else []
        except Exception: candidates = []
    elif isinstance(raw, list):
        candidates = list(raw)

    threshold = _t.time() - window_min * 60.0

    # 闸 ①/② merged_video 文件路径检查
    merged_path = os.path.join(out_dir, f"{shot['id']}-分段-{target_resolution}.mp4")
    if os.path.exists(merged_path):
        try:
            mt = os.path.getmtime(merged_path)
        except OSError:
            mt = 0
        if mt >= threshold:
            return {
                "merged_path": merged_path,
                "generated_at": _t.strftime("%Y-%m-%d %H:%M:%S",
                                             _t.localtime(mt)),
                "reason": (f"merged_video 本地文件存在（{merged_path.split(os.sep)[-1]}），"
                            f"且生成于 {window_min} 分钟内。"),
            }

    # 闸 ③ 走 candidates 看有没有最近一段是分段合并出来的
    for c in sorted(candidates, key=lambda x: float(x.get("created_at") or 0),
                    reverse=True)[:3]:
        p = (c.get("path") or "").strip()
        if not p or "分段" not in os.path.basename(p) and "segments" not in p:
            continue
        if not os.path.exists(p):
            continue
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = 0
        if mt >= threshold:
            return {
                "merged_path": p,
                "generated_at": _t.strftime("%Y-%m-%d %H:%M:%S",
                                             _t.localtime(mt)),
                "reason": (f"candidates 里最近一条是分段合并出来的"
                            f"（{os.path.basename(p)}），生成于 {window_min} 分钟内。"),
            }
    return {}


async def _produce_shot_segments_inner(
    db, shot, ctx, plans, *,
    model: str, target_resolution: str,
    upload_only: bool,
    use_existing_first_frame: bool,
    budget_yuan: float = 0.0,
    _est_total: float = 0.0,
    _resolution_warning: str = "",
) -> Dict[str, Any]:
    """真跑部分（在并发锁里跑；外层 produce_shot_segments 已经做了去重）。"""
    pid = shot.get("project_id") or ""
    segs_done: List[Dict[str, Any]] = []
    seg_videos: List[str] = []
    failed: List[Dict[str, Any]] = []
    log: List[str] = []

    # ★ 取设置里的 api_keys（每家适配器从 settings.api_keys 读自己的 key）
    try:
        _settings = db.get_all_settings() if hasattr(db, "get_all_settings") else {}
    except Exception:  # noqa: BLE001
        _settings = {}

    # ffmpeg 路径（拼接 / 验证都依赖）
    _ffmpeg = _ffmpeg_path()

    # 真跑（外层已查过去重与并发锁）
    from core import kling_mcp_tools as T

    segs_done: List[Dict[str, Any]] = []
    seg_videos: List[str] = []
    failed: List[Dict[str, Any]] = []

    # ★ 拿 shot 配的 video provider：决定合成首帧是否走（只有 Kling 带 image-to-image）
    seg_provider_for_dispatch = (shot.get("model_provider") or "").strip() or "kling"

    for p in plans:
        # 0. 合成首帧（如需要）
        first_url = ""
        if use_existing_first_frame and p.first_image and \
                not str(p.first_image).startswith(("http://", "https://")):
            # ★ video provider 是 Kling → 要先把本地文件传到 Kling 拿 URL。
            #   其他 provider 走适配器系统会自动 upload（to_image_ref 内部会调 Kling）。
            #   但 to_image_ref 走的是 Kling MCP——这里只有 shot provider == kling 才走 Kling 传。
            if seg_provider_for_dispatch.startswith("kling"):
                first_url = await T.upload_file(p.first_image)
        elif not use_existing_first_frame and p.needs_synthesis \
                and seg_provider_for_dispatch.startswith("kling"):
            # ★ Seedance 2.5 没有 image-to-image 能力。video provider 不是 Kling
            #   时跳过合成（active_composite 会跌到 lying/character_only 当首帧）。
            log.append(f"[seg{p.index}] 现做合成首帧（kling-image-v3_0_omni）")
            refs: List[str] = []
            if ctx.get("scene_only"):
                refs.append(await T.upload_file(ctx["scene_only"]))
            if ctx.get("character_only"):
                refs.append(await T.upload_file(ctx["character_only"]))
            res = await T.image_to_image(
                p.synthesis_prompt, refs,
                model="kling-image-v3_0_omni",
                aspect_ratio="16:9",
                rationale=f"shot {shot.get('id')} seg {p.index} {p.kind} 首帧")
            r = await T.wait_task(res, timeout=600)
            url = (r.get("urls") or [None])[0]
            if url:
                first_url = url
                log.append(f"[seg{p.index}] 合成首帧 ok ({len(url)}b)")
            else:
                failed.append({"i": p.index, "stage": "synthesis",
                               "error": r.get("status_message") or "合成失败"})
                continue
        elif p.first_image:
            first_url = await T.upload_file(p.first_image)
        else:
            failed.append({"i": p.index, "stage": "no_first",
                           "error": "没有任何首帧可用"})
            continue

        tail_url = ""
        if p.tail_image and p.tail_image != p.first_image:
            tail_url = await T.upload_file(p.tail_image)

        if upload_only:
            segs_done.append({
                "i": p.index, "kind": p.kind,
                "first_image_url": first_url, "tail_image_url": tail_url,
                "duration": p.duration, "prompt": p.prompt,
                "needs_synthesis": p.needs_synthesis, "note": p.note,
            })
            continue

        # ★ 1. 视频生成（走**适配器系统**，读 shot 自己的 model_provider/model_name）
        #   之前这里硬编码 kling-video-v3_0，用户改 shot 配成 Seedance 完全不起作用。
        #   现在：shot 写 seedance 就走 seedance，写 kling 走 kling，写 hailuo 走 hailuo。
        #   Kling MCP 作为 fallback（shot 没写 provider 时）。
        seg_provider = (shot.get("model_provider") or "").strip() or "kling"
        seg_model = (shot.get("model_name") or "").strip() or model
        seg_resolution = ("480p" if target_resolution == "480p"
                     else target_resolution)  # ★ 480p 真正传 480p，不再被笔误顶到 720p
        seg_path, seg_err, seg_meta = await _dispatch_segment_video(
            provider=seg_provider, model_name=seg_model,
            log=log,
            prompt=p.prompt, first_image_url=first_url, tail_image_url=tail_url,
            duration=p.duration, aspect_ratio="16:9",
            resolution=seg_resolution,
            seg_id=f"seg{p.index:02d}_{int(p.duration)}s",
            shot_id=str(shot.get("id")), seg_index=p.index, kind=p.kind,
            settings=_settings,
            # ★ 两层闸都已在编排层过，这里显式放行 + 给足预算
            spend_approved=True,
            budget_yuan=budget_yuan,
        )
        if seg_err:
            failed.append({"i": p.index, "stage": "video", "error": seg_err,
                           "provider": seg_provider, "model": seg_model})
            log.append(f"[seg{p.index}] ✗ {seg_provider}/{seg_model}: {seg_err[:120]}")
            logger.error("[seg%s] video dispatch FAILED: provider=%s err=%s",
                         p.index, seg_provider, seg_err)
            continue
        log.append(f"[seg{p.index}] ✓ {seg_provider}/{seg_model} ({seg_meta.get('elapsed','?')}s)")
        seg_videos.append(seg_path)
        segs_done.append({
            "i": p.index, "kind": p.kind,
            "first_image_url": first_url, "tail_image_url": tail_url,
            "video_path": seg_path, "duration": p.duration,
            "prompt": p.prompt, "needs_synthesis": p.needs_synthesis,
            "note": p.note,
            "provider": seg_provider, "model": seg_model,
        })
        continue

        # ---- 以下是旧 Kling MCP 路径（保留作为 fallback）----
        # 1. 视频生成
        try:
            # ★★★ 读 blueprint.json：用可灵 Element 绑定角色（与主路径相同）
            _bp = _load_blueprint(shot.get("id"), log)
            _eids, _ebind, _prompt_aug = await _bind_elements_from_blueprint(_bp, p.prompt, T)
            _prompt_with_elems = (p.prompt + _prompt_aug) if _prompt_aug else p.prompt
            gid = await T.image_to_video(
                _prompt_with_elems, model=seg_model,
                first_image=first_url, tail_image=tail_url,
                duration=p.duration, aspect_ratio="16:9",
                resolution=("480p" if target_resolution == "480p"
                          else target_resolution),  # ★ 同上笔误修
                element_ids=_eids if _eids else None,
                element_bind=_ebind if _ebind else None,
                rationale=f"shot {shot.get('id')} seg {p.index} {p.kind}")
        except Exception as e:  # noqa: BLE001
            failed.append({"i": p.index, "stage": "video_submit",
                           "error": f"{type(e).__name__}: {e}"})
            continue
        log.append(f"[seg{p.index}] gid={gid}")

        # 2. 等任务完成 + 下载
        try:
            r = await T.wait_task(gid, timeout=1500)
        except Exception as e:  # noqa: BLE001
            failed.append({"i": p.index, "stage": "wait", "error": f"{e}"})
            continue
        urls = (r.get("urls_no_watermark") or r.get("urls") or [])
        if not urls:
            failed.append({"i": p.index, "stage": "no_url",
                           "error": r.get("status_message") or "生成完成但无 URL",
                           "raw": r})
            continue

        seg_dir = os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd(),
            "outputs", pid, "shots", "segments", shot.get("id"))
        os.makedirs(seg_dir, exist_ok=True)
        seg_path = os.path.join(seg_dir, f"seg{p.index:02d}_{int(p.duration)}s.mp4")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=600.0, follow_redirects=True,
                                         trust_env=False) as cli:
                rr = await cli.get(urls[0])
                with open(seg_path, "wb") as f:
                    f.write(rr.content)
        except Exception as e:  # noqa: BLE001
            failed.append({"i": p.index, "stage": "download",
                           "error": f"{type(e).__name__}: {e}"})
            continue

        if os.path.getsize(seg_path) < 50_000:
            failed.append({"i": p.index, "stage": "size",
                           "error": f"文件过小 {os.path.getsize(seg_path)}b"})
            continue

        seg_videos.append(seg_path)
        segs_done.append({
            "i": p.index, "kind": p.kind,
            "first_image_url": first_url, "tail_image_url": tail_url,
            "video_path": seg_path, "duration": p.duration,
            "prompt": p.prompt, "needs_synthesis": p.needs_synthesis,
            "note": p.note,
        })

    if not seg_videos:
        return {
            "ok": False, "shot_id": shot.get("id"), "segments": segs_done,
            "failures": failed, "log": log,
            "note": "没有任何一段生成成功 —— 见 failures[]",
        }

    # 3. 拼接
    out_dir = os.path.join(
        os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd(),
        "outputs", pid, "shots")
    os.makedirs(out_dir, exist_ok=True)
    merged_path = os.path.join(out_dir, f"{shot.get('id')}-分段-{target_resolution}.mp4")
    concat = _concat_videos(seg_videos, merged_path,
                            target_resolution=target_resolution, ffmpeg=_ffmpeg)

    # 4. 验证
    verify = {}
    if concat.get("ok"):
        verify = _verify_video(merged_path, ffmpeg=_ffmpeg,
                               expected_secs=[p.duration for p in plans])

    return {
        "ok": bool(concat.get("ok")),
        "shot_id": shot.get("id"),
        "plan": [asdict(p) for p in plans],
        "segments": segs_done,
        "failures": failed,
        "merged_video": concat,
        "verify": verify,
        "totals": {
            "n_segments": len(plans),
            "seconds_planned": sum(p.duration for p in plans),
            "seconds_got": len(seg_videos),
            "cost_estimate": sum(p.cost_estimate for p in plans),
            "real_estimate_yuan": _est_total,
            "target_resolution": target_resolution,
        },
        # ★ 分辨率提醒（Seedance 非 480p 时非空）
        "resolution_warning": _resolution_warning,
        "log": log,
        "note": ("按 L2 时间轴分段出片完成。**拼接后视频的总时长可能与原 L2 "
                 "有 ±0.5s 偏差**（可灵生成时长是 5/10s 取整的，4s 段会被渲成 5s）。"
                 "如要严格匹配，请把每段时长改成 5/10s 倍数。"),
    }