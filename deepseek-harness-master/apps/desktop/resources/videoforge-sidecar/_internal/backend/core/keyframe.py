# -*- coding: utf-8 -*-
"""VideoForge · 写实关键帧（白膜 → 关键帧 → 成片）

═══════════════════════════════════════════════════════════════
解决什么（2026-10-22 实测 + 对照实验得出）
═══════════════════════════════════════════════════════════════
用户的原话：「白膜预演视频我没看到，但我看了照片，感觉跟你给的 480p 视频不一致，你得做到一致」。

实测事实（都有日志/截图佐证）：
  ① 白膜首帧**确实**作为 `first_frame` 发给了平台；
  ② 但适配器里有一条硬规则（core/adapters/seedance.py:150-158）：
       有角色/场景参考图 → 首帧被降级成"又一张 reference_image"（锁长相，不锁构图）
       没有参考图       → 首帧才以 `role=first_frame` 发出（**锁构图**）
  ③ 对照实验（只把参考图去掉，其他不变）：
       成片首帧 = 白膜首帧，构图 100% 一致 ⇒ **平台严格认 role=first_frame**。
  ④ 但那一版成片**也是灰模** ⇒ 首帧被照搬，画面没有重绘。

⇒ 结论：**构图靠 first_frame 能锁死；写实靠关键帧**。
   所以中间必须加一步：把白膜灰模**重绘成写实关键帧**
   （构图取自白膜，长相/氛围取自角色图与场景图），再拿关键帧当首帧出片。

这一步只花**一张图**的钱（硅基流动图像编辑），比多抽一次视频便宜得多，
而且把「白膜 = 用户点头的构图」这件事真正落到了成片上。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.keyframe")

#: 关键帧提示词模板：把"保持构图"这件事说死
KEYFRAME_PROMPT = (
    "这是短片的**首帧定妆图**：按下面的画面设定生成一张写实电影剧照。"
    "构图、机位、景别、人物站位与道具位置都要明确，"
    "画面干净、无任何文字与水印（招牌也不要出现字）。"
)


def keyframe_path(output_dir: str, project_id: str, shot_id: str) -> str:
    """关键帧的固定存放位置（按 shot_id 命名，便于复用与覆盖）。"""
    return str(Path(output_dir) / str(project_id) / "keyframe" / f"{shot_id}.png")


def _data_uri(path: str) -> str:
    """本地图片 → data URI（平台只吃 http(s) 或 data URI）。"""
    try:
        from core.adapters import to_image_ref
        return to_image_ref(path) or ""
    except Exception:  # noqa: BLE001
        return ""


def _collect_refs(db: Any, shot: Dict[str, Any]) -> List[str]:
    """收集「场景图 + 角色图」作为重绘参考（顺序：场景在前，人物在后）。"""
    refs: List[str] = []
    scene_id = str(shot.get("scene_id") or "")
    if scene_id:
        try:
            scene = db.get_scene(scene_id)
        except Exception:  # noqa: BLE001
            scene = None
        if scene and scene.get("reference_image_path"):
            refs.append(str(scene["reference_image_path"]))
    for cid in (shot.get("character_ids") or []):
        try:
            char = db.get_character(str(cid))
        except Exception:  # noqa: BLE001
            char = None
        if not char or not char.get("reference_image_path"):
            continue
        # ★ 只出声不出镜的角色（旁白等）**不能**当视觉参考 ——
        #   实测：把旁白的灰色人偶图喂进去，图像模型会真的画一个人站在街上。
        try:
            from core.shot_prompts import is_voice_only
            if is_voice_only(char):
                continue
        except Exception:  # noqa: BLE001
            pass
        refs.append(str(char["reference_image_path"]))
    return refs


async def build_keyframe(
    db: Any,
    shot: Dict[str, Any],
    settings: Dict[str, Any],
    *,
    output_dir: str,
    prompt: str = "",
    use_extra_refs: bool = False,
) -> Dict[str, Any]:
    """把白膜首帧重绘成写实关键帧。

    @returns `{ok, path, refs, error}`
    """
    previz = str(shot.get("previz_path") or "").strip()
    if not prompt:
        # 没给提示词就用镜头的概述兜底（不该发生：出片前一定会先编译提示词）
        prompt = KEYFRAME_PROMPT + "\n" + str(shot.get("layer1_overview") or "")
    provider = str(settings.get("image_provider") or "siliconflow")
    model = str(settings.get("image_model") or "")
    # ★ key 可能在三个桶里的任何一个（后端 main.py 也是三桶都查的）：
    #   api_keys（视频/图像）· llm_api_keys（文本）· tts_api_keys（语音/图像兼用）
    api_key = ""
    for bucket in ("api_keys", "llm_api_keys", "tts_api_keys"):
        api_key = str((settings.get(bucket) or {}).get(provider) or "")
        if api_key:
            break
    if api_key == "":
        return {"ok": False, "error": f"没有配 {provider} 的 API Key，跳过关键帧"}

    # ★ 关键帧的素材：**角色图 + 场景图**（保长相与氛围），
    #   构图由提示词（分镜规格）决定；白膜灰模**不再**当参考图
    #   （实测图像模型保不住它的布局，喂了反而跑偏）。
    refs = _collect_refs(db, shot) if use_extra_refs else []

    from core import imagegen

    target = keyframe_path(output_dir, str(shot.get("project_id") or ""), str(shot.get("id") or ""))
    os.makedirs(os.path.dirname(target), exist_ok=True)

    try:
        result = await imagegen.generate_image(
            provider=provider,
            api_key=api_key,
            prompt=prompt,
            out_dir=os.path.dirname(target),
            size="1280x720",
            model=model,
            reference_images=(refs[:2] if use_extra_refs else []),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("关键帧生成异常：%s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("error") or "关键帧生成失败")}

    produced = str(result.get("path") or "")
    if produced and produced != target:
        try:
            os.replace(produced, target)
        except Exception:  # noqa: BLE001
            target = produced
    logger.info("关键帧已生成：%s（参考：白膜首帧 + %d 张参考图）", target, len(refs))
    return {"ok": True, "path": target, "refs": (refs[:2] if use_extra_refs else [])}


def existing_keyframe(output_dir: str, shot: Dict[str, Any]) -> str:
    """取已有关键帧（没有就空串）。"""
    path = keyframe_path(output_dir, str(shot.get("project_id") or ""), str(shot.get("id") or ""))
    return path if os.path.isfile(path) else ""
