# -*- coding: utf-8 -*-
"""VideoForge · 跨镜接龙（continuity relay）

═══════════════════════════════════════════════════════════════
为什么需要（2026-10-22，用户第 3 条 + 强模型规格）
═══════════════════════════════════════════════════════════════
用户原话：「保证分镜之间的连贯性，比如同一个场景，保证在硬切画面前原先角色、道具位置的一致性
（角度可以变，但位置逻辑得一致）」。

已实测的硬约束：
  · 平台**严格采纳** first_frame（构图 100% 照搬）；但 first_frame 与 reference_image **互斥**。
  · 所以"接龙"（上一镜尾帧 → 下一镜首帧）能拿到**像素级连续**，
    代价是这一镜**不能再带角色/场景参考图**（身份只能靠文字锚定）。
  · 白膜视频不能当参考素材（网关不声明）⇒ 运镜只能靠提示词。

决策规则（强模型给的表，我照做）：
  同场景 + 同机位(同景别) + 动作连续 + 没有时间跳跃   → **接龙 A**
  其余（同场景换机位 / 跨场景 / 时间跳跃 / 景别突变）  → **独立 B**（关键帧当首帧 + 参考图）
  另外：**连续接龙满 2 镜后强制回到 B**（重校准），防角色漂移。

本模块负责：判定、接龙首帧路径、连续性快照（continuity brief）、
           以及"首帧与提示词动作是否打架"的预检（打架就降级 B）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.shot_chain")

#: 景别关键词（用于判断"同机位"）
_SHOT_SIZES = ("远景", "全景", "中景", "近景", "特写", "大特写", "微距")
#: 时间跳跃关键词（出现即视为断层，不能接龙）
_TIME_GAPS = ("次日", "第二天", "三日后", "数日后", "后来", "与此同时", "与此同时刻", "若干年后", "回忆", "闪回", "转场")


def last_frame_path(output_dir: str, project_id: str, shot_id: str) -> str:
    """找某一镜的**最后一段尾帧**（生成本来就会存 seg_XX_last.jpg）。

    ★ 不用新增保存逻辑：分段生成时每段都会落一张 `seg_XX_last.jpg`，
      这里按序号取最大的那张即可（没有就返回空串 → 该镜退回独立镜）。
    """
    if not shot_id:
        return ""
    folder = Path(output_dir) / str(project_id) / str(shot_id)
    if not folder.is_dir():
        return ""
    frames = sorted(folder.glob("seg_*_last.jpg"))
    if not frames:
        return ""
    def _idx(p: Path) -> int:
        m = re.search(r"seg_(\d+)_last", p.name)
        return int(m.group(1)) if m else 0
    return str(sorted(frames, key=_idx)[-1])


def shot_size(text: str) -> str:
    """从镜头描述里取景别。

    ★ 按**出现位置**取最早的那个，不是按固定顺序 —— 旧写法按顺序找，
      "全景"会永远压过"特写"（两者都出现时），于是同机位连续动作被判成换机位，
      接龙永远不触发（实测踩到）。
    """
    body = text or ""
    best_at = -1
    best = ""
    for size in _SHOT_SIZES:
        at = body.find(size)
        if at >= 0 and (best_at < 0 or at < best_at):
            best_at = at
            best = size
    return best



def _timeline_of(shot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """镜头的 layer2_timeline（字符串则解 JSON）。"""
    timeline = shot.get("layer2_timeline")
    if isinstance(timeline, str):
        try:
            timeline = json.loads(timeline)
        except Exception:  # noqa: BLE001
            timeline = []
    return [i for i in (timeline or []) if isinstance(i, dict)]


def shot_size_of(shot: Dict[str, Any]) -> str:
    """这一镜的景别 —— **以 camera 字段为准**，没有才回退到概述。

    ★ 实测踩坑：概述常写成"从全景推到特写"，按全文出现位置取会取到"全景"，
      而真正的机位是 camera 里的"特写" ⇒ 同机位连续动作被误判成换机位，接龙不触发。
    """
    for item in _timeline_of(shot):
        camera = str(item.get("camera") or "")
        size = shot_size(camera)
        if size:
            return size
    return shot_size(str(shot.get("layer1_overview") or ""))

def has_time_gap(text: str) -> bool:
    """镜头描述里是否有时间跳跃。"""
    return any(mark in (text or "") for mark in _TIME_GAPS)


def can_relay(prev_shot: Optional[Dict[str, Any]], shot: Dict[str, Any]) -> Dict[str, Any]:
    """判定这一镜能不能接上一镜的尾帧。

    @returns `{relay: bool, reason: str}`
    """
    if not prev_shot:
        return {"relay": False, "reason": "没有上一镜"}
    if str(prev_shot.get("scene_id") or "") != str(shot.get("scene_id") or ""):
        return {"relay": False, "reason": "不是同一个场景"}
    prev_text = _shot_text(prev_shot)
    this_text = _shot_text(shot)
    if has_time_gap(this_text) or has_time_gap(prev_text):
        return {"relay": False, "reason": "有时间跳跃"}
    prev_size = shot_size_of(prev_shot)
    this_size = shot_size_of(shot)
    if prev_size and this_size and prev_size != this_size:
        return {"relay": False, "reason": f"景别不同（{prev_size} → {this_size}）"}
    if not this_size:
        return {"relay": False, "reason": "这一镜没有写明景别，接龙容易出怪缩放"}
    return {"relay": True, "reason": f"同场景同景别（{this_size}）"}


def _shot_text(shot: Dict[str, Any]) -> str:
    """镜头描述的合集（概述 + 时间轴 + 约束）。"""
    bits = [str(shot.get("layer1_overview") or "")]
    timeline = shot.get("layer2_timeline")
    if isinstance(timeline, str):
        try:
            timeline = json.loads(timeline)
        except Exception:  # noqa: BLE001
            timeline = []
    for item in (timeline or []):
        if isinstance(item, dict):
            bits.append(str(item.get("action") or ""))
            bits.append(str(item.get("camera") or ""))
    bits.append(str(shot.get("layer3_constraints") or "")[:400])
    return "\n".join(bits)


def continuity_brief(prev_shot: Optional[Dict[str, Any]]) -> str:
    """从上一镜的文本推一份"接续快照"，注入下一镜提示词。

    ★ 我们目前没有"读尾帧画面"的视觉模型调用（网关的 gemini 也能看图，但要先接进来），
      所以这一版**只从上一镜的文字**推（动作末尾 + 机位 + 道具），
      事实来自上一镜自己写的分镜，**不编造**。
    """
    if not prev_shot:
        return ""
    timeline = prev_shot.get("layer2_timeline")
    if isinstance(timeline, str):
        try:
            timeline = json.loads(timeline)
        except Exception:  # noqa: BLE001
            timeline = []
    last_action = ""
    last_camera = ""
    props: List[str] = []
    for item in (timeline or []):
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip()
        if action:
            last_action = action
        camera = str(item.get("camera") or "").strip()
        if camera:
            last_camera = camera
        for p in (item.get("props_used") or []):
            if p and str(p) not in props:
                props.append(str(p))
    if not (last_action or last_camera):
        return ""
    bits = []
    if last_action:
        bits.append(f"上一镜结束在：{last_action[-40:]}")
    if last_camera:
        bits.append(f"机位延续：{last_camera}")
    if props:
        bits.append("在场道具：" + "、".join(props[:3]))
    return "【接续上镜】" + "；".join(bits) + "。本镜从这个状态自然接下去，不要重置人物位置与道具位置。"


async def detect_first_frame_conflict(
    brief: str,
    action_text: str,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """首帧与提示词动作是否打架（打架就别接龙，降级独立镜）。

    强模型给的例子：「首帧是正面，提示词要求转身」→ 模型会原地扭动。
    """
    action = str(action_text or "").strip()
    if not action or not brief:
        return {"conflict": False, "reason": "没有可判定的动作"}
    try:
        from core.orchestrator import _get_llm_from_settings
        llm = _get_llm_from_settings(settings)
        prompt = (
            "判断一段视频的**首帧状态**与**后续动作**是否**物理上不可能**。\n"
            f"首帧状态：{brief}\n"
            f"要求动作：{action}\n"
            "\n"
            "规则（**只判物理矛盾，不判姿势不同**）：\n"
            "· 允许：从当前姿势继续变化（蹲→捧起、坐→站、走→停、伸手→握住）、"
            "允许换机位与景别、允许时间微跳。\n"
            "· 判为冲突：首帧里根本没有的东西却被要求出现（如首帧手里空着却要拿起杯子）、"
            "方向互斥（首帧背对镜头却要正对镜头说话）、"
            "位置互斥（首帧在门外却要求已在屋内完成动作）、"
            "首帧是倒地的却要求站起来跑。\n"
            "\n"
            "只回答一个词：自然 或 冲突。"
        )
        raw = await llm.chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=20)
        answer = str(raw or "").strip()
        conflict = "冲突" in answer
        return {"conflict": conflict, "reason": answer[:60]}
    except Exception as e:  # noqa: BLE001
        logger.warning("首帧冲突预检失败（按不冲突处理）：%s", e)
        return {"conflict": False, "reason": "预检不可用"}


def mark_downstream_stale(db: Any, project_id: str, changed_shot_id: str) -> List[str]:
    """上游镜头重生成 → 把**依赖它的下游接龙镜**标记为过期（需重编译/重出）。

    ★ 为什么要：接龙镜的首帧就是上一镜的尾帧。上一镜重画了，下游的接龙首帧就是旧的，
      不重出就会出现"接缝对不上"。这里只**标记**（chain_mode=independent + stale 原因），
      真正重出由上层决定（花钱的事不能自动做）。
    """
    stale: List[str] = []
    shots = sorted(db.list_shots(project_id) or [], key=lambda s: s.get("order_index") or 0)
    ids = [str(s.get("id")) for s in shots]
    if changed_shot_id not in ids:
        return stale
    start = ids.index(changed_shot_id)
    for shot in shots[start + 1:]:
        prompts = stored_prompts_of(shot)
        if str(prompts.get("relay_source_shot_id") or "") != changed_shot_id:
            continue
        prompts["chain_mode"] = "independent"
        prompts["stale_reason"] = f"上一镜 {changed_shot_id[:8]} 重生成，本镜接龙首帧已过期"
        try:
            from core import shot_prompts as _sp
            layer3 = _sp.attach_prompts(shot, prompts)
            db.update_shot(str(shot.get("id")),
                           layer3_constraints=json.dumps(layer3, ensure_ascii=False))
            stale.append(str(shot.get("id")))
        except Exception as e:  # noqa: BLE001
            logger.warning("标记下游过期失败 %s：%s", shot.get("id"), e)
    return stale


def stored_prompts_of(shot: Dict[str, Any]) -> Dict[str, Any]:
    """读一个镜头已存的提示词载荷（没有就空 dict）。"""
    try:
        from core import shot_prompts as _sp
        return _sp.stored_prompts(shot)
    except Exception:  # noqa: BLE001
        return {}


def find_last_frame(db: Any, project_id: str, shot_id: str, output_dir: str = "") -> str:
    """找某一镜的尾帧（跨镜接龙的第一帧来源）。

    优先级：
      ① 本镜提示词载荷里的 `last_frame`（可能是平台 URL，也可能是本地路径）
      ② 本地落盘的 `seg_*_last.jpg`（平台没给尾帧时我们自己抽的）
    都没有就返回空串 ⇒ 该镜退回"独立镜"。
    """
    if not shot_id:
        return ""
    try:
        from core import shot_prompts as _sp
        for shot in db.list_shots(project_id) or []:
            if str(shot.get("id")) != str(shot_id):
                continue
            stored = _sp.stored_prompts(shot)
            value = str(stored.get("last_frame") or "").strip()
            if value:
                return value
            break
    except Exception:  # noqa: BLE001
        pass
    if output_dir:
        return last_frame_path(output_dir, project_id, shot_id)
    return ""
