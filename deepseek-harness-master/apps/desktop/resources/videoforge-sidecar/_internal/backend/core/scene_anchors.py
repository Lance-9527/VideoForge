# -*- coding: utf-8 -*-
"""VideoForge · 场景锚点表（跨镜连贯）

═══════════════════════════════════════════════════════════════
解决什么（2026-10-22，用户第 3 条）
═══════════════════════════════════════════════════════════════
用户原话：「保证分镜之间的连贯性，比如同一个场景，保证在硬切画面前原先角色、道具位置的一致性
（角度可以变，但位置逻辑得一致）」。

现状：每镜各写各的，模型每镜重新编一次空间关系 ⇒ 同一场景里道具会"换边"、
角色会"瞬移"。已有的 `continuity` 模块只管**长镜头分段续接**（同一镜内），
管不到**跨镜**。

做法：给每个场景做一张**锚点表**（角色站位 / 道具方位 / 光源方向），
只做一次（一次 LLM 调用，很便宜），之后**每一镜的提示词都原样注入同一段话**：
    【空间锚点】农夫站在雪堆的北侧、柴担横在身后左肩、晨光从西北方向打来；
    这些空间关系在后续镜头保持不变（机位可以变，位置关系不变）。
提示词里再给一条**禁止句**：
    不要把柴担/角色改到与锚点相反的方位。

检查器再加两条：
    C10 每一镜的 video_prompt 必须含有该场景的锚点句
    C11 提示词里不得出现与锚点相反的方位（左右/前后对调）
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.scene_anchors")

#: 注入提示词的固定句式（每镜原样复用，保证模型看到的是同一套空间关系）
ANCHOR_SENTENCE_PREFIX = "【空间锚点】"

#: 方位对（用于 C11 冲突检查）
_OPPOSITES = {
    "左": "右", "右": "左",
    "前": "后", "后": "前",
    "上": "下", "下": "上",
    "内": "外", "外": "内",
    "里": "外", "外": "里",
    "近": "远", "远": "近",
}

ANCHOR_PROMPT = """你是短剧的空间调度。下面给你一个场景和它要拍的几个镜头。
请输出一份**空间锚点表**（JSON，不要解释文字），规定这个场景里所有东西的固定位置关系，
让不同机位的镜头之间不会穿帮。

要求：
1. "characters"：每个角色的固定位置（相对场景的固定参照物，不要用"画面左侧"这种跟机位走的说法）。
2. "props"：每件关键道具的固定方位（同样用固定参照物描述）。
3. "light"：光源方向与色温（一句话）。
4. "note"：一句话说明这些关系在任何机位下都不变。
5. "forbidden"：数组，列出"绝对不能出现的矛盾"（例如把柴担从左肩换到右肩、把人换到门外）。

场景卡：{scene}

镜头清单：
{shots}

只输出 JSON。"""


def anchors_path(output_dir: str, project_id: str, scene_id: str) -> str:
    """锚点表文件位置（一个场景一张表）。"""
    return str(Path(output_dir) / str(project_id) / "anchors" / f"{scene_id}.json")


def _fallback_anchors(scene: Dict[str, Any]) -> Dict[str, Any]:
    """LLM 不可用时的兜底：从场景卡硬取（不编造，只复述已知事实）。"""
    name = str(scene.get("name") or "场景")
    light = str(scene.get("lighting") or "")
    return {
        "scene_id": str(scene.get("id") or ""),
        "scene_name": name,
        "characters": {},
        "props": {},
        "light": (f"{light}光" if light else ""),
        "note": f"「{name}」里所有物体与人物的位置关系在任何机位下都保持不变。",
        "forbidden": [],
        "source": "fallback",
    }


def _anchor_phrase(value: Any) -> str:
    """把锚点值渲染成一句中文散文。

    ★★ 2026-10-23 修：**不能把 dict 原样 str() 塞进提示词**。
      锚点表里人物的 position/orientation/posture 是 **dict**
      （LLM 返回的结构），原来的 `f"{k}在{v}"` 会把整个 Python 字面量
      写进提示词：

          【空间锚点】阿岚在{'position': '坐在书桌正前方…', 'orientation': …}

      实测：这种非自然语言文本会被平台侧的内容/参数校验判成
      `invalid_request`，**视频直接生成失败**（网关侧原文：
      `{'code':'invalid_request','message':'生成失败，请稍后重试'}`）。
      所以这里把 dict 拆成「方位 + 朝向 + 姿态」的正常中文。
    """
    if isinstance(value, dict):
        order = ("position", "orientation", "posture", "direction", "distance")
        bits: List[str] = []
        for key in order:
            v = value.get(key)
            if v is None:
                continue
            s = str(v).strip()
            if not s:
                continue
            label = {
                "position": "", "orientation": "朝向", "posture": "姿态为",
                "direction": "朝向", "distance": "距离",
            }.get(key, key)
            # ★ value 文本本身常已带"位于""朝向前"等词 ⇒ 别再叠一个介词
            #   （叠出来是"位于位于书桌右侧"，读着别扭，模型也容易理解错）。
            if label and not s.startswith(label):
                s = f"{label}{s}"
            bits.append(s)
        for key, v in value.items():
            if key in order:
                continue
            s = str(v).strip()
            if s:
                bits.append(s)
        return ("，" + "，".join(bits)) if bits else ""
    s = str(value or "").strip()
    if not s:
        return ""
    return s if s.startswith("位于") else ("位于" + s)


def render_anchor_sentence(anchors: Dict[str, Any]) -> str:
    """把锚点表渲染成提示词里那一段固定句。"""
    if not anchors:
        return ""
    bits: List[str] = []
    chars = anchors.get("characters") or {}
    if isinstance(chars, dict) and chars:
        bits.append("；".join(f"{k}{_anchor_phrase(v)}" for k, v in chars.items()))
    props = anchors.get("props") or {}
    if isinstance(props, dict) and props:
        bits.append("；".join(f"{k}{_anchor_phrase(v)}" for k, v in props.items()))
    light = str(anchors.get("light") or "")
    if light:
        bits.append(light)
    note = str(anchors.get("note") or "")
    body = "，".join(b for b in bits if b)
    if not body:
        return ""
    tail = f"；{note}" if note else ""
    return f"{ANCHOR_SENTENCE_PREFIX}{body}{tail}。这些空间关系在后续镜头保持不变（机位可以变，位置关系不变）。"


def render_forbidden_note(anchors: Dict[str, Any]) -> str:
    """禁止句（拼进 negative_prompt）。"""
    forbidden = anchors.get("forbidden") or []
    if not forbidden:
        return ""
    return "不要" + "；不要".join(str(f) for f in forbidden) + "。"


def load_anchors(output_dir: str, project_id: str, scene_id: str) -> Dict[str, Any]:
    """读锚点表（没有就空 dict）。"""
    if not scene_id:
        return {}
    path = anchors_path(output_dir, project_id, scene_id)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _direction_of(text: str, name: str, window: int) -> str:
    """取「对象名后面 window 字以内」的方位词（取不到就空串）。"""
    pattern = re.escape(str(name)) + r"[^。；\n]{0,%d}" % window
    match = re.search(pattern, text or "")
    if not match:
        return ""
    m = re.search(r"(左|右|前|后|上|下|内|外|里|近|远)", match.group(0))
    return m.group(1) if m else ""


def find_conflicts(anchors: Dict[str, Any], video_prompt: str) -> List[str]:
    """C11：提示词里是否出现了与锚点相反的方位（**对象绑定**，不误报别人）。"""
    conflicts: List[str] = []
    if not anchors:
        return conflicts
    props = anchors.get("props") or {}
    chars = anchors.get("characters") or {}
    if not isinstance(props, dict):
        props = {}
    if not isinstance(chars, dict):
        chars = {}
    if not props and not chars:
        return conflicts

    # ① 去掉锚点句本身（它是事实来源，不能被自己判违规）
    body = re.sub(r"【空间锚点】[^。]*。", "", video_prompt or "")

    for table in (props, chars):
        for name, position in table.items():
            # ② 锚点里该对象的方位（必须紧跟名字，否则不做判定）
            word = _direction_of(str(position or ""), str(name), 14)
            if not word:
                continue
            opposite = _OPPOSITES.get(word)
            if not opposite:
                continue
            # ③ 提示词正文里，该对象名附近出现了反向方位
            if _direction_of(body, str(name), 6) == opposite:
                conflicts.append(
                    f"「{name}」在锚点里是「{word}」，提示词里却写成「{opposite}」")
    return conflicts


async def build_scene_anchors(
    db: Any,
    scene: Dict[str, Any],
    shots: List[Dict[str, Any]],
    settings: Dict[str, Any],
    *,
    output_dir: str,
    force: bool = False,
) -> Dict[str, Any]:
    """给一个场景生成锚点表（一次 LLM 调用；失败就用兜底）。"""
    scene_id = str(scene.get("id") or "")
    project_id = str(scene.get("project_id") or "")
    if not scene_id:
        return {}
    target = anchors_path(output_dir, project_id, scene_id)
    if os.path.isfile(target) and not force:
        return load_anchors(output_dir, project_id, scene_id)

    shot_text = []
    for i, s in enumerate(shots):
        shot_text.append(
            f"镜{i + 1}：{(s.get('layer1_overview') or '')}\n"
            f"    镜头：{(s.get('layer2_timeline') or '')}"
        )
    prompt = ANCHOR_PROMPT.format(scene=json.dumps(scene, ensure_ascii=False)[:1200],
                                   shots="\n".join(shot_text)[:2000])

    data: Optional[Dict[str, Any]] = None
    for attempt in range(2):
        try:
            from core.orchestrator import _get_llm_from_settings
            from core.llm import extract_json

            llm = _get_llm_from_settings(settings)
            raw = await llm.chat([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=2000)
            data = extract_json(raw) if raw else None
            # ★ LLMClient 没有 close() 方法（旧代码在这里抛异常，把已经拿到的结果也丢了）
            if isinstance(data, dict) and data:
                break
            logger.warning("场景锚点表 JSON 不可用（第 %d 次），重试", attempt + 1)
        except Exception as e:  # noqa: BLE001
            logger.warning("场景锚点表生成失败（第 %d 次）：%s", attempt + 1, e)
            data = None

    if not isinstance(data, dict) or not data:
        data = _fallback_anchors(scene)
    else:
        data.setdefault("scene_id", scene_id)
        data.setdefault("scene_name", str(scene.get("name") or ""))
        data.setdefault("source", "llm")

    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    logger.info("场景锚点表已生成：%s（%s）", target, data.get("source"))
    return data
