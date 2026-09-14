"""
VideoForge · 编排器

4 步流程的核心编排（借鉴 Pavo + Skill 手册）：
1. 剧本 → 2. 资产库 → 3. 生成视频 → 4. 后期

主要功能：
- 自定义时长（任意秒数，自动分段拼接）
- 中途换人物/场景/模型
- 多模型适配器调度
- 角色一致性（reference images + IP-Adapter 风格描述）
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
import httpx

from .db import Database
from .task_queue import TaskQueue, register_task, make_progress_updater
from .adapters import VideoGenRequest, VideoGenResult, get_adapter, list_providers
from .llm import LLMClient, ScriptService, AbstractionService


logger = logging.getLogger("videoforge.orchestrator")


# ──────────── 任务处理器 ────────────

@register_task("generate_shot")
async def generate_shot_handler(payload: Dict[str, Any],
                                  progress_callbacks: List) -> Dict[str, Any]:
    """生成分镜视频"""
    from main import get_app_state

    state = get_app_state()
    db: Database = state["db"]
    queue: TaskQueue = state["queue"]

    shot_id = payload["shot_id"]
    num_candidates = payload.get("num_candidates", 4)
    override_provider = payload.get("model_provider")
    override_model = payload.get("model_name")

    update = make_progress_updater(payload.get("_task_id", ""), db, progress_callbacks)

    shot = db.get_shot(shot_id)
    if not shot:
        raise ValueError(f"Shot not found: {shot_id}")

    # 更新 shot 状态
    db.update_shot(shot_id, status="generating", progress=0.0)
    update(0.05, "准备生成")

    # 获取适配器
    provider = override_provider or shot["model_provider"]
    model_name = override_model or shot["model_name"]
    settings = db.get_all_settings()
    api_keys = settings.get("api_keys", {})
    api_key = api_keys.get(provider, "")

    adapter = get_adapter(provider, api_key=api_key, config={"model_name": model_name})

    # 构造请求
    # 角色参考图
    # ⚠ 必须转成 URL / data URI：本地路径 API 不认（这是"配了 Key 也用不了模型"的直接原因）
    from .adapters import to_image_ref
    char_refs = []
    for cid in shot.get("character_ids", []):
        c = db.get_character(cid)
        if c and c.get("reference_image_path"):
            ref = to_image_ref(c["reference_image_path"])
            if ref:
                char_refs.append(ref)

    # 场景参考图
    scene_ref = None
    if shot.get("scene_id"):
        s = db.get_scene(shot["scene_id"])
        if s and s.get("reference_image_path"):
            scene_ref = to_image_ref(s["reference_image_path"])

    # 拼装完整提示词（三层）
    full_prompt = _compose_full_prompt(shot)

    req = VideoGenRequest(
        prompt=full_prompt,
        duration=min(shot["duration_seconds"], adapter.max_duration),
        aspect_ratio=shot["aspect_ratio"],
        resolution=shot["resolution"],
        reference_images=char_refs,
        first_frame=scene_ref,  # 场景图作为首帧
        character_reference=char_refs,
        negative_prompt="\n".join(shot.get("layer3_constraints", {}).get("must_not_appear", [])),
    )

    # 校验
    err = adapter.validate_request(req)
    if err:
        db.update_shot(shot_id, status="failed", error_message=err, progress=0)
        raise ValueError(err)

    update(0.1, f"调用 {adapter.display_name}")

    # 生成 num_candidates 个候选
    candidates = []
    errors: List[str] = []
    output_dir = Path(settings.get("output_dir", "./data/outputs")) / shot["project_id"] / shot_id
    output_dir.mkdir(parents=True, exist_ok=True)

    for i in range(num_candidates):
        try:
            result: VideoGenResult = await adapter.generate(req)
            if not result.success:
                last_err = result.error or "未知错误"
                logger.warning(f"Candidate {i+1} failed: {last_err}")
                errors.append(f"候选{i+1}: {last_err}")
                continue

            # 下载到本地
            local_path = output_dir / f"candidate_{i+1}.mp4"
            await adapter.download_to_local(result.video_url, str(local_path))

            candidates.append({
                "candidate_id": f"cand_{i+1}",
                "video_path": str(local_path),
                "video_url": result.video_url,
                "duration_seconds": result.duration_seconds,
                "is_selected": i == 0,  # 默认选第一个
                "rating": 0,
                "raw": result.raw,
            })
            update(0.1 + 0.85 * (i + 1) / num_candidates, f"候选 {i+1}/{num_candidates}")
        except Exception as e:
            logger.exception(f"Candidate {i+1} error: {e}")

    await adapter.close()

    # 更新 shot
    # 失败时把**真实原因**写进 error_message（此前只写 "All candidates failed"，
    # 用户在界面上完全看不出是缺 Key、模型名错、还是内容审核）
    fail_msg = None
    if not candidates:
        fail_msg = "；".join(errors[:3]) if errors else "全部候选生成失败（无更多信息）"
    db.update_shot(
        shot_id,
        candidates=candidates,
        selected_candidate_id=candidates[0]["candidate_id"] if candidates else None,
        status="completed" if candidates else "failed",
        progress=1.0,
        error_message=fail_msg[:800] if fail_msg else None,
    )

    return {
        "shot_id": shot_id,
        "num_candidates": len(candidates),
        "candidates": candidates,
    }


@register_task("generate_script")
async def generate_script_handler(payload: Dict[str, Any],
                                   progress_callbacks: List) -> Dict[str, Any]:
    """生成剧本（LLM）"""
    from main import get_app_state

    state = get_app_state()
    db: Database = state["db"]
    settings = db.get_all_settings()

    update = make_progress_updater(payload.get("_task_id", ""), db, progress_callbacks)

    llm = _get_llm_from_settings(settings)
    svc = ScriptService(llm)

    update(0.1, "调用 LLM 生成剧本")
    # ★ 2026-09-14：场次数由 `scriptplan` 定，返回后再**强制**把总时长对齐到
    #   用户要的秒数（与 `/api/projects/{pid}/script/generate` 同一条规矩）。
    #   两条入口要是各写一套，就会出现"从这条进对、从那条进不对"的鬼故事。
    from core import scriptplan as _sp
    _want = int(payload.get("total_duration", 30) or 30)
    result = await svc.generate_script(
        user_prompt=payload["user_prompt"],
        total_duration=_want,
        style=payload.get("style", "cinematic"),
        auto_split=True,
        scene_count=_sp.scene_count_for(_want),
    )
    _fit = _sp.fit_scenes(result.get("scenes"), _want)
    if _fit.get("scenes"):
        result["scenes"] = _fit["scenes"]
    result["duration_plan"] = _sp.duration_brief(_fit)
    try:
        _proj = db.get_project(payload["project_id"]) or {}
        _ps = dict(_proj.get("settings") or {})
        _ps["default_total_duration"] = _want
        db.update_project(payload["project_id"], settings=_ps)
    except Exception as e:
        logger.warning("总时长写回项目设置失败：%s", e)

    update(0.8, "生成三层提示词")

    # 为每个场次生成三层提示词
    characters = result.get("characters", [])
    scenes_meta = result.get("scenes_meta", [])

    three_layer = {}
    for scene in result.get("scenes", []):
        scene_id = f"scene_{scene['scene_number']}"
        # 找到匹配的场景 meta
        scene_meta = next((s for s in scenes_meta if s.get("name") == scene.get("title")), None)
        layer = await svc.generate_three_layer_prompt(scene, characters, scene_meta)
        three_layer[scene_id] = layer

    update(0.95, "保存剧本")

    # 保存到 DB
    db.upsert_script(
        project_id=payload["project_id"],
        user_prompt=payload["user_prompt"],
        outline=json.dumps(result, ensure_ascii=False),
        scenes=result.get("scenes", []),
        three_layer_prompts=three_layer,
    )

    return {
        "title": result.get("title"),
        "scenes_count": len(result.get("scenes", [])),
        "three_layer_count": len(three_layer),
    }


@register_task("abstract_copyright")
async def abstract_handler(payload: Dict[str, Any],
                            progress_callbacks: List) -> Dict[str, Any]:
    """版权抽象化"""
    from main import get_app_state

    state = get_app_state()
    db: Database = state["db"]
    settings = db.get_all_settings()

    update = make_progress_updater(payload.get("_task_id", ""), db, progress_callbacks)

    llm = _get_llm_from_settings(settings)
    svc = AbstractionService(llm)

    update(0.2, "分析版权特征")
    result = await svc.abstract(payload["source_description"])

    update(0.7, "生成抽象化设定")

    log_id = db.log_abstraction(
        project_id=payload["project_id"],
        source=payload["source_description"],
        abstracted=result,
        removed=result.get("removed_features", []),
        preserved=result.get("preserved_features", []),
    )

    # 自动创建角色/场景/道具
    for char in result.get("characters", []):
        db.create_character(
            project_id=payload["project_id"],
            name=char.get("abstracted_name", "未命名角色"),
            description=char.get("description", ""),
            costume_main=char.get("costume", ""),
            reference_features={
                "preserved": char.get("preserved_traits", []),
            },
        )

    for scene in result.get("scenes", []):
        db.create_scene(
            project_id=payload["project_id"],
            name=scene.get("abstracted_name", "未命名场景"),
            description=scene.get("description", ""),
        )

    for prop in result.get("props", []):
        db.create_prop(
            project_id=payload["project_id"],
            name=prop.get("abstracted_name", "未命名道具"),
            description=prop.get("description", ""),
        )

    return {
        "log_id": log_id,
        "characters": len(result.get("characters", [])),
        "scenes": len(result.get("scenes", [])),
        "props": len(result.get("props", [])),
    }


# ──────────── 工具函数 ────────────

def _compose_full_prompt(shot: Dict[str, Any]) -> str:
    """把三层提示词拼装成一段完整 prompt"""
    parts = []

    # Layer 1
    if shot.get("layer1_overview"):
        parts.append(shot["layer1_overview"])

    # Layer 2 timeline
    timeline = shot.get("layer2_timeline", [])
    if timeline:
        parts.append("\n分镜时间线：")
        for item in timeline:
            camera = item.get("camera", "static")
            parts.append(
                f"[{item.get('start', 0)}-{item.get('end', 0)}秒] "
                f"{item.get('action', '')} | 表情:{item.get('expression', '')} | "
                f"镜头:{camera} | 道具:{','.join(item.get('props_used', []))}"
            )

    # Layer 3 constraints
    constraints = shot.get("layer3_constraints", {})
    if constraints:
        parts.append("\n约束条件：")
        if constraints.get("must_not_appear"):
            parts.append("不要出现: " + "、".join(constraints["must_not_appear"]))
        if constraints.get("must_not_happen"):
            parts.append("不要发生: " + "、".join(constraints["must_not_happen"]))
        if constraints.get("must_keep"):
            parts.append("必须保持: " + "、".join(constraints["must_keep"]))
        if constraints.get("must_appear"):
            parts.append("必须出现: " + "、".join(constraints["must_appear"]))
        if constraints.get("must_happen"):
            parts.append("必须发生: " + "、".join(constraints["must_happen"]))

    return "\n".join(parts)


def _get_llm_from_settings(settings: Dict[str, Any]) -> LLMClient:
    """根据设置构造 LLM 客户端"""
    if settings.get("enable_llm_local"):
        # 本地 Ollama
        return LLMClient(
            provider="ollama",
            base_url=settings.get("ollama_url", "http://localhost:11434"),
            model=settings.get("ollama_model", "qwen2.5:7b"),
        )
    elif settings.get("remote_llm_key"):
        # 云端
        return LLMClient(
            provider=settings.get("remote_llm_provider", "deepseek"),
            api_key=settings["remote_llm_key"],
            model=settings.get("remote_llm_model", "deepseek-chat"),
        )
    else:
        # 默认 DeepSeek（不验证 key，但用户必须配）
        return LLMClient(
            provider="deepseek",
            api_key=settings.get("remote_llm_key", ""),
        )


async def generate_long_video(
    shot_id: str,
    db: Database,
    queue: TaskQueue,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """长视频生成：自动分段拼接"""
    shot = db.get_shot(shot_id)
    if not shot:
        raise ValueError(f"Shot not found: {shot_id}")

    provider = shot["model_provider"]
    api_key = settings.get("api_keys", {}).get(provider, "")
    adapter = get_adapter(provider, api_key=api_key,
                            config={"model_name": shot["model_name"]})

    total_duration = shot["duration_seconds"]
    chunk_size = adapter.max_duration

    if total_duration <= chunk_size:
        # 单段即可
        return await generate_shot_handler(
            {"shot_id": shot_id, "num_candidates": 4, "_task_id": ""},
            [],
        )

    # 多段：分别生成再拼接
    segments = []
    num_segments = (total_duration + chunk_size - 1) // chunk_size
    for i in range(num_segments):
        start = i * chunk_size
        end = min(start + chunk_size, total_duration)
        sub_duration = end - start

        sub_shot = dict(shot)
        sub_shot["id"] = f"{shot_id}_seg_{i+1}"
        sub_shot["duration_seconds"] = sub_duration
        sub_shot["layer1_overview"] = (
            f"{shot.get('layer1_overview', '')}\n\n"
            f"（第 {i+1}/{num_segments} 段，{sub_duration}秒）"
        )
        # 保存临时 shot
        db.create_shot(**{k: v for k, v in sub_shot.items() if k in [
            "project_id", "scene_id", "order_index", "duration_seconds",
            "layer1_overview", "layer2_timeline", "layer3_constraints",
            "character_ids", "model_provider", "model_name", "aspect_ratio",
            "resolution", "negative_reference_paths",
        ]})
        # 触发单段生成
        ...
        # 实际拼接留到 postprocess
