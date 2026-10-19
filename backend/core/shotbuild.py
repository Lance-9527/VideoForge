# -*- coding: utf-8 -*-
"""剧本 → 分镜 构建（唯一实现）。

★ v1.4.3 抽出：原来这段 200 行逻辑只写在 main.py 的
  `POST /api/projects/{pid}/shots/from-script` 端点里，
  agent 的对话流程根本调不到 —— agent 只能拿到 generate_script 的
  {"title", "scenes_count", ...} 计数摘要，找不到任何分镜，
  于是永远停在"未生成任何镜头"。

现在抽成 core 函数，**API 端点和 agent pipeline 共用同一份实现**，
不会出现"网页上能拆镜、对话里不能"的双份逻辑。

错误用 ShotBuildError（业务错误），由调用方决定怎么转成 HTTP 响应。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from models import DEFAULT_MODEL_IDS
except Exception:  # pragma: no cover - 独立部署时没有 models 模块
    DEFAULT_MODEL_IDS = {}


class ShotBuildError(Exception):
    """拆镜的业务错误（不是程序 bug）。"""


def build_shots_from_script(db, pid: str, replace: bool = True) -> Dict[str, Any]:
    """把剧本的场次批量转成分镜，并建立分镜 ↔ 角色/场景 的真实引用。

    Args:
        db: Database 实例
        pid: 项目 id
        replace: 是否先删掉旧分镜

    Returns:
        {"created", "existing_before", "removed", "shots", "linkage", "default_model"}
    """
    script = db.get_script(pid)
    if not script or not script.get("scenes"):
        raise ShotBuildError("该项目还没有剧本，请先在「剧本」页生成剧本")
    scenes = script["scenes"]
    if not isinstance(scenes, list) or not scenes:
        raise ShotBuildError("剧本里没有可用场次")

    tlp_all = script.get("three_layer_prompts") or {}
    char_list = db.list_characters(pid) or []
    scene_list = db.list_scenes(pid) or []
    proj = db.get_project(pid) or {}
    ps = proj.get("settings") or {}
    if isinstance(ps, str):
        try:
            ps = json.loads(ps)
        except Exception:
            ps = {}
    settings = db.get_all_settings()
    # 没有显式配置时，优先用**用户已经配了 Key 的**视频模型，
    # 避免「配了 seedance 却一直用默认 kling」这种"配了用不上"的情况
    default_provider = settings.get("default_model_provider")
    default_model = settings.get("default_model_name")
    if not default_provider:
        keys = settings.get("api_keys") or {}
        for k, v in keys.items():
            if v:
                default_provider = k
                break
    if not default_provider:
        default_provider = "kling"
    if not default_model:
        from core.adapters import get_adapter
        try:
            a = get_adapter(default_provider)
            default_model = DEFAULT_MODEL_IDS.get(default_provider, "")
        except Exception:
            default_model = ""

    # ── 建立匹配索引 ──
    def norm(s: str) -> str:
        s = str(s or "").strip()
        # 去掉「（伏击圈内）」这类括注，便于与场景名对上
        s = re.sub(r"[（(][^）)]*[）)]", "", s)
        return re.sub(r"\s+", "", s)

    scene_by_name = {}
    for sc in scene_list:
        scene_by_name.setdefault(norm(sc.get("name")), sc)
        scene_by_name.setdefault(str(sc.get("name") or "").strip(), sc)
    char_by_name = {}
    for c in char_list:
        char_by_name[str(c.get("name") or "").strip()] = c

    def match_scene(scene: Dict[str, Any]) -> Optional[str]:
        """把剧本场次的地点匹配到场景资产"""
        cands = [scene.get("location"), scene.get("name"), scene.get("title"),
                 scene.get("place")]
        for cand in cands:
            if not cand:
                continue
            key = norm(cand)
            if key in scene_by_name:
                return scene_by_name[key]["id"]
            # 包含匹配（"山林小道（伏击圈内）" → "山林小道"）
            for k, sc in scene_by_name.items():
                if k and (k in key or key in k) and len(k) >= 2:
                    return sc["id"]
        return None

    def match_chars(scene: Dict[str, Any], tlp: Dict[str, Any]) -> List[str]:
        """把该场次的出场人物匹配到角色资产；剧本没写就用文本里出现的名字兜底"""
        names: List[str] = []
        raw = scene.get("characters")
        if raw:
            names = [str(x).strip() for x in (raw if isinstance(raw, list) else [raw])]
        if not names and isinstance(tlp, dict):
            blob = json.dumps(tlp, ensure_ascii=False)
            names = [n for n in char_by_name if n and n in blob]
        if not names:
            # 最后兜底：整段文本里出现的角色名
            blob = json.dumps(scene, ensure_ascii=False)
            names = [n for n in char_by_name if n and n in blob]
        ids = []
        for n in names:
            c = char_by_name.get(n)
            if c and c["id"] not in ids:
                ids.append(c["id"])
        return ids

    existing = db.list_shots(pid) or []
    # ★ 分镜必须**严格按剧本细节**：细节里有逐秒动作/表情/镜头，
    #   比剧本场次本身细一层，直接决定生成质量。
    details_map = {}
    try:
        for d in (db.list_scene_details(pid) or []):
            if isinstance(d, dict) and d.get("scene_number"):
                details_map[d["scene_number"]] = d
    except Exception:
        details_map = {}
    # ⚠ 真的把旧分镜删掉：此前注释写着"先清掉"，代码却没实现，
    #   导致每点一次「自动从剧本生成」就多出一整套重复分镜。
    removed = 0
    if replace and existing:
        for old_shot in existing:
            try:
                if db.delete_shot(old_shot["id"]):
                    removed += 1
            except Exception as e:
                logger.warning("删除旧分镜失败 %s: %s", old_shot.get("id"), e)
    created, unmatched_scene, no_char = [], [], []
    used_detail = 0
    for i, scene in enumerate(scenes):
        num = scene.get("scene_number") or (i + 1)
        tlp = tlp_all.get(f"scene_{num}") or {}
        detail = details_map.get(num) or {}
        if detail:
            used_detail += 1
        sid_matched = match_scene(scene)
        cids = match_chars(scene, tlp)
        # 细节里明确写了出场人物时，以**细节为准**（比场次字段更准）
        dchars = detail.get("characters")
        if isinstance(dchars, list) and dchars:
            names = []
            for c in dchars:
                nm = (c.get("name") if isinstance(c, dict) else c)
                if nm:
                    cobj = char_by_name.get(str(nm).strip())
                    if cobj and cobj["id"] not in names:
                        names.append(cobj["id"])
            if names:
                cids = names
        if not sid_matched:
            unmatched_scene.append(str(scene.get("location") or scene.get("name") or f"第{num}场"))
        if not cids:
            no_char.append(num)
        # ★ 优先级：剧本细节 > 三层提示词 > 场次字段
        #   细节是"逐秒动作 + 表情 + 镜头"，比场次概述细一层，直接决定成片质感
        d_timeline = detail.get("timeline") if isinstance(detail.get("timeline"), list) else []
        if d_timeline:
            layer2 = [{
                "start": t.get("start"), "end": t.get("end"),
                "action": t.get("action") or t.get("description") or "",
                "expression": t.get("expression") or "",
                "camera": t.get("camera") or t.get("camera_move") or "",
                "shot_size": t.get("shot_size") or t.get("景别") or "",
                "dialogue": t.get("dialogue") or t.get("line") or "",
            } for t in d_timeline if isinstance(t, dict)]
        else:
            layer2 = tlp.get("layer2_timeline") or []
        layer1 = (detail.get("summary") or detail.get("atmosphere")
                  or tlp.get("layer1_overview") or scene.get("summary")
                  or scene.get("description") or "")
        layer3 = tlp.get("layer3_constraints") or {}
        if not layer3 and detail.get("constraints"):
            layer3 = detail.get("constraints") if isinstance(detail.get("constraints"), dict) else {}
        dur = detail.get("duration_seconds") or scene.get("duration_seconds") or 10
        # ★★ 新分镜的生成条件必须跟**本项目已有镜头**保持一致，而不是跟全局设置。
        #    否则用户换了全局默认模型之后再生成分镜，新旧镜头立刻变成"两个世界"
        #    —— 不同模型的画风/色彩科学不同，拼起来一眼就是两个片子，
        #    后期调色和转场**补不回来**（实测线上 9 个项目里 5 个中招）。
        #    只有在项目里还没有任何镜头时，才回落到全局设置。
        try:
            from core.consistency import default_shot_conditions
            _cond = default_shot_conditions(settings, db.list_shots(pid) or [])
        except Exception:
            _cond = {}
        _prov = _cond.get("model_provider") or default_provider
        _model = _cond.get("model_name") or default_model or ""
        _ar = _cond.get("aspect_ratio") or ps.get("default_aspect_ratio") or "16:9"
        _res = _cond.get("resolution") or "1080p"
        shot = db.create_shot(
            project_id=pid,
            order_index=i,
            scene_id=sid_matched,
            duration_seconds=max(5, int(dur)),
            layer1_overview=layer1,
            layer2_timeline=layer2,
            layer3_constraints=layer3,
            character_ids=cids,
            model_provider=_prov,
            model_name=_model,
            aspect_ratio=_ar,
            resolution=_res,
        )
        created.append(shot)

    linked_scene = sum(1 for s in created if s.get("scene_id"))
    linked_char = sum(1 for s in created if s.get("character_ids"))
    return {
        "created": len(created),
        "existing_before": len(existing),
        "removed": removed,
        "shots": created,
        "linkage": {
            "scene_linked": linked_scene,
            "char_linked": linked_char,
            "total": len(created),
            "used_scene_detail": used_detail,
            "unmatched_scenes": unmatched_scene[:8],
            "shots_without_character": no_char[:8],
        },
        "default_model": {"provider": default_provider, "model": default_model},
    }
