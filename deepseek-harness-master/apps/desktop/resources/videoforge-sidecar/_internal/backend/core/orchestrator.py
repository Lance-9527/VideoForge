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

    # ★★ 白膜注入 —— 这是"白膜流程"的最后一公里。
    #   在此之前：render_previz 渲了白膜、review_previz 让用户确认了，
    #   然后生成时**一个 previz 都没提** —— 白膜躺在硬盘上没人看，
    #   有白膜和没白膜的生成结果完全一样。
    #   现在把白膜真正作为控制信号发出去（不支持的平台会如实降级说明）。
    previz_info: Dict[str, Any] = {}
    try:
        from . import previz_ref as _pr
        # ★ `settings` 必须传：白膜要作为参考**视频**发出去时，平台只吃公网 URL，
        #   而"公网 URL 从哪来"完全由设置决定（public_base_url / 命名隧道 /
        #   临时隧道）。不传的话用户配好的固定域名一个都用不上，
        #   白膜会**静默降级**成"只把运镜写进提示词"。
        ref = _pr.decide(shot, provider, settings=settings)
        previz_info = ref.to_dict()
        if ref.usable and ref.video_ref:
            logger.info("[%s] 白膜注入：mode=%s", shot_id, ref.mode)
    except Exception as e:
        logger.warning("白膜决策失败（按不带白膜继续）：%s", e)
        ref = None

    # ★ 可灵 Element：角色一致性**不是**发参考图，而是引用已注册的 element_id。
    #   这里把分镜关联角色的 element_id 取出来。没有它，可灵这条线即使
    #   `supports_character_ref=True` 也发不出人物一致性（适配器会明确报错，
    #   不会静默把图片塞进 element_id）。
    element_ids: List[str] = []
    if provider == "kling":
        try:
            from .kling_element import element_ids_for_shots
            element_ids = await element_ids_for_shots(db, shot) or []
        except Exception as e:
            logger.warning("取可灵 element_id 失败（不影响其它模型）：%s", e)

    req = VideoGenRequest(
        prompt=full_prompt,
        duration=min(shot["duration_seconds"], adapter.max_duration),
        aspect_ratio=shot["aspect_ratio"],
        resolution=shot["resolution"],
        reference_images=char_refs,
        first_frame=scene_ref,  # 场景图作为首帧
        character_reference=char_refs,
        negative_prompt="\n".join(shot.get("layer3_constraints", {}).get("must_not_appear", [])),
        extra=({"element_ids": element_ids} if element_ids else {}),
    )

    # 把白膜决策落到请求上（追加 prompt 说明 / 追加参考视频 / 处理互斥）
    if ref is not None:
        try:
            did = _pr.apply_to_request(req, ref)
            previz_info["applied"] = did
            if did.get("added_video_ref"):
                logger.info("[%s] 白膜已作为参考素材附加", shot_id)
            if did.get("dropped_first_frame"):
                # 参考素材与首帧互斥，丢掉首帧必须说清楚（否则人物/场景锚点悄悄没了）
                previz_info.setdefault("warnings", []).append(
                    "为了让白膜作为参考素材发出去，本次没有使用首帧图"
                    "（平台把「首帧」和「参考素材」列为互斥场景）。"
                    "场景一致性改由参考图承担。")
        except Exception as e:
            logger.warning("白膜注入失败（按不带白膜继续）：%s", e)

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

    from core.reference_delivery import (finalize_reference_candidate, prepare_reference_spend,
                                          record_reference_spend)
    rejected_candidates = []
    for i in range(num_candidates):
        try:
            from core.previz_ref import require_reference_quality
            _reference_quality = require_reference_quality(shot, provider, model_name, settings)
            if _reference_quality is not None and not getattr(req, "video_url", None):
                raise ValueError("白膜质量通过但参考视频未实际加入请求，已停止付费生成")
            _spend = prepare_reference_spend(db, shot, req.duration, req.resolution,
                                             provider=provider, model_name=model_name)
            result: VideoGenResult = await adapter.generate(req)
            if not result.success:
                last_err = result.error or "未知错误"
                logger.warning(f"Candidate {i+1} failed: {last_err}")
                errors.append(f"候选{i+1}: {last_err}")
                continue

            _charge = record_reference_spend(db, shot, _spend, result.task_id or "")
            # 下载到本地；付费成功即记账，下载/验收失败也不抹掉费用。
            local_path = output_dir / f"candidate_{i+1}.mp4"
            await adapter.download_to_local(result.video_url, str(local_path))

            _delivery = None
            if _reference_quality is not None:
                from core.previz_ref import white_model_video
                _delivery = await finalize_reference_candidate(
                    str(local_path), white_model_video(shot), float(shot.get("duration_seconds") or req.duration),
                    aspect_ratio=shot.get("aspect_ratio") or "16:9", silent=not req.with_audio,
                    source_metadata={"provider": provider, "model": model_name,
                                     "task_id": result.task_id or "", "charge": _charge})
                if not _delivery.get("ok"):
                    rejected_candidates.append({"candidate_id": "rejected_" + __import__('uuid').uuid4().hex,
                        "path": str(local_path), "video_path": str(local_path), "is_selected": False,
                        "source": "seedance", "provider": provider, "model": model_name,
                        "task_id": result.task_id or "", "validation_status": "rejected", "delivery": _delivery})
                    errors.append(_delivery.get("error") or "白膜一致性验收未通过")
                    break  # 拒片不自动重抽；原片和费用证据保留。
                local_path = Path(_delivery["path"])
            candidates.append({
                "candidate_id": f"cand_{i+1}",
                "video_path": str(local_path),
                "video_url": result.video_url,
                "duration_seconds": (_delivery or {}).get("duration") or result.duration_seconds,
                "delivery": _delivery or {}, "provider": provider, "model": model_name,
                "task_id": result.task_id or "", "validation_status": "verified" if _delivery else "not_requested",
                "is_selected": not candidates,  # 默认选第一个
                "rating": 0,
                "raw": result.raw,
            })
            update(0.1 + 0.85 * (i + 1) / num_candidates, f"候选 {i+1}/{num_candidates}")
        except Exception as e:
            errors.append(f"候选{i+1}: {e}")
            logger.exception(f"Candidate {i+1} error: {e}")

    await adapter.close()

    # 更新 shot
    # 失败时把**真实原因**写进 error_message（此前只写 "All candidates failed"，
    # 用户在界面上完全看不出是缺 Key、模型名错、还是内容审核）
    fail_msg = None
    if not candidates:
        fail_msg = "；".join(errors[:3]) if errors else "全部候选生成失败（无更多信息）"
    if rejected_candidates:
        _old = (db.get_shot(shot_id) or {}).get("candidates") or []
        if isinstance(_old, str):
            try: _old = json.loads(_old)
            except Exception: _old = []
        db.update_shot(shot_id, candidates=(list(_old) if isinstance(_old, list) else []) + rejected_candidates,
                       status="failed", progress=0.0, error_message=(fail_msg or "一致性未通过")[:800])
    else:
        db.update_shot(
            shot_id, candidates=candidates,
            selected_candidate_id=candidates[0]["candidate_id"] if candidates else None,
            status="completed" if candidates else "failed", progress=1.0 if candidates else 0.0,
            error_message=fail_msg[:800] if fail_msg else None)

    return {
        "shot_id": shot_id,
        "num_candidates": len(candidates),
        "candidates": candidates, "rejected_candidates": rejected_candidates,
        "ok": bool(candidates) and not rejected_candidates,
        "requires_user_review": bool(rejected_candidates), "no_fallback": bool(rejected_candidates),
        # 白膜这一档到底带没带上、以什么方式带的 —— 让前端/agent 能如实告诉用户。
        # 不说的话用户没法判断"我确认过的白膜到底起作用了没"。
        "previz": previz_info,
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
    # ★ 2026-10-20：用户明确给了几场/几个镜头就**听用户的**。
    #   原来这里无条件用 `scene_count_for(_want)` 反推 —— 用户说「2 个镜头」
    #   也会被重算成别的场次数。
    _want_scenes = payload.get("scene_count")
    try:
        _want_scenes = int(_want_scenes) if _want_scenes else None
    except Exception:
        _want_scenes = None
    # ★ 2026-10-23：注入 settings（用户手册/提示词规范）与用户原话意图。
    try:
        from core.db import db as _dbx  # type: ignore
        _settings_x = _dbx.get_all_settings() or {}
    except Exception:  # noqa: BLE001
        _settings_x = {}
    _intent_x = str(payload.get("user_prompt") or "").strip()
    try:
        _pid_x = str(payload.get("project_id") or "")
        if _pid_x and not _intent_x:
            _proj_x = _dbx.get_project(_pid_x) or {}
            _intent_x = (str(_proj_x.get("description") or "").strip()
                         or str(_proj_x.get("title") or "").strip())
    except Exception:  # noqa: BLE001
        pass
    result = await svc.generate_script(
        user_prompt=payload["user_prompt"],
        total_duration=_want,
        style=payload.get("style", "cinematic"),
        auto_split=True,
        scene_count=_want_scenes or _sp.scene_count_for(_want),
        settings=_settings_x,
        intent=_intent_x,
    )
    # ★ 2026-10-20：`fit_scenes` 用 `scene_count_for(total)` 当**场次上限**
    #   （10 秒 → 1 场），于是用户明确要「2 个镜头」时第 2 场会被当成
    #   "时长放不下"丢掉。用户给了场次数就把上限抬到用户的数。
    _fit_kw = {}
    if _want_scenes:
        # ★ 光抬 cap 不够：`scene_count_for` 是**先按 PREFERRED_SCENE_SECONDS(12s)
        #   估场次数**再夹上限的 —— 10 秒照样估成 1 场，cap=2 也变不出第 2 场。
        #   要让"用户说几场就是几场"，得把 preferred 设成 `总时长/场次`，
        #   这样 round(total/preferred) == 用户要的场次数。
        _fit_kw["cap"] = max(int(_want_scenes), 1)
        _fit_kw["preferred"] = max(1, int(round(float(_want) / max(1, int(_want_scenes)))))
    # ★ 2026-10-24：把节奏档传给加权分配（钩子短/高潮长/每场不平均）
    try:
        _pacing_o = _sp.pacing_for(
            style=payload.get("style"), intent=_intent_x,
            explicit=str((_settings_x or {}).get("pacing") or ""))
    except Exception:  # noqa: BLE001
        _pacing_o = None
    _fit = _sp.fit_scenes(result.get("scenes"), _want, pacing=_pacing_o, **_fit_kw)
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
    """把三层提示词拼装成一段完整 prompt。

    ★★ 2026-09-23 修两处：

    ① **运镜原来写的是英文枚举**：`镜头:dolly_in`。视频模型（尤其国产那几家）
       对 `dolly_in` 这种内部标识符基本没有理解，等于**白写**。
       现在翻成中文（"镜头缓慢推近"）。
    ② **白膜里定好的运镜没进 prompt**。用户在对话里确认白膜时看的就是那个运镜，
       但生成时这里完全没读 `previz_camera` —— 确认过的信息丢了。
       现在：白膜已确认时，**以它为准**（那才是用户点头的那个），
       而不是用分镜表里 agent 随手写的默认值。
    """
    # ★ 2026-10-21（用户第 5 条）：分镜阶段编译好的「三段式视频提示词」优先。
    #   它是按 画面质感→场景→主体(角色锚定)→镜头→动作→构图→禁文字 的顺序拼的专业 prompt，
    #   比下面用一句话概述 + 枚举运镜拼出来的强得多。没有就用旧路径（旧项目照常能出片）。
    try:
        from core import shot_prompts as _sp
        _stored = _sp.stored_prompts(shot)
        _video = str(_stored.get("video_prompt") or "").strip()
        if _video:
            _scene = str(_stored.get("scene_anchor") or "").strip()
            if _scene and _scene not in _video:
                _video = _video + "　" + _scene + "。"
            return _video
    except Exception:  # noqa: BLE001
        pass

    parts = []

    # Layer 1
    if shot.get("layer1_overview"):
        parts.append(shot["layer1_overview"])

    # ★ 白膜确认过的运镜优先。白膜里的 `previz_camera` 是**受控枚举**
    #   （dolly_in / pan_left …），翻成中文才是干净的一句运镜指令。
    cam_text = ""
    cam_src = ""
    try:
        from core import previz as _pv
        pc = (shot.get("previz_camera") or "").strip()
        pst = (shot.get("previz_status") or "").strip()
        if pc and pst in ("draft_approved", "final_ready"):
            cam_text = _pv.LABELS_CAMERA.get(pc, "")
            cam_src = "白膜已确认"
    except Exception:
        pass

    # Layer 2 timeline
    timeline = shot.get("layer2_timeline", [])
    has_enum_cam = False       # 时间线里有没有"机器填的枚举运镜"
    if timeline:
        from core import previz as _pv
        parts.append("\n分镜时间线：")
        cams: List[str] = []
        for item in timeline:
            # ══════════════════════════════════════════════════════════
            # ★★ 2026-09-26 加固：**时间线条目不保证是字典**
            #
            # 本仓记录过这个形状（`core/acting.py` 的 `enrich_timeline()` 专门
            # 为它加了计数与告警）：
            #     上游偶尔会把时间线写成**字符串数组**（`["他抬头", "他握枪"]`）
            # 而 `enrich_timeline()` 对这类条目是**原样透传**的，
            # `dialogue.normalize_timeline()` 也只在"规范化真的跑过"时才把它
            # 转成 `{"action": ...}`。也就是说：**库里真的可能存着字符串条目**。
            #
            # 原来这里直接 `item.get("camera")` → AttributeError。而本函数的
            # 调用点是 `core/longvideo.py:346` 的 `base_prompt = _compose_full_prompt(shot)`
            # —— **那一行没有 try**。于是：
            #     一条坏时间线条目 → 整条长视频生成失败
            # 而长视频正是 `generate_final`（1080P 成片）的**主路径**
            # （见 `core/film.py` 里 `collect_clips()` 的说明）。
            #
            # 坏条目**跳过并留痕**，比整条生成挂掉好得多 —— 和
            # `acting.enrich_timeline()` 的处理保持一致。
            # ══════════════════════════════════════════════════════════
            if not isinstance(item, dict):
                logger.warning(
                    "分镜时间线里有非字典条目（%s），拼提示词时已跳过：%r",
                    type(item).__name__, str(item)[:60])
                continue
            raw_cam = str(item.get("camera") or "").strip()
            if raw_cam in _pv.LABELS_CAMERA:
                has_enum_cam = True
            # 描述型（"低角度特写，展示地图绘制过程"）原样保留 ——
            # 它本身就是完整指令，翻译或摘要都只会损失信息。
            cam = _pv.LABELS_CAMERA.get(raw_cam, raw_cam or "固定机位")
            cams.append(cam)
            # ★ `props_used` 可能是 `None`（LLM 输出里常见）或混着非字符串 ——
            #   原来直接 `','.join(item.get('props_used', []))`：
            #   `.get(k, [])` 只在**键不存在**时给默认值，键存在而值是 None
            #   时它原样返回 None → `join(None)` → TypeError，同样会炸整条生成。
            _props = item.get("props_used")
            _props = _props if isinstance(_props, list) else []
            parts.append(
                f"[{item.get('start', 0)}-{item.get('end', 0)}秒] "
                f"{item.get('action', '')} | 表情:{item.get('expression', '')} | "
                f"镜头:{cam} | 道具:{','.join(str(x) for x in _props)}"
            )
        multi_cam = len({c for c in cams if c}) > 1
    else:
        multi_cam = False

    # 只有这两种情况才补一句运镜总结：
    #   ① 白膜确认过一个**受控枚举**运镜 —— 那是用户点头的那个，最有权威
    #   ② 时间线里的运镜是**枚举**（说明是机器填的，光一个 `dolly_in`
    #      模型看不懂，需要翻成人话）
    #
    # ★ 实测：真实库里 179 条 camera 值**全是**自然语言描述（0 条枚举）。
    #   所以对绝大多数镜头这里**一句都不加** —— 加了只会和时间线里的
    #   镜头标注重复，白白稀释 prompt 的信息密度。
    if cam_text:
        if multi_cam and cam_src != "白膜已确认":
            parts.append("\n【运镜】本段有多次镜头切换，以时间线里逐段标注的镜头为准。")
        else:
            parts.append(f"\n【运镜】{cam_text}（来源：{cam_src}）。"
                         f"整段保持这个运镜，中途不要换机位。")
    elif has_enum_cam and multi_cam:
        parts.append("\n【运镜】本段有多次镜头切换，以时间线里逐段标注的镜头为准。")

    # ══════════════════════════════════════════════════════════════
    # ★★ 微表情表演（biaoqing-yanyi skill）—— 2026-09-26 补
    #
    # 这个函数是**长视频路径**在用的提示词构造器
    # （`core/longvideo.py:297` 里 `from core.orchestrator import _compose_full_prompt`）。
    # 它和 `videoprompt.build_video_prompt` 是**两套并行的构造器**：
    # 前者拼结构化清单，后者拼连续镜头描述。
    #
    # 之前只给 `build_video_prompt` 接了微表情，于是：
    #   · 单镜生成（main.py 的 api/分段路径）→ 有完整表演结构
    #   · **长视频生成（本函数）→ 只有 `表情:xxx` 一行**
    # 也就是同一个"微表情没送到模型"的问题，在长视频这条路上**依然存在**。
    # 只修一条路就是半个修复 —— 这是本轮顺着"谁在读这份 timeline"查出来的。
    #
    # `performance_prompt_lines` 自带兜底：库里已有的老数据只有 `expression`
    # 时也会把它送出去，所以这里对历史分镜同样有效。
    # ══════════════════════════════════════════════════════════════
    try:
        from core.acting import performance_prompt_lines as _ppl
        _perf = _ppl(timeline)
        if _perf:
            parts.append("\n" + _perf)
    except Exception as _e:  # noqa: BLE001
        # 表演拼不出来不该毁掉整条提示词，但要留痕（静默失败最难查）
        logger.warning("拼装微表情表演失败（本镜提示词将不含表演指令）：%s", _e)

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
    """根据设置构造 LLM 客户端。

    ★ 2026-09-24 修一个真事故：这里原来只读**老字段名**
      （`remote_llm_key` / `remote_llm_provider` / `remote_llm_model`），
      而界面和数据库用的是新名字（`llm_provider` / `llm_model` / `llm_api_keys`）。
      在用户的真库里老名字**一个都不存在**，于是每次都落到最后的 else 分支：

          LLMClient(provider="deepseek", api_key="")

      —— 用户明明配了 MiniMax，抽取资产 / 生成剧本却一直在用
      DeepSeek 加一把空 Key。失败信息也指不到真正的原因。

      现在统一走 `resolve_llm_settings`（新旧字段名都认、模型名为空时
      回落到该厂商自己的默认模型）。
    """
    from .chat import resolve_llm_settings
    r = resolve_llm_settings(settings)
    base_url = ""
    if r["provider"] == "ollama":
        base_url = (settings.get("ollama_url")
                    or settings.get("ollama_base_url")
                    or "http://localhost:11434")
    return LLMClient(provider=r["provider"], api_key=r["api_key"],
                     model=r["model"], base_url=base_url)


async def generate_long_video(
    shot_id: str,
    db: Database,
    queue: TaskQueue,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """长视频生成：按三档策略分段，再交给后期拼接。

    ★ 2026-09-23 重写。原来这个函数只建了几条 sub_shot 记录，然后写了个
      `...` 就结束了 —— 既没生成、也没拼接，是个死 stub。现在接的是
      `core.longvideo`（排片/续接/复用/抽尾帧都在那，有单测）。

    为什么分档（而不是一律按 max_duration 切）：
      · 目标 ≤ 上限     → 一次出完。很多人明明只要 12 秒却按 5 秒切 3 段，
                          白多两个接缝。
      · 目标 ≤ 上限×3   → 首尾帧续接，运动连贯。
      · 更长            → 在分镜切点断开重锚。一路续到底会"退化累积"
                          （每段都是有损重建，12 段后主体/色调必然漂移）。
    """
    shot = db.get_shot(shot_id)
    if not shot:
        raise ValueError(f"Shot not found: {shot_id}")

    from . import longvideo as _lv

    provider = shot["model_provider"]
    # ★ 原来只读 settings["api_keys"]；但真实库里视频 key 就在这个字段下
    #   （seedance/hailuo 都在），保持原样，只是把 longvideo 需要的都传进去。
    api_key = (settings.get("api_keys") or {}).get(provider, "")
    adapter = get_adapter(provider, api_key=api_key,
                          config={"model_name": shot["model_name"]})

    total = float(shot.get("duration_seconds") or 0)
    strategy = _lv.choose_strategy(total, adapter.max_duration)

    def _prog(p: float, msg: str) -> None:
        try:
            db.update_shot(shot_id, progress=round(p, 3))
        except Exception:
            pass
        logger.info("[%s] %.0f%% %s", shot_id, p * 100, msg)

    out_dir = str(Path(settings.get("output_dir", "./data/outputs"))
                  / shot["project_id"] / shot_id)

    # 复用上次成功的段（只重跑失败的），这是"抽卡抽到第 7 段发现第 2 段不行"的救命开关
    prior = shot.get("segments")
    if isinstance(prior, str):
        try:
            prior = json.loads(prior)
        except Exception:
            prior = []
    prior = prior or []

    db.update_shot(shot_id, status="generating", progress=0.01)

    # ★ 负面约束：原先是只有 `must_not_appear`。
    #   这里补上微表情 skill 的必带负面限制（不脸崩 / 不夸张开合 / 不多人 /
    #   不烧字幕），与 `videoprompt.build_video_prompt` 保持一致 ——
    #   否则**长视频那条路的负面约束比单镜那条路弱**，
    #   同一部片子里两种镜头的"脸崩风险"就不一样了。
    neg_items = [str(x) for x in
                 ((shot.get("layer3_constraints") or {}).get("must_not_appear") or [])]
    try:
        from core.acting import performance_negatives as _pn
        for _x in _pn(shot.get("layer2_timeline")):
            if _x and _x not in neg_items:
                neg_items.append(_x)
    except Exception as _e:  # noqa: BLE001
        logger.warning("取微表情负面限制失败（用原负面词继续）：%s", _e)
    neg = "\n".join(neg_items)

    res = await _lv.generate_long_video(
        shot, adapter, db=db, out_dir=out_dir,
        strategy=strategy,
        anchor_mode="shot",
        negative_prompt=neg,
        prior_segments=prior,
        progress=_prog,
        settings=settings,
    )
    await adapter.close()

    db.update_shot(
        shot_id,
        segments=res.get("segments") or [],
        status="completed" if res.get("ok") else "failed",
        progress=1.0 if res.get("ok") else 0.0,
        error_message=None if res.get("ok") else str(res.get("error") or "")[:800],
    )

    return {
        "shot_id": shot_id,
        "strategy": res.get("strategy"),
        "segment_count": len(res.get("plan") or []),
        "reused": res.get("reused", 0),
        "clips": res.get("clips") or [],
        "ok": bool(res.get("ok")),
        "error": res.get("error"),
        "segments": res.get("segments") or [],
        # ★★ `previz` 必须透出去（2026-09-27 第 70 轮）。
        #   `longvideo` 算了"白膜到底带没带上、以什么方式带的"，
        #   `agent_tools_production.t_generate_final` 也一直在读
        #   `res.get("previz")` —— 但**这个键从来没被放进来过**，
        #   所以用户看到的永远是 `白膜注入: None`（我自己跑 e2e 时看到的就是这个）。
        #   白膜流程一半的价值在于"用户知道它生效了"，不透出来等于白做。
        "previz": res.get("previz") or {},
    }


def plan_long_video(shot_id: str, db: Database,
                    settings: Dict[str, Any]) -> Dict[str, Any]:
    """只排片不生成：让用户（和 agent）先看清"要切几段、每段几秒"再掏钱。

    这一步是免费的，也永远不会因为参数非法而白跑一次 API。
    """
    shot = db.get_shot(shot_id)
    if not shot:
        return {"ok": False, "error": f"Shot not found: {shot_id}"}

    from . import longvideo as _lv

    provider = shot["model_provider"]
    api_key = (settings.get("api_keys") or {}).get(provider, "")
    adapter = get_adapter(provider, api_key=api_key,
                          config={"model_name": shot["model_name"]})

    total = float(shot.get("duration_seconds") or 0)
    strat = _lv.choose_strategy(total, adapter.max_duration)
    plan = _lv.plan_segments(total, adapter.min_duration, adapter.max_duration, strat)
    bad = _lv._validate_plan(plan, total, adapter.min_duration, adapter.max_duration)
    return {
        "ok": not bad,
        "error": bad,
        "shot_id": shot_id,
        "provider": provider,
        "model": shot["model_name"],
        "model_range": [adapter.min_duration, adapter.max_duration],
        "total_duration": total,
        "strategy": strat,
        "segments": plan,
        "hint": {
            "single": "一次调用出完，无接缝",
            "chained": "首尾帧续接，运动连贯（段数 ≤ 3 时才用）",
            "anchored": "在分镜切点断开、每段重新用参考图锚定身份（长片用这个，避免退化累积）",
        }.get(strat, ""),
    }
