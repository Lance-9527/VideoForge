"""
VideoForge · Agent 工具（第二批：长视频 / 白膜两阶段）

════════════════════════════════════════════════════════════════════════
为什么单独一个文件
════════════════════════════════════════════════════════════════════════
第一批工具（`agent_tools.py`）是「查项目 / 建项目 / 生成剧本 / 拆分镜」，
都是轻量、几秒返回的。这一批不一样：

  · `render_previz` / `generate_preview` / `generate_final` 是**真花钱、
    真耗时**的操作（分钟级、每段几毛到几块）
  · 它们之间还有**强制顺序**：没确认白膜不许出终版

把这两类混在一个文件里，会让「哪些工具是安全的、哪些是要花钱的」变得看不出来。
分开之后，`writes=True` 的那几个一眼就能认出来。

════════════════════════════════════════════════════════════════════════
工具清单
════════════════════════════════════════════════════════════════════════
    plan_long_video       排片预览，**免费**，先看清"要切几段、每段几秒"
    render_previz         渲白膜（本地免费），G1 闸门
    review_previz         确认/驳回白膜，**驳回必须带理由码**
    generate_preview      480P 草稿样片（便宜），G2 闸门
    generate_final        1080P 成片（贵），G3 闸门
    get_shot_progress     看某个镜头走到哪一步了
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from .agent_tools import REGISTRY, ToolResult, err, ok, merged, _services, _call, _need

logger = logging.getLogger("videoforge.agent_tools.production")


def _shot_or_err(db, shot_id: str):
    if not shot_id:
        return None, err("缺少 shot_id")
    shot = db.get_shot(shot_id)
    if not shot:
        return None, err(f"分镜不存在：{shot_id}")
    return shot, None


def _settings(db) -> Dict[str, Any]:
    try:
        return db.get_all_settings() or {}
    except Exception:
        return {}


def _risk_check(db, shot: Dict[str, Any], *,
                prompt: str = "") -> Dict[str, Any]:
    """出片前的**内容风险预检**（免费）。

    为什么要抽成函数：样片和成片两条路都要用，而且后端 API 路由也该能用。
    不做的后果是实测过的那种浪费 —— 平台跑完 93 秒才判违规，钱照扣。

    注意它是**建议**不是硬拦：风险词表天生是启发式的，
    硬拦会把正常创作卡住（`哈利波特` 也可能是同人研究）。
    """
    try:
        from core import moderation as _mod
        from core.orchestrator import _compose_full_prompt

        p = prompt or _compose_full_prompt(shot) or (shot.get("layer1_overview") or "")
        refs, names = [], []
        try:
            for cid in (shot.get("character_ids") or []):
                c = db.get_character(cid) if cid else None
                if c:
                    names.append(c.get("name") or "")
                    refs.append(c.get("reference_image_path") or "")
        except Exception:
            pass
        return _mod.check(prompt=p,
                          negative_prompt=shot.get("error_message") or "",
                          reference_images=[r for r in refs if r],
                          character_names=[n for n in names if n])
    except Exception as e:
        logger.warning("内容预检失败（按无风险继续）：%s", e)
        return {"level": "ok", "hits": [], "advice": [],
                "need_confirm": False, "summary": "预检不可用（已跳过）"}


# ──────────────────────── 0. 出片前必查：资产引用 ────────────────────────


# ──────────────────────── 转镜衔接：尾帧接力 ────────────────────────
#
# ★★ 2026-10-21 补（用户诉求「转镜一致性 + 衔接丝滑」的落地点）。
#
#   现状：core/continuity.py 里 `chain_plan` 算得出"这一镜该接上一镜的尾帧"，
#   `extract_last_frame` / `supports_first_frame` 也都有 ——
#   但**生成阶段一次都没用**：VideoGenRequest 只传了 character_reference，
#   没有 first_frame。于是每一镜都是**各自独立**生成的，
#   画面接不上（"转镜要接得上"只写在提示词里，是软的；真正接住的是首帧图）。
#
#   现在：第 N 镜的首帧 = 第 N-1 镜的**尾帧**（用户选中那个候选的尾帧）。
#   前提：模型支持首帧（supports_first_frame）且上一镜已经有成片。

def _write_chain_constraint(db, shot: Dict[str, Any], chain_ref: str) -> None:
    """把本镜的**转镜衔接约束**写进 shot.reference_features['chain']。

    ★ 2026-10-21：算出来不存就等于白算 —— 事后没人能查
      "这一镜为什么接不上 / 上一步是什么"。
      字段与 core.continuity.chain_plan() 的输出对齐。
    """
    try:
        pid = str(shot.get("project_id") or "")
        if not pid:
            return
        all_shots = db.list_shots(pid) or []
        ordered = sorted([s for s in all_shots if isinstance(s, dict)],
                         key=lambda s: (s.get("order_index")
                                        if s.get("order_index") is not None else 0))
        try:
            idx = next(i for i, s in enumerate(ordered)
                       if str(s.get("id")) == str(shot.get("id")))
        except StopIteration:
            return
        if idx <= 0:
            return
        prev = ordered[idx - 1]
        # ★ 与实际生成**用同一套判定**（core.shot_chain.can_relay）——
        #   落库的东西如果和实际行为对不上，事后查"这镜为什么接不上"就是误导。
        from core import shot_chain as _sc
        _d = _sc.can_relay(prev, shot)
        _relay = bool(_d.get("relay"))
        _why = str(_d.get("reason") or "")
        from core.continuity import chain_plan
        plan = chain_plan(ordered[: idx + 1])
        item = next((x for x in plan
                     if str(x.get("shot_id")) == str(shot.get("id"))), None)
        if not item:
            return
        _rf = shot.get("reference_features")
        if isinstance(_rf, str):
            import json as _j
            try:
                _rf = _j.loads(_rf or "{}")
            except Exception:
                _rf = {}
        _rf = dict(_rf or {})
        _rf["chain"] = {
            "prev_shot_id": item.get("prev_shot_id"),
            "transition_type": item.get("transition_type"),
            "must_match_first_frame_desc": item.get("must_match_first_frame_desc"),
            "use_last_frame_of": item.get("use_last_frame_of"),
            # ★ 与实际生成一致的三个字段
            "relay": _relay,
            "relay_reason": _why,
            "first_frame_applied": bool(chain_ref),
        }
        db.update_shot(shot["id"], reference_features=json.dumps(_rf, ensure_ascii=False))
    except Exception as e:
        logger.debug("写 chain 约束失败（不影响生成）: %s", e)

def _chain_first_frame(db, shot: Dict[str, Any]) -> tuple:
    """判定本镜要不要**接上一镜的尾帧**，并给出首帧。

    ★★ 2026-10-22 改写（重要）：原来这里是"无条件接龙" ——
      只要上一镜有视频就拿它的尾帧当本镜首帧。**这是错的**：
      平台 **first_frame 与 reference_image 互斥**
      （core/shot_chain.py 的文件头写明：「平台严格采纳 first_frame，
      但 first_frame 与 reference_image 互斥」）。
      无条件接龙 = 这一镜**带不了角色/场景参考图** = 角色长相漂移、
      场景道具换边 —— 正好是用户最不想要的结果。

      现在**委托 core.shot_chain.can_relay() 判定**（dsh 2026-10-22 定的规则）：
        · 同场景 + 同景别 + 无时间跳跃 → 接龙（像素级连续）
        · 其余（换机位 / 跨场景 / 时间跳跃 / 景别突变）→ 独立镜（关键帧 + 参考图）
        · 连续接龙满 2 镜 → 强制回独立镜（重校准，防角色漂移）

    Returns:
        (first_frame_ref, relay: bool, reason: str)
        relay=True 时**不要**再传 character_reference（互斥）。
    """
    try:
        from core import shot_chain as _sc
    except Exception:
        return "", False, "shot_chain 不可用"
    try:
        pid = str(shot.get("project_id") or "")
        if not pid:
            return "", False, "没有项目"
        all_shots = db.list_shots(pid) or []
        ordered = sorted([s for s in all_shots if isinstance(s, dict)],
                         key=lambda s: (s.get("order_index")
                                        if s.get("order_index") is not None else 0))
        try:
            idx = next(i for i, s in enumerate(ordered)
                       if str(s.get("id")) == str(shot.get("id")))
        except StopIteration:
            return "", False, "找不到本镜"
        if idx <= 0:
            return "", False, "第一镜"
        prev = ordered[idx - 1]

        _d = _sc.can_relay(prev, shot)
        if not _d.get("relay"):
            return "", False, str(_d.get("reason") or "不满足接龙条件")

        # ★ 连续接龙满 2 镜 → 强制回独立镜（重校准，防角色漂移）
        _chain_len = 0
        for _j in range(idx - 1, -1, -1):
            _pj = ordered[_j]
            _pp = ordered[_j - 1] if _j > 0 else None
            if _pp is None or not _sc.can_relay(_pp, _pj).get("relay"):
                break
            _chain_len += 1
        if _chain_len >= 2:
            return "", False, "已连续接龙 2 镜，按规则回到独立镜（防角色漂移）"

        # 取尾帧（复用 shot_chain.last_frame_path：分段生成本来就会存 seg_XX_last.jpg）
        _out = _sc.last_frame_path(_outputs_root(db), pid, str(prev.get("id") or ""))
        if not _out or not os.path.exists(_out):
            return "", False, "上一镜没有可用的尾帧"
        from core.adapters import to_image_ref
        _ref = to_image_ref(_out) or ""
        if not _ref:
            return "", False, "尾帧转 data URI 失败"
        return _ref, True, str(_d.get("reason") or "接龙")
    except Exception as e:
        logger.debug("尾帧接力判定失败（按独立镜继续）: %s", e)
        return "", False, f"判定异常: {e}"


def _outputs_root(db=None) -> str:
    """输出根目录。

    ★ 与 core/previz_blender.py / core/shot_prompts.py 的取法**保持一致**
      （settings.output_dir → 回落 data/outputs）——
      不一致的话 shot_chain.last_frame_path 会去别的目录找尾帧，永远找不到。
    """
    try:
        if db is not None:
            root = (db.get_all_settings() or {}).get("output_dir") or ""
            if root:
                return str(root)
    except Exception:
        pass
    return os.path.join(os.getcwd(), "data", "outputs")

def collect_refs(db, shot: Dict[str, Any]) -> Dict[str, Any]:
    r"""把这一镜**该用到的资产参考图**收齐：角色的定妆照 + 场景图。

    ★★ 2026-09-28 第 74 轮（用户原话两条）：
        「还有就是生成的视频要用指定的资产里面的角色+场景来生成」
        「要先生成白膜视频，让用户确认；用户确认通过后再生成对应的视频…
          不是你一上来一顿乱甩」

    原来这条路上有两个洞，合起来正好造成用户看到的现象：

      ① **只收角色图、从来不收场景图** —— 于是"场景"这个资产对成片毫无影响，
         用户设的场景图和最终画面没关系；
      ② 用户的镜头**一个角色都没挂上**（`character_ids` 是空的），
         循环跑 0 次 ⇒ `refs = []` ⇒ **一张参考图都没发**，
         模型自己编人编景 —— 就是「每个镜头一张脸」。

    所以这里既收齐（角色 + 场景），又把"收到几张、谁没图"如实返回，
    让上层在花钱之前就能拦住。
    """
    out = {"refs": [], "character_names": [], "scene_name": "",
           "characters_with_image": 0, "characters_total": 0,
           "has_scene_image": False, "missing": [],
           # ★ 2026-10-21：本次实际带上的基准角色（给界面/日志看）
           "canonical_characters": []}
    from core.adapters import to_image_ref
    try:
        cids = shot.get("character_ids") or []
        if isinstance(cids, str):
            import json as _j
            try:
                cids = _j.loads(cids) if cids.strip() else []
            except Exception:  # noqa: BLE001
                cids = []
        # ★★ 2026-10-21：用户主动指定为「基准角色」的角色，**每一镜都带上**。
        #
        #   为什么：上面那段只收分镜自己挂的 character_ids。
        #   但用户给一个角色标了 canonical，意思就是"**这个角色每镜都得是他**"
        #   —— 而分镜表里的 character_ids 经常漏挂（尤其剧情推进后新角色没补上）。
        #   漏挂 = 这一镜没有那张脸 = "每个镜头一张脸"。
        #   基准角色不能被分镜的漏挂否决。
        _seen_cids = {str(x) for x in cids if x}
        try:
            _pid = str(shot.get("project_id") or "")
            for _cc in (db.get_canonical_characters(_pid) or []):
                _cid = str(_cc.get("id") or "")
                if not _cid or _cid in _seen_cids:
                    continue
                _seen_cids.add(_cid)
                cids.append(_cid)
        except Exception as _ce:
            logger.debug("取基准角色失败（按原样继续）: %s", _ce)

        for cid in cids:
            c = db.get_character(cid) if cid else None
            if not c:
                continue
            out["characters_total"] += 1
            out["character_names"].append(c.get("name") or "")
            # ★ 基准角色**无论有没有参考图**都要记下来（界面/日志要显示"这是基准"）
            if c.get("is_canonical"):
                out["canonical_characters"].append(c.get("name") or "")
            p = (c.get("reference_image_path") or "").strip()
            if p and os.path.exists(p):
                r = to_image_ref(p)
                if r:
                    out["refs"].append(r)
                    out["characters_with_image"] += 1
            else:
                out["missing"].append(c.get("name") or cid)

        sid = shot.get("scene_id")
        if sid:
            sc = db.get_scene(sid) if hasattr(db, "get_scene") else None
            if sc:
                out["scene_name"] = sc.get("name") or ""
                sp = (sc.get("reference_image_path") or "").strip()
                if sp and os.path.exists(sp):
                    r = to_image_ref(sp)
                    if r:
                        # ★ 场景图**放在最后**：提示词里的编号顺序要求
                        #   "角色在前、场景在后"（见 refcheck / 适配器同源约定）
                        out["refs"].append(r)
                        out["has_scene_image"] = True
                else:
                    out["missing"].append(out["scene_name"] or sid)
        else:
            out["missing"].append("（这一镜没关联场景）")
    except Exception as e:  # noqa: BLE001
        logger.warning("收集资产参考图失败（按少图继续）：%s", e)
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def refs_gate(db, shot: Dict[str, Any], *, allow_no_refs: bool = False) -> Optional[ToolResult]:
    r"""**花钱之前**的资产门：这一镜必须真的用上资产（角色+场景）。

    返回 `None` = 放行；返回 `ToolResult` = 拦下（带可执行的下一步）。

    为什么是硬门（而不是"提醒一下"）：用户实测的原话是
    「生成的视频要用指定的资产里面的角色+场景来生成」。
    没有参考图时模型**一定会自己编**，花的钱换回来一个跟设定无关的片子 ——
    这是最贵的浪费。而且用户明确要求先白膜、确认后再出片，
    所以这里要把"资产没关联"这种前置问题在花钱前解决掉。
    """
    info = collect_refs(db, shot)
    chars = 0
    try:
        chars = len(db.list_characters(shot.get("project_id") or "") or [])
    except Exception:  # noqa: BLE001
        chars = 0
    if info["refs"]:
        return None
    if allow_no_refs:
        return None
    if chars:
        return err(
            "这一镜**没有挂上任何角色/场景**，所以一张参考图都发不出去 —— "
            "模型会自己编人编景（就是「每个镜头一张脸」）。\n"
            f"（项目里有 {chars} 个角色，但这一镜的 character_ids 是空的。）\n"
            "先把分镜和资产关联起来：调 relink_shots（按名字自动匹配）；"
            "名字对不上就告诉用户'剧本里的名字和角色页的名字是两套'，"
            "让他决定是重抽资产还是改名字。**不要**就这样开始花钱生成。",
            refs_gate={"refs": 0, "characters": chars,
                       "hint": "先 relink_shots，或明确告诉用户这一镜没有参考图"},
        )
    # 项目里一个角色都没有：可能是纯空景片，允许但要说清
    return None


def _reference_capability_gate(db, shot: Dict[str, Any], refs: List[Any],
                               allow_no_reference: bool, *,
                               stage: str = "出片") -> Optional[ToolResult]:
    r"""这个模型**收不了参考图**时，**别花这笔钱**（拦下；放行返回 None）。

    ★★ 2026-09-29（用户第 2 条：「资产里的角色图**必须真能用上**」）

    真跑实测（未计费，被平台判非法参数）：
        InvalidParameter: the specified task_type r2v does not support
                          model doubao-seedance-1-0-pro
    用户分镜挂的正是这个模型 ⇒ **挂着它出片，角色/场景图一定用不上**。

    旧行为：适配器悄悄摘掉参考图、照样生成 —— **钱花了，人不像**。
    用户的原话是"必须真能用上"，所以：**宁可拦住，也不悄悄花这笔钱**。
    真要出（接受"这一条不带参考图"）就显式给 `allow_no_reference=true` ——
    "显式同意"与"被瞒着"是两回事。
    """
    if allow_no_reference or not refs:
        return None
    try:
        from core import model_catalog as _mc
        prov = str(shot.get("model_provider") or "")
        mid = str(shot.get("model_name") or "")
        rs = _mc.reference_support(prov, mid)
    except Exception as e:  # noqa: BLE001
        logger.debug("查模型参考图能力失败（放行）：%s", e)
        return None
    if rs.get("supports_reference") is not False:
        return None                      # 收得了 / 不确定 → 不拦
    alts = rs.get("alternatives") or []
    names = "、".join("%s（%s）" % (a.get("label"), a.get("id")) for a in alts[:2])
    return err(
        f"**先别花这个钱** —— 这一镜用的模型「{rs.get('label') or mid}」"
        f"**不支持图生视频**，平台会直接拒收参考图。\n"
        f"也就是说：挂着它{stage}，你资产里的**角色图/场景图一定用不上**"
        f"（长相和环境都成了模型自己编的），而钱照样花。\n"
        + (f"\n建议换成（都支持参考图）：{names}" if names else "")
        + "\n换法：在卡片上点「🔁 换成 …」（一次换全部镜头），或调 set_shots_model 工具。\n"
          "如果你**明知如此**仍要出这一条（接受不带参考图），"
          "带上 `allow_no_reference=true` 再说一次。",
        shot_id=shot.get("id"),
        model_reference_support=False,
        alternatives=alts,
        say_to_user=(f"这个镜头的模型「{rs.get('label') or mid}」用不了你的角色/场景图 —— "
                     f"先换成支持图生视频的模型再出，别白花这笔钱。"),
        next_step=("换成支持参考图的模型（卡片上就有「换成 …」按钮）→ 再出片。"
                   "确实要出不带参考图的版本，才带 allow_no_reference=true。"),
    )


# ──────────────────────── 1. 排片预览（免费）────────────────────────


@REGISTRY.register(
    "plan_long_video",
    "★先调这个★。排片预览：告诉用户这个镜头会被切成几段、每段几秒、走哪档策略。"
    "免费、瞬间返回，不花一分钱。生成前先给用户看排片，同意了再真生成。",
    {"shot_id": {"type": "string", "desc": "分镜 id", "required": True}},
)
async def t_plan_long_video(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "shot_id")
    if bad:
        return bad
    try:
        from core.orchestrator import plan_long_video
        db, _, _ = _services()
        res = await _call(plan_long_video, args["shot_id"], db, _settings(db))
        if not res.get("ok") and res.get("error"):
            return err(res["error"], detail=res)
        return ok(**res)
    except Exception as e:
        return err(f"排片失败：{type(e).__name__}: {e}")


# ──────────────────────── 2. 白膜（本地免费，G1）────────────────────────


@REGISTRY.register(
    "render_previz",
    "渲白膜（previz / 白模预演）。★ 本地渲染，**完全免费**，可以无限次重来。"
    "用途：在花任何钱之前，先把运镜、构图、节奏定下来。"
    "★ 2026-09-29：引擎已换成 **Blender 3D 预演**（世界单位为米，有真实厚度、"
    "遮挡与透视，人物可以是匍匐/蹲踞等真实姿态）。装了 Blender 就走新引擎；"
    "没装时才回退到旧的 ffmpeg 平面色块（那种画不出伏地的人，也没遮挡）。"
    "若只想知道引擎是否就绪，用 `previz_status`（更轻）。"
    "生成前会先做几何体检，体检不过会如实告诉你哪里不行。"
    "注意：白膜不是画风，是控制信号 —— 它只给空间关系/运镜路径/时间节奏。"
    "★ 渲完返回里的 can_constrain_generation 会告诉你这版白膜能不能真的约束生成"
    "（取决于白膜参考通道通不通，用 check_previz_channel 可单独查）。",
    {"shot_id": {"type": "string", "desc": "分镜 id", "required": True},
     "camera": {"type": "string", "desc": "运镜：static/dolly_in/dolly_out/pan_left/pan_right/tilt_up/tilt_down",
                "enum": ["static", "dolly_in", "dolly_out", "pan_left", "pan_right", "tilt_up", "tilt_down", "track"]},
     "notes": {"type": "string", "desc": "这个镜头要看到什么、主体怎么动"},
     "tier": {"type": "string",
              "desc": "白膜档位。**A/B/C 三档全部内置**（不再需要 Three.js/Blender）："
                      "A=平面灰模视频（最快，验节奏与构图）；"
                      "B=多机位**静态**联络表（俯视+正面+侧面，验走位与遮挡，需主体带坐标）；"
                      "C=多机位**视频**（同一场戏 4 个机位各出一段有时长的白膜 + 一条覆盖片，"
                      "用来**选机位**、看运动中的遮挡，有真透视）。"
                      "★ 不填会自动判档：3 个以上主体、或走位+运镜 → C；"
                      "有纵深/走位/两个主体 → B；其余 → A。",
              "enum": ["A", "B", "C", "auto"]},
     "angles": {"type": "string",
                "desc": "C 档用哪些机位，逗号分隔。可选："
                        "master(正面,分镜原构图)/left(左前3-4)/right(右前3-4)/"
                        "top(高角)/low(低角)/over(过肩)。默认 master,left,right,top"},
     "subjects": {"type": "string",
                  "desc": "B 档需要：主体的几何描述（JSON 数组）。每项形如 "
                          "{\"name\":\"林薇\",\"x\":520,\"y\":320,\"width\":90,\"height\":340,"
                          "\"z\":900,\"depth\":120,\"humanoid\":true,\"facing\":\"right\","
                          "\"must_see\":true,\"move\":[[520,320],[1000,320]]}。"
                          "x/y/width/height 是**画面像素**，z/depth 是纵深（越大越近镜头）"}},
    needs_project=False,
    writes=True,
)
async def t_render_previz(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "shot_id")
    if bad:
        return bad
    import asyncio as _a
    # ══════════════════════════════════════════════════════════════
    # ★★ 2026-10-25：**白膜默认通道换成 openVideoForge（MCP 口径）**。
    #
    #   链路：本工具 → 工作台桥（8787, POST /tool ≡ MCP tools/call）
    #         → 工作台排场景/机位 → VideoForge 服务(8765) 的 Blender 渲
    #         → 产物（mp4 + 首帧 png）落盘 → wb_render_bind 绑到分镜。
    #
    #   为什么不是内嵌 Blender 直渲：契约里分工是「Agent 造 / 工作台排 /
    #   VideoForge 渲」。白膜的"排"（环境、机位、时长、画幅）归工作台，
    #   这样人在页面里拖一下就能改，不必回改代码或重生成规格。
    #
    #   失败不致命：桥没起 / 页面停在入口 / 渲染失败 → 如实记原因，
    #   继续往下走 Blender 直渲，再不行走旧 ffmpeg 方案。
    #   通道可用 VIDEOFORGE_PREVIZ_CHANNEL=blender 强制回到老路。
    # ══════════════════════════════════════════════════════════════
    try:
        from core import previz_mcp as _mc
        if _mc.channel() in ("mcp", "auto"):
            _db0, _st0, _ = _services()
            _shot0, _e0 = _shot_or_err(_db0, args["shot_id"])
            if _e0:
                return _e0
            _pid0 = _shot0.get("project_id")
            _oi = None
            try:
                _oi = [int(_shot0.get("order_index") or 0) + 1]
            except Exception:  # noqa: BLE001
                _oi = None
            _r0 = await _a.wait_for(
                _a.to_thread(_mc.render_project_previz_mcp, _db0, _pid0, only=_oi),
                timeout=900)
            _rdy0 = _r0.get("ready") or {}
            if _rdy0.get("actions"):
                logger.info("白膜工作台通道自启：%s", "；".join(_rdy0["actions"]))
            if _r0.get("ok"):
                _vids = _r0.get("videos") or []
                _n0 = sum(len(s["frames"]) for s in _r0["shots"])
                return ok(
                    engine="openvideoforge",
                    channel="mcp",
                    bridge=_r0.get("bridge"),
                    out_dir=_r0["out_dir"],
                    shots=len(_r0["shots"]), frames=_n0,
                    videos=_vids,
                    job_ids=[s.get("job_id") for s in _r0["shots"] if s.get("job_id")],
                    paths=[f for s in _r0["shots"] for f in s["frames"]][:24],
                    note=(f"白膜已用 **OpenVideoForge 工作台** 渲出（MCP 通道，"
                          f"{len(_r0['shots'])} 镜，零费用）"
                          + (f"，白膜视频 {len(_vids)} 条" if _vids else "")
                          + f"。目录：{_r0['out_dir']}"
                          + (("\n\n⚠ " + str(_r0.get("note")))
                             if _r0.get("note") else "")))
            # ★ 2026-10-25：失败要说清「卡在哪一层 + 下一步怎么修」，
            #   只写"失败"会让用户以为是代码坏了（实际常是桥没起/8765 没开）。
            logger.warning(
                "openVideoForge 白膜失败，回退 Blender（卡在「%s」）：%s。下一步：%s",
                _r0.get("step") or "render",
                _r0.get("reason") or "未知原因",
                _r0.get("next_action") or "重试一次；或用 check_previz_channel 查通道")
    except Exception as _em:  # noqa: BLE001
        logger.warning("openVideoForge 白膜异常，回退 Blender：%s", _em)

    # ★★ 2026-09-29：**白膜改用 Blender 3D 预演**，本工具先走新路。
    #   用户原话：「再把 Blender 这个环节加入进去（原先出白膜视频的工具可以去掉了）」。
    #   旧路（下面那大段 ffmpeg 平面色块）有三个实测硬伤，不再作为默认：
    #     · 画不出伏地的人（剧本写「俯卧泥水中」渲出站着的方块）；
    #     · 没有厚度与遮挡关系，也没有真透视；
    #     · `y` 是方块顶部坐标，调用方按中心给值就整体埋进地面。
    #   Blender 的世界单位就是米，无二义性，12 镜 5.7 秒渲完、零费用。
    #   旧路保留为**回退**（没装 Blender 时才用），并在返回里说明。
    try:
        from core import previz_blender as _pb
        if _pb.blender_available():
            _db, _st, _ = _services()
            _shot, _e = _shot_or_err(_db, args["shot_id"])
            if _e:
                return _e
            _pid = _shot.get("project_id")
            import asyncio as _a
            _only = None
            try:
                _oi = int(_shot.get("order_index") or 0) + 1
                _only = [_oi]
            except Exception:  # noqa: BLE001
                _only = None
            _r = await _a.wait_for(
                _a.to_thread(_pb.render_project_previz, _db, _pid, only=_only),
                timeout=900)
            if _r.get("ok"):
                _n = sum(len(s["frames"]) for s in _r["shots"])
                return ok(
                    engine="blender",
                    out_dir=_r["out_dir"],
                    shots=len(_r["shots"]), frames=_n,
                    paths=[f for s in _r["shots"] for f in s["frames"]][:24],
                    # ★ 2026-10-22：白膜**视频**（mp4）。平台的白模参考只吃视频，
                    #   只有帧图时 previz_ref 会如实降级；这里把路径带出去，
                    #   用户/下游都能直接看"动不动"。
                    videos=_r.get("videos") or [],
                    note=(f"白膜已用 **Blender 3D 预演** 渲出 {_n} 张"
                          f"（{len(_r['shots'])} 镜，零费用）"
                          + (f"，并编码 {len(_r.get('videos') or [])} 条白膜视频"
                             if _r.get("videos") else "")
                          + f"。目录：{_r['out_dir']}"
                          # ★ 2026-10-20：把"这是内置模板、不是你剧本 3D 重建"
                          #   这条实话说出来（不写用户会拿它当自己的片子预览）
                          + (("\n\n⚠ " + str(_r.get("note")))
                             if _r.get("note") else "")))
            logger.warning("Blender 白膜失败，回退旧方案：%s", _r.get("reason"))
        else:
            logger.info("未装 Blender，白膜走旧的 ffmpeg 方案")
    except Exception as _eb:  # noqa: BLE001
        logger.warning("Blender 白膜异常，回退旧方案：%s", _eb)
    try:
        from core import previz as pv
        db, _, _ = _services()
        shot, e = _shot_or_err(db, args["shot_id"])
        if e:
            return e
        shot_id = shot["id"]

        settings = _settings(db)
        out_dir = (settings.get("cache_dir") or settings.get("output_dir") or ".")
        out_path = f"{out_dir}/previz/{shot_id}.mp4"

        spec = pv.shot_template(
            shot_id=shot_id,
            duration=float(shot.get("duration_seconds") or 5),
            camera=args.get("camera") or "static",
        )
        # ★★ 2026-09-27 第 72 轮：调用方没给运镜时，**从分镜文字里读**，
        #   而不是一律 static。
        #   实测事故：5 个镜头里 4 个渲出来是**一张完全静止的灰块**
        #   （25 KB / 12 秒，三个时间点抽帧统计量一模一样：均值 125.7、标准差 12.69）
        #   —— 用户原话就是「白膜视频生成有问题」。静止的白膜验不了运镜和节奏，
        #   等于白渲。这里读不到才退回 static，并且**把依据如实带出去**。
        cam_from_text = ""
        if not args.get("camera"):
            try:
                _txt_cam = " ".join([
                    str(shot.get("layer1_overview") or ""),
                    json.dumps(shot.get("layer2_timeline") or "", ensure_ascii=False),
                    json.dumps(shot.get("layer3_constraints") or "", ensure_ascii=False),
                ])
                _cam, cam_from_text = pv.infer_camera(_txt_cam)
                spec["camera"] = _cam
            except Exception as e:  # noqa: BLE001
                logger.warning("推断运镜失败（按固定机位）：%s", e)
        # 分镜里已有的信息拿来填，别让用户重打一遍
        # ★★ 2026-09-28 第 74 轮：**按剧本认人**，不再只看 `character_ids`。
        #   用户原话：「你的白膜视频生成没有按照用户的要求来，得跟完全符合剧本指示做」。
        #   而他的镜头 `character_ids` 是空的 ⇒ 旧代码只放**一块匿名灰板**，
        #   既看不出是谁、也看不出剧本写了什么。现在从剧本文字里认说话人/角色名。
        script_who: Dict[str, Any] = {}
        subs: List[Dict[str, Any]] = []
        try:
            chars = db.list_characters(shot.get("project_id") or "") or []
            # ★ 剧本这一场的**演员表**最可靠（它本来就是剧本写的"这一场有谁"）
            scene_chars: List[str] = []
            try:
                _sc = db.get_script(shot.get("project_id") or "") or {}
                _scenes = _sc.get("scenes")
                if isinstance(_scenes, str):
                    _scenes = json.loads(_scenes)
                _oi = shot.get("order_index") or 0
                if isinstance(_scenes, list) and _oi < len(_scenes):
                    _cs = (_scenes[_oi] or {}).get("characters") or []
                    for x in _cs:
                        scene_chars.append(str((x.get("name") if isinstance(x, dict) else x) or ""))
                    scene_chars = [x for x in scene_chars if x]
            except Exception as _e:  # noqa: BLE001
                logger.warning("取剧本演员表失败：%s", _e)
            subs, script_who = pv.subjects_from_script(shot, chars, scene_chars)
            script_who["scene_characters"] = scene_chars
        except Exception as e:  # noqa: BLE001
            logger.warning("按剧本认人失败（退回按角色卡）：%s", e)
        if not subs:
            for cid in (shot.get("character_ids") or []):
                c = db.get_character(cid) if cid else None
                if c and c.get("name"):
                    subs.append(pv.subject_template(name=c["name"], height=700, humanoid=True))
        if not subs and shot.get("layer1_overview"):
            subs.append(pv.subject_template(
                name=(shot.get("layer1_overview") or "")[:12], height=700,
                move=[[500, 400], [700, 400]]))
        spec["subjects"] = subs
        spec["notes"] = args.get("notes") or (shot.get("layer1_overview") or "")[:200]

        # ★ 给主体补走位，让灰块**真的动起来**（依据是分镜文字里的动作词）。
        move_notes: List[str] = []
        if not args.get("subjects"):
            try:
                _txt_move = pv.script_shot_text(shot)
                spec["subjects"], move_notes = pv.infer_subject_moves(
                    spec["subjects"], _txt_move)
            except Exception as e:  # noqa: BLE001
                logger.warning("推断走位失败（按静止）：%s", e)

        # ★ B 档：调用方给了几何信息就用它（覆盖上面按角色凑的简化主体）。
        #   B/C 档验的是走位与遮挡 —— A 档是平面的，看不出
        #   "人会不会撞到桌子""柱子会不会挡住主角"，而这两个错误最贵
        #   （花钱生成出来才发现，这一镜就白抽了）。
        tier = str(args.get("tier") or "auto").strip().upper()
        raw_sub = args.get("subjects")
        if raw_sub:
            try:
                parsed = json.loads(raw_sub) if isinstance(raw_sub, str) else raw_sub
                if isinstance(parsed, list) and parsed:
                    spec["subjects"] = parsed
            except Exception as e:
                return err(f"subjects 不是合法 JSON：{e}。"
                           f"它应该是一个数组，每项含 name/x/y/width/height/z/depth。")

        # ★ auto：按场景复杂度自动选档。判定逻辑在 previz_multi.render_plan_for
        #   （纯函数、有单测），不在这里堆 if —— 免得两处判定不一致。
        auto_why = ""
        if tier in ("AUTO", ""):
            try:
                from core import previz_multi as _pm
                pl = _pm.render_plan_for(spec)
                tier, auto_why = pl["tier"], pl["why"]
            except Exception as e:
                logger.warning("自动判档失败，退回 A：%s", e)
                tier = "A"

        lint = pv.lint_shot(spec)
        spatial = None
        views_info = None
        coverage = None

        # B / C 都需要三维几何：先做免费的空间体检（重叠、出画、被完全遮挡）
        if tier in ("B", "C"):
            try:
                from core import previz_3d as p3
                spatial = p3.lint_spatial(spec)
                if not spatial.get("ok"):
                    return err("三维体检没过，先修再渲：\n"
                               + "\n".join(f"· {e}" for e in spatial["errors"]),
                               lint=spatial, shot_id=shot_id, tier=tier)
            except Exception as e:
                logger.warning("%s 档三维体检失败：%s", tier, e)

        if tier == "C":
            # C 档：多机位各出一段视频（更慢但信息量最大）
            try:
                from core import previz_multi as p3m
                want = [a.strip() for a in str(args.get("angles") or "").split(",")
                        if a.strip()] or None
                coverage = p3m.render_coverage(
                    spec, os.path.join(os.path.dirname(out_path), "coverage"),
                    angles=want)
                if not coverage.get("ok"):
                    logger.warning("C 档失败，降级到 B：%s", coverage.get("error"))
                    tier = "B"
                    coverage = None
            except Exception as e:
                logger.warning("C 档渲染异常，降级到 B：%s", e)
                tier, coverage = "B", None

        if tier == "B":
            # B 档：多机位静态联络表（俯视/正面/侧面）
            try:
                from core import previz_3d as p3
                views_info = p3.render_views(
                    spec, os.path.join(os.path.dirname(out_path), "views"))
            except Exception as e:
                logger.warning("B 档多机位渲染失败，退回 A 档：%s", e)
                tier = "A"

        res = pv.render_greybox(spec, out_path)

        db.update_shot(shot_id,
                       previz_status=pv.S_DRAFT_READY if res.get("ok") else pv.S_NOT_STARTED,
                       previz_path=res.get("path") or "",
                       previz_camera=(args.get("camera") or "static"))

        # ★★ 把白膜的 spec **落一份在磁盘上**（第 70 轮）。
        #   为什么：白膜真正的价值是它定下的**运镜 / 时长 / 走位**，
        #   而"平台不支持视频参考"时要靠提示词把这几样补回去
        #   （`previz_ref._degraded_note`）。在此之前 spec 只活在本次调用的
        #   局部变量里，渲完就没了 —— 降级提示词因此只能写一句"镜头：推近"，
        #   时长和走位全丢。存在白膜视频旁边（sidecar）而不是加 DB 字段：
        #   不用迁移、老项目按"没有 sidecar"自然退回简单降级。
        if res.get("ok") and res.get("path"):
            try:
                import json as _json
                spec_path = str(res["path"]) + ".spec.json"
                with open(spec_path, "w", encoding="utf-8") as f:
                    _json.dump(spec, f, ensure_ascii=False, indent=1)
            except Exception as e:  # noqa: BLE001
                logger.warning("白膜 spec 落盘失败（降级提示词会简单一些）：%s", e)

        # ★ 渲完就要告诉用户"它**能不能真的起作用**" ——
        #   参考视频必须是公网 URL（实测：base64 和本地路径都被平台拒），
        #   而这台机器在 NAT 后面。所以"渲好了"和"用上了"是两件事，
        #   不说清楚用户会以为白膜生效了。
        usability = None
        if res.get("ok"):
            try:
                from core import previz_ref as _pr
                shot_after = db.get_shot(shot_id) or {}
                d = _pr.decide(shot_after, shot.get("model_provider") or "",
                               settings=settings, publish=False)
                usability = {"usable": d.usable, "mode": d.mode, "reason": d.reason}
            except Exception as e:
                usability = {"usable": False, "mode": "unknown",
                             "reason": f"判断可用性失败：{e}"}

        # 体检抽帧：白膜必须**看画面**才算验过（lint 全绿 ≠ 运镜对）。
        # 返回**可访问的 URL** 而不只是路径 —— 前端要直接把帧画在对话里，
        # 用户不可能自己去翻临时目录。`/api/media` 是项目既有的文件出口。
        frames: List[str] = []
        frame_urls: List[str] = []
        video_url = ""
        # B 档的多机位图也要转成可访问 URL，否则前端看不到
        view_urls: List[str] = []
        # C 档：每个机位一段视频 + 一条覆盖片 + 一张联络表
        camera_clips: List[Dict[str, Any]] = []
        coverage_cut = ""
        coverage_sheet_url = ""
        if coverage and coverage.get("ok"):
            from urllib.parse import quote as _q
            try:
                from core.ffmpeg_manager import get_ffmpeg_for_postprocess
                import subprocess as _sp
                _ff = get_ffmpeg_for_postprocess()
                for a in coverage.get("angles") or []:
                    camera_clips.append({
                        "name": a["name"], "label": a.get("label") or a["name"],
                        "url": f"/api/media?path={_q(str(a['path']))}",
                        "path": a["path"], "frames": a.get("frames"),
                    })
                if coverage.get("cut"):
                    coverage_cut = f"/api/media?path={_q(str(coverage['cut']))}"
                sp = coverage.get("sheet")
                if sp and os.path.exists(str(sp)):
                    png = str(sp).rsplit(".", 1)[0] + ".png"
                    _sp.run([_ff, "-y", "-v", "error", "-i", str(sp), png],
                            capture_output=True, timeout=120)
                    if os.path.exists(png):
                        coverage_sheet_url = f"/api/media?path={_q(png)}"
            except Exception as e:
                logger.warning("C 档结果转 URL 失败：%s", e)
        if views_info and views_info.get("ok"):
            from urllib.parse import quote as _q
            try:
                from core.ffmpeg_manager import get_ffmpeg_for_postprocess
                import subprocess as _sp
                _ff = get_ffmpeg_for_postprocess()
                # PGM → PNG（ffmpeg 直接读 PGM；浏览器不认 PGM，必须转）
                for key in ("sheet",):
                    p = views_info.get(key)
                    if not p:
                        continue
                    png = str(p).rsplit(".", 1)[0] + ".png"
                    _sp.run([_ff, "-y", "-v", "error", "-i", str(p), png],
                            capture_output=True, timeout=120)
                    if os.path.exists(png):
                        view_urls.append(f"/api/media?path={_q(png)}")
            except Exception as e:
                logger.warning("多机位图转 PNG 失败：%s", e)
        if res.get("ok") and res.get("path"):
            from urllib.parse import quote as _q
            video_url = f"/api/media?path={_q(str(res['path']))}"
            try:
                frames = pv.extract_frames(
                    res["path"],
                    os.path.join(os.path.dirname(res["path"]), "frames"),
                    count=3)
                frame_urls = [f"/api/media?path={_q(f)}" for f in frames]
            except Exception as e:
                # ★ 这里**必须记日志**。早先只写 `frames, frame_urls = [], []`，
                #   抽帧一失败就静默变空表，前端于是什么画面都不显示 ——
                #   而白膜的价值全在画面上，等于整个白膜流程白设。
                #   我自己就被这个静默 except 挡了一次，查了半天。
                logger.warning("白膜抽帧失败（对话里将看不到画面）：%s: %s",
                               type(e).__name__, e)
                frames, frame_urls = [], []
            if res.get("ok") and not frames:
                logger.warning("白膜抽帧返回空：path=%s exists=%s",
                               res.get("path"), os.path.exists(str(res.get("path") or "")))

        return ok(
            rendered=bool(res.get("ok")),
            tier=tier,
            auto_tier_why=auto_why,
            path=res.get("path") or "",
            video_url=video_url,
            frames=frames,
            frame_urls=frame_urls,
            view_urls=view_urls,
            # C 档：多机位视频
            camera_clips=camera_clips,
            coverage_cut=coverage_cut,
            coverage_sheet_url=coverage_sheet_url,
            coverage_note=(coverage or {}).get("note") or "",
            spatial_lint=spatial,
            duration_seconds=res.get("duration"),
            duration_ok=res.get("duration_ok", False),
            lint_errors=lint["errors"],
            lint_warnings=lint["warnings"],
            cost="0（本地渲染）",
            # ★★ 这一版白膜**到底动没动**，必须如实说（2026-09-27 第 72 轮）。
            #   用户原话「白膜视频生成有问题」—— 实测 4/5 个白膜是**一张静止的
            #   灰块**（25 KB / 12 秒，三个时间点抽帧统计量完全一样），
            #   而工具却报了 rendered=true、lint 全绿，用户只能自己盯着看。
            #   现在把"运镜从哪来、有没有走位"直接写进结果里。
            motion={
                "camera": spec.get("camera") or "static",
                "camera_source": (cam_from_text or "调用方指定"),
                "subject_moves": move_notes,
                # ★★ 2026-09-28 第 74 轮：**这一镜的人是从哪来的**。
                #   用户原话「得跟完全符合剧本指示做」—— 所以要把
                #   "剧本这一场有谁、名单取自哪里"如实报出来，
                #   用户才能一眼核对白膜有没有按剧本画。
                "script": script_who,
                "moving_subjects": res.get("moving_subjects") or [],
                "text_on_screen": bool(res.get("text_overlay")),
                "is_static": ((spec.get("camera") or "static") == "static"
                              and not (res.get("moving_subjects") or [])),
                "note": ("这一版**固定机位 + 主体不动** —— 只能验构图/时长/遮挡，"
                         "验不了运镜和节奏。要让白膜真的动起来："
                         "在分镜里写明运镜（推近/拉远/跟拍…）与走位（走近/离开/转身…）"
                         "再渲一次；或直接给 render_previz 传 camera= 与 subjects="
                         if ((spec.get("camera") or "static") == "static"
                             and not (res.get("moving_subjects") or []))
                         else "白膜里有运镜或走位，可以验节奏"),
            },
            # 画面上写了什么（用户要"能看懂"）
            on_screen={
                "characters": (script_who or {}).get("names") or [],
                "names_from": (script_who or {}).get("from"),
                "camera_caption": True,
                "script_caption": (spec.get("notes") or "")[:64],
            },
            # "能不能真的起作用"要说清楚，这是两件事
            can_constrain_generation=usability,
            next_step=("白膜已经渲好了。**把上面的画面发给用户看**"
                       + ("（C 档：把每个机位都给他，让他挑一个）" if camera_clips else "")
                       + "，并让用户确认运镜/走位/节奏对不对。"
                       "确认 → review_previz(decision=approve)；"
                       "不对 → review_previz(decision=reject, reason_code=…)。"
                       if res.get("ok") else "白膜没渲出来，看 rendered=False 的原因。"),
            caveat=(res.get("note") or "") + " " + lint["caveat"],
            error=res.get("error"),
        )
    except Exception as e:
        logger.exception("render_previz failed")
        return err(f"渲白膜失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "review_previz",
    "确认或驳回首轮白膜/样片。★ 驳回**必须**带理由码，因为理由码决定回退到哪一步重跑，"
    "这就是省钱开关：CAMERA/BLOCKING/PACING → 回 G1 重渲白膜；"
    "LOOK/IDENTITY → 停在 G2 只换参考图，**白膜不用重渲**。",
    {"shot_id": {"type": "string", "desc": "分镜 id", "required": True},
     "decision": {"type": "string", "desc": "approve=通过，reject=驳回",
                  "enum": ["approve", "reject"], "required": True},
     "reason_code": {"type": "string",
                     "desc": "驳回时的理由码（approve 时可省）",
                     "enum": ["CAMERA", "BLOCKING", "PACING", "LOOK", "IDENTITY", "OTHER"]},
     "comment": {"type": "string", "desc": "用户的原话，比如'推镜太慢了'"}},
    writes=True,
)
async def t_review_previz(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "shot_id", "decision")
    if bad:
        return bad
    decision = str(args["decision"]).lower()
    if decision not in ("approve", "reject"):
        return err(f"decision 只能是 approve 或 reject，收到 {args['decision']!r}")
    try:
        from core import previz as pv
        db, _, _ = _services()
        shot, e = _shot_or_err(db, args["shot_id"])
        if e:
            return e

        if decision == "reject" and not args.get("reason_code"):
            return err(
                "驳回首轮必须给 reason_code，否则不知道该回退到哪一步重跑 —— "
                "那就会为了'颜色不对'把已经确认过的运镜也推翻重做，白烧钱。"
                "可选：" + "、".join(f"{k}({v[0]})" for k, v in pv.REASON_CODES.items())
            )

        state = pv.get_state(shot)
        cur = state["status"]
        action = (pv.approve(comment=args.get("comment") or "") if decision == "approve"
                  else pv.reject(args["reason_code"], gate="G1",
                                 comment=args.get("comment") or ""))
        nxt = pv.next_status_for(action, cur)

        hist = state["history"] + [action]
        import json as _json
        db.update_shot(shot["id"], previz_status=nxt,
                       previz_notes=_json.dumps(hist[-50:], ensure_ascii=False),
                       final_approved=1 if (decision == "approve" and nxt == pv.S_DRAFT_APPROVED) else 0)

        retries = state["retries"]
        # ★ 状态文案要说真话：驳回 LOOK/IDENTITY 后回到的是"出样片"这一级(G2)，
        #   白膜**不会**重渲 —— 那正是省钱的地方。用 `after_reject` 让文案分清楚。
        label = pv.state_label(nxt, after_reject=(decision == "reject"))
        return ok(**merged(
            ({"reason": action["reason"], "label": action["label"],
              "back_to": action["back_to"],
              "regenerate_draft": action["regenerate_draft"],
              "advice": action["advice"]} if decision == "reject" else {}),
            status=nxt,
            status_text=label,
            decision=decision,
            retries_used=retries,
            next_step=(
                "白膜已确认，可以出 480P 草稿样片了（generate_preview）。"
                if nxt == pv.S_DRAFT_APPROVED else
                (action.get("advice") if decision == "reject" else "继续白膜。")
            ),
        ))
    except Exception as e:
        logger.exception("review_previz failed")
        return err(f"处理确认失败：{type(e).__name__}: {e}")


# ──────────────────────── 3. 草稿样片（便宜，G2）────────────────────────


async def _persist_clip(db, pid: str, shot_id: str, video_url: str,
                         *, source: str = "", task_id: str = "") -> Dict[str, Any]:
    """把生成好的视频**下载到本地**并写回分镜（第 69 轮，堵住两个真 bug；
    第 70 轮补齐"追加而不覆盖"）。

    为什么必须有它：
      `generate_preview` / `generate_final` 以前只记 `task_id`，
      拿到 `video_url` 就返回了 —— 视频留在平台上（链接 24 小时过期）✗。
      于是：① 合成器找不到素材，"永远出不了成片"；
            ② 下次重跑以为没做，**重复花钱**（用户实测撞到过）。

    ★ 第 70 轮修的两件事：
      ① **追加**而不是覆盖。原来直接 `candidates=[新的一条]` ——
         配音版（`voiced_*`）会被下一次重出样片**整批冲掉**，用户听到的
         声音就又没了。现在读旧表 → 去掉同 id 的旧条目 → 追加 → 写回。
      ② **两套 id 键名都写**（`candidate_id` + `id`）。本仓读候选的两处
         （`film.collect_clips`、`compose._selected_candidate_path`）历史上
         各认一套，只写一套必然有一处读不到 —— 而失败方式是**静默挑错片段**。

    返回：{'ok': bool, 'path':..., 'error':...}（**只报告真实发生的事**）
    """
    import json as _json
    import os as _os
    import time as _t

    out_dir = _os.path.join(_os.environ.get("VIDEOFORGE_DATA_DIR") or _os.getcwd(),
                            "outputs", str(pid), "shots")
    _os.makedirs(out_dir, exist_ok=True)
    # 原片按生成任务唯一保存，不能重生成时覆盖旧候选。
    dst = _os.path.join(out_dir, "%s_%s.raw.mp4" % (shot_id, __import__('uuid').uuid4().hex))
    _source_shot = await _call(db.get_shot, shot_id) or {}
    _source_settings = await _call(db.get_all_settings) or {}
    from core.reference_delivery import requires_reference_delivery
    _strict = requires_reference_delivery(_source_shot, _source_settings)
    _delivery = None
    if not video_url:
        return {"ok": False, "error": "没有 video_url（平台没返回成品地址）"}
    # ★ 已经有同一个 shot 的成品下载过、且这次没有新的 URL（重跑）→ 直接用旧的，
    #   不重复下载。真正防"重复花钱"的是调用方在发请求前的候选检查。
    try:
        import httpx
        async with httpx.AsyncClient(timeout=600.0, follow_redirects=True,
                                     trust_env=False) as cli:
            r = await cli.get(video_url)
            if r.status_code != 200:
                return {"ok": False,
                        "error": "下载失败：HTTP %s" % r.status_code,
                        "video_url": video_url}
            with open(dst, "wb") as f:
                f.write(r.content)
    except Exception as e:  # noqa: BLE001
        logger.exception("下载生成结果失败")
        return {"ok": False, "error": "下载失败：%s: %s" % (type(e).__name__, e),
                "video_url": video_url}

    size = _os.path.getsize(dst)
    if size < 1024:
        return {"ok": False, "error": "下载到的文件太小（%d 字节），可能不是视频" % size,
                "video_url": video_url}

    if _strict:
        from core.reference_delivery import finalize_reference_candidate
        from core.previz_ref import white_model_video
        _delivery = await finalize_reference_candidate(
            dst, white_model_video(_source_shot), float(_source_shot.get("duration_seconds") or 0),
            aspect_ratio=_source_shot.get("aspect_ratio") or "16:9",
            silent=not _shot_has_dialogue(_source_shot),
            ffmpeg_path=_source_settings.get("ffmpeg_path") or "",
            source_metadata={"provider": _source_shot.get("model_provider"),
                             "model": _source_shot.get("model_name"), "task_id": task_id})
        if not _delivery.get("ok"):
            _old = _source_shot.get("candidates") or []
            if isinstance(_old, str):
                try: _old = _json.loads(_old)
                except Exception: _old = []
            _old = list(_old) if isinstance(_old, list) else []
            from urllib.parse import quote as _quote
            _play_url = "/api/media?path=" + _quote(dst)
            _old.append({"candidate_id": "rejected_" + __import__('uuid').uuid4().hex,
                "path": dst, "source": source, "provider": _source_shot.get("model_provider"),
                "model": _source_shot.get("model_name"), "task_id": task_id,
                "url": _play_url, "download_url": _play_url + "&download=1",
                "is_selected": False, "validation_status": "rejected", "delivery": _delivery,
                "note": "已生成并保留，可播放和下载；一致性未通过，未选为正式成片。"})
            await _call(db.update_shot, shot_id, candidates=_json.dumps(_old, ensure_ascii=False),
                        status="failed", progress=0.0, error_message=_delivery.get("error") or "一致性未通过")
            return {**_delivery, "generated": True, "playable": True,
                    "path": dst, "url": _play_url,
                    "download_url": _play_url + "&download=1", "bytes": size}
        dst = _delivery["path"]
        size = _os.path.getsize(dst)

    # ★★ 2026-09-28 第 73 轮：**平台给的 mp4 常常没有 faststart**
    #   （实测 seedance 下载下来是 `ftyp → free → mdat → moov`）。
    #   那样在网页播放器里会"卡在 0:00"（要读完整个文件才知道时长）——
    #   用户原话就是「在对话框里面的视频看不了」。
    #   这里顺手重封装一次（`-c copy`，不重编码、无损、秒级）。
    try:
        from core.postprocess import ensure_faststart
        from core.ffmpeg_manager import get_ffmpeg_for_postprocess
        _fs = {"changed": False} if _strict else await ensure_faststart(dst, get_ffmpeg_for_postprocess())
        if _fs.get("changed"):
            logger.info("已把 %s 重封装成 faststart（网页播放器不必读完整个文件）",
                        _os.path.basename(dst))
        elif _fs.get("error"):
            logger.warning("faststart 重封装失败（不影响出片，只影响网页里拖动播放）：%s",
                           _fs["error"])
    except Exception as _e:  # noqa: BLE001
        logger.warning("faststart 检查失败（忽略）：%s", _e)

    cid = "gen_%s_%d" % (str(shot_id)[:8], int(_t.time()))
    entry = {
        # 两套键名都写：读候选的两处历史实现各认一套（见本函数说明）
        "candidate_id": cid,
        "id": cid,
        "path": dst,
        "url": "/api/media?path=" + dst.replace("\\", "/"),
        "source": source or "video",
        "task_id": task_id,
        "provider": _source_shot.get("model_provider"), "model": _source_shot.get("model_name"),
        "delivery": _delivery or {}, "validation_status": "verified" if _delivery else "not_requested",
        "bytes": size,
        "size_bytes": size,
        "is_selected": True,
        "added_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at": _t.time(),
    }

    # 读旧候选 → 追加（不覆盖）
    old = await _call(db.get_shot, shot_id) or {}
    try:
        cands = old.get("candidates") or []
        if isinstance(cands, str):
            cands = _json.loads(cands) if cands.strip() else []
        if not isinstance(cands, list):
            cands = []
    except Exception:
        cands = []
    cands = [c for c in cands if isinstance(c, dict)
             and str(c.get("candidate_id") or c.get("id") or "") != cid]
    # ★ 同一个**文件路径**上的旧条目也要去掉：本函数固定写
    #   `shots/<shot_id>.mp4`，重生成会**覆盖**这个文件 ——
    #   留着旧条目的话，它指向的内容已经换了，用户看到两条不同的候选
    #   其实是同一个文件（评分/对比全部失去意义）。
    try:
        _same = _os.path.normcase(_os.path.abspath(dst))
        cands = [c for c in cands
                 if not (c.get("path")
                         and _os.path.normcase(_os.path.abspath(str(c["path"]))) == _same)]
    except Exception:  # noqa: BLE001
        pass
    for c in cands:
        c["is_selected"] = False
    cands.append(entry)

    await _call(db.update_shot, shot_id,
                candidates=_json.dumps(cands, ensure_ascii=False),
                selected_candidate_id=cid,
                status="completed", progress=1.0, error_message=None)
    # ★ 回读：写不上就如实报错（本仓栽过"报成功却没落库"）
    back = await _call(db.get_shot, shot_id) or {}
    if str(back.get("selected_candidate_id") or "") != cid:
        return {"ok": False, "error": "视频下载好了但**没能写回分镜**（回读不到）",
                "path": dst, "video_url": video_url}
    try:
        back_cands = back.get("candidates") or []
        if isinstance(back_cands, str):
            back_cands = _json.loads(back_cands)
    except Exception:
        back_cands = []
    if not any(str(c.get("candidate_id") or c.get("id") or "") == cid
               for c in (back_cands if isinstance(back_cands, list) else [])):
        return {"ok": False, "error": "回读到了选中 id，但候选列表里找不到这条（写回不完整）",
                "path": dst, "video_url": video_url}
    return {"ok": True, "path": dst, "bytes": size, "candidate_id": cid,
            "url": entry["url"], "candidates": len(cands)}


#: 一条候选片段算不算"某一档"——按生成时写下的 provenance 认
_TIER_MARKERS = {
    "draft": ("preview", "480", "draft"),
    "final": ("final", "1080", "upgrade"),
}


def _tier_of(source: Any) -> str:
    """一条候选属于哪一档（按生成时写下的 provenance）。

    ★ 顺序有讲究：**先判 final 再判 draft**。
      实测（守卫测试当场抓到）：`generate_final(1080p,from_draft)` 里
      **包含 "draft" 字样**（from_draft），先判 draft 会把**成片**判成小样
      ⇒ 出成片时就会以为"已经有小样了"。两个档必须分得开。
    """
    s = str(source or "").lower()
    if any(k in s for k in _TIER_MARKERS["final"]):
        return "final"
    if any(k in s for k in _TIER_MARKERS["draft"]):
        return "draft"
    return ""


def _existing_local_clip(shot: Dict[str, Any], want_tier: str = "") -> str:
    """这个镜头**本地已经有**可用片段吗？有就返回路径。

    这是"重复花钱"的唯一防线：调模型之前先看本地。
    判据取候选里的实际文件（存在且 > 100KB），不信任任何状态字段 ——
    "status=completed 但文件不在"在本仓真实发生过。

    ★★ 2026-09-29（用户第 8、9 条：真跑 480P 时才暴露出来的**大 bug**）

    `want_tier` 是这次新加的：**"有片段"≠"有这一档的片段"**。
    实测原话（真的点了「出 480P 小样」）：
        ok=True, skipped=True
        note="这个镜头本地**已经有片段了**，这次没有调用模型、没有花钱 ✓"
    —— 而它看到的"片段"是**分镜重建后认回的片段**和**配音版**，
    **一版 480P 小样都没有**。后果是这条防线把用户**永远挡在门外**：
    他点多少次"出小样"都只会得到"已经有了、没花钱"，480P 这一步
    永远做不完 ⇒ 正是他说的「流程跑不通」。

    所以：`want_tier="draft"` 时**只认 480P 那一档的片段**；
    `want_tier="final"` 只认成片档；不传则维持原义（任意片段），
    留给"完全不要重复生成"那种场景用。
    """
    import json as _json
    import os as _os

    cands = shot.get("candidates") or []
    if isinstance(cands, str):
        try:
            cands = _json.loads(cands) if cands.strip() else []
        except Exception:
            cands = []
    if not isinstance(cands, list):
        return ""
    sel = str(shot.get("selected_candidate_id") or "")
    ordered = sorted(
        [c for c in cands if isinstance(c, dict)],
        key=lambda c: 0 if str(c.get("candidate_id") or c.get("id") or "") == sel else 1)
    for c in ordered:
        if c.get("validation_status") in ("rejected", "unverified"):
            continue
        if want_tier and _tier_of(c.get("source")) != want_tier:
            continue
        p = c.get("path") or c.get("video_path") or ""
        try:
            if p and _os.path.exists(str(p)) and _os.path.getsize(str(p)) > 102400:
                return str(p)
        except Exception:
            continue
    return ""


async def _revalidate_existing_reference(db, shot, settings, path):
    """Re-check a reused local candidate without making another provider request."""
    from core.reference_delivery import finalize_reference_candidate
    from core.previz_ref import white_model_video
    cands = shot.get("candidates") or []
    if isinstance(cands, str):
        try: cands = json.loads(cands)
        except Exception: cands = []
    cands = list(cands) if isinstance(cands, list) else []
    match = next((c for c in cands if isinstance(c, dict) and str(c.get("path") or c.get("video_path") or "") == str(path)), {})
    metadata = dict((match.get("delivery") or {}).get("source_metadata") or {})
    if not metadata:
        metadata = {"provider": match.get("provider"), "model": match.get("model"), "task_id": match.get("task_id")}
    verdict = await finalize_reference_candidate(path, white_model_video(shot), float(shot.get("duration_seconds") or 0),
        aspect_ratio=shot.get("aspect_ratio") or "16:9", silent=not _shot_has_dialogue(shot),
        ffmpeg_path=settings.get("ffmpeg_path") or "", source_metadata=metadata)
    if match:
        match["delivery"] = verdict
        match["validation_status"] = "verified" if verdict.get("ok") else "rejected"
        if verdict.get("ok"):
            match["path"] = verdict["path"]
        else:
            match["is_selected"] = False
    changes = {"candidates": json.dumps(cands, ensure_ascii=False)}
    if not verdict.get("ok"):
        changes.update(status="failed", progress=0.0, error_message=verdict.get("error") or "旧候选一致性未通过")
        if str(shot.get("selected_candidate_id") or "") == str(match.get("candidate_id") or match.get("id") or ""):
            changes["selected_candidate_id"] = ""
    await _call(db.update_shot, shot["id"], **changes)
    return verdict


@REGISTRY.register(
    "generate_preview",
    "出 480P 草稿样片（Seedance/即梦的 Draft 模式）。比成片便宜得多 ——"
    "官方数据：抽卡 4 次命中省 57%，20 次命中省 77%。"
    "确认画风/表演没问题之后，再用同一个草稿出 1080P 成片（generate_final）。"
    "★ 前提：该镜头的白膜必须已确认。",
    {"shot_id": {"type": "string", "desc": "分镜 id", "required": True},
     "again": {"type": "boolean", "desc": "true=再抽一版样片（上一版不满意）"},
     "acknowledge_risk": {"type": "boolean",
                          "desc": "内容预检判定高风险时，用户明确同意才传 true"},
     "allow_no_reference": {"type": "boolean",
                            "desc": "明知模型收不了参考图仍要出（出来的人/景是模型自己编的）。"
                                    "默认 false = **拦住、不花这笔钱**"}},
    writes=True,
)
def _previz_first_frame(shot: Dict[str, Any]) -> str:
    """找这一镜白膜的第一帧（预演过的起始构图）。找不到返回空串。

    ★ 2026-10-23：出片时若没有接龙首帧，就用白膜首帧当 first_frame，
      让成片构图贴着白膜走（用户："480P 跟白膜没关系"的直接原因）。
    """
    try:
        import glob as _g
        p = str(shot.get("previz_path") or "")
        if p and os.path.isfile(p) and p.lower().endswith((".png", ".jpg", ".jpeg")):
            return p
        cands = []
        if p:
            d = os.path.dirname(p)
            cands += _g.glob(os.path.join(d, "f0001.png"))
            cands += _g.glob(os.path.join(os.path.dirname(d), "**", "f0001.png"), recursive=True)
        for c in cands:
            if os.path.isfile(c):
                return c
    except Exception:  # noqa: BLE001
        pass
    return ""


def _shot_has_dialogue(shot: Dict[str, Any]) -> bool:
    """这一镜有没有人物台词（决定要不要让视频模型自己生成对白语音）。"""
    try:
        tl = shot.get("layer2_timeline")
        if isinstance(tl, str):
            import json as _j
            tl = _j.loads(tl) if tl.strip() else []
        for it in (tl or []):
            if not isinstance(it, dict):
                continue
            d = it.get("dialogue")
            if isinstance(d, dict) and str(d.get("text") or "").strip():
                return True
            if isinstance(d, str) and d.strip():
                return True
            if isinstance(d, list) and any(isinstance(x, dict) and str(x.get("text") or "").strip() for x in d):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


async def t_generate_preview(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "shot_id")
    if bad:
        return bad
    try:
        from core import previz as pv
        from core.adapters import VideoGenRequest, get_adapter, to_image_ref
        db, _, _ = _services()
        shot, e = _shot_or_err(db, args["shot_id"])
        if e:
            return e

        state = pv.get_state(shot)
        # ★★ 硬闸门：没确认白膜不许出样片。跳步 = 花钱抽盲盒。
        #
        #   ★ 2026-09-28 第 74 轮修掉一个**静默失效**：
        #     原来写的是
        #         if status not in (S_DRAFT_APPROVED, S_FINAL_READY, S_NOT_STARTED):
        #             if status == S_NOT_STARTED: return err("还没渲白膜…")
        #     把 `S_NOT_STARTED` **放进了白名单**，于是"还没渲白膜"时
        #     `not in` 为假 → 整个 if 被跳过 → 里面那句"先 render_previz"
        #     **永远执行不到**（死代码），而且**照样开始花钱生成**。
        #     这正是用户说的「不是你一上来一顿乱甩」。
        #     现在：白名单只留"已确认"的两态；没渲/渲了没确认都拦下，
        #     并且分别给出可执行的下一步。
        if state["status"] == pv.S_NOT_STARTED:
            return err(
                "这个镜头还没渲白膜。白膜是**本地免费**的 —— "
                "先跑 render_previz，把构图/走位/运镜给用户看一眼、他确认了再花钱。"
                "（用户明确要求：先白膜 → 确认 → 才生成视频）",
                shot_id=shot["id"], previz_status=state["status"],
                next_step="调 render_previz(shot_id=…)，然后把白膜给用户看。",
            )
        if state["status"] not in (pv.S_DRAFT_APPROVED, pv.S_FINAL_READY):
            return err(
                f"白膜还没**确认**（当前状态 {state['status']}）—— 用户看过白膜并认可之后，"
                f"才会用它约束后面的成片。先让用户看白膜：\n"
                f"· 满意 → review_previz(shot_id=…, decision=\"approve\")\n"
                f"· 不满意 → 让他说哪里不对，改完重渲（本地免费）\n"
                f"**不要**跳过这一步直接花钱。",
                shot_id=shot["id"], previz_status=state["status"],
                next_step="让用户确认白膜（review_previz approve）后再出样片。",
            )

        # ★★ 资产门：这一镜必须真的用上「角色 + 场景」的参考图（用户明确要求）。
        _g = refs_gate(db, shot)
        if _g:
            return _g

        # ★★ 重复花钱的唯一防线：调模型之前先看**本地有没有现成的**。
        #   实测踩过：上一轮生成成功了但没落盘，重跑又花了一条的钱。
        #   这里不信任任何状态字段，只认候选里真实存在的文件。
        #
        #   ★★ 2026-09-29：这里**必须限定"是小样那一档"** ——
        #      原来不限定，于是"认回的片段/配音版"也被当成"已经有小样了"，
        #      用户点多少次「出 480P 小样」都只会得到"已经有了、没花钱"，
        #      这一步**永远做不完**（真跑时当场撞上）。
        if not args.get("again"):
            have = _existing_local_clip(shot, want_tier="draft")
            if have:
                from core.reference_delivery import requires_reference_delivery
                if requires_reference_delivery(shot, _settings(db)):
                    _review = await _revalidate_existing_reference(db, shot, _settings(db), have)
                    if not _review.get("ok"):
                        return err(_review.get("error") or "旧候选一致性未通过", shot_id=shot["id"],
                                   reference_delivery=_review, requires_user_review=True, no_fallback=True)
                    have = _review["path"]
                return ok(
                    shot_id=shot["id"], path=have, skipped=True, reused=True,
                    note=("这个镜头本地**已经有 480P 小样了**，这次没有调用模型、"
                          "没有花钱 ✓（要再抽一版就带 again=true）"),
                    next_step=("让用户看这版；满意就 generate_final 出成片（**升级**、"
                               "不重抽），不满意才带 again=true 再抽一版。"),
                )
            _prior = shot.get("candidates") or []
            if isinstance(_prior, str):
                try: _prior = json.loads(_prior)
                except Exception: _prior = []
            if any(isinstance(c, dict) and c.get("validation_status") in ("rejected", "unverified") for c in _prior):
                return err("上一版付费样片的一致性未通过，原片和费用记录仍在；请先查看报告，明确要求再抽时才使用 again=true。",
                           shot_id=shot["id"], requires_user_review=True, no_fallback=True)
            # 有别的片段（认回的 / 配音版）但**没有小样** → 明确说一句再继续，
            # 免得用户以为"它跳过没做"。
            other = _existing_local_clip(shot)
            if other:
                logger.info("镜头 %s 有片段但不是 480P 小样，按用户要求照常出小样",
                            shot.get("id"))

        # ★ 内容风险预检。样片这一级同样会被内容审核拦（而且被拦也计费），
        #   所以在这里也要先看一眼 —— 越早发现越省钱。
        risk = _risk_check(db, shot)
        if risk.get("level") == "high" and not args.get("acknowledge_risk"):
            return err(
                "内容预检发现高风险，**这次先不发**（发了很可能被拦、而且被拦也计费）。\n"
                + risk["summary"] + "\n"
                + "\n".join(f"· {a}" for a in risk["advice"])
                + "\n\n命中：" + "、".join(h["term"] for h in risk["hits"][:6])
                + "\n\n把建议转告用户让他决定；他说「照发」就带 "
                  "acknowledge_risk=true 再调一次。",
                risk=risk, shot_id=shot["id"],
            )

        provider = shot["model_provider"]
        api_key = (_settings(db).get("api_keys") or {}).get(provider, "")
        adapter = get_adapter(provider, api_key=api_key,
                              config={"model_name": shot["model_name"]})
        # ★★ 2026-10-22：通道没有平台 Draft 档时**不要直接报错退出**。
        #
        #   用户指定要走 token6688 网关的 Seedance 2.0「特价按秒」通道，
        #   而这个网关没有 Draft 模式 ⇒ 原来这里 return err，
        #   整条「480P 小样」步骤直接死掉，用户只能被迫跳到最贵的成片档。
        #
        #   正确做法：**降级成 480p 直出**。
        #   480p 本身就是便宜档（按秒计费、分辨率最低），
        #   保留了这一步的核心价值 —— 花小钱先看一版活动影像；
        #   只是没有"抽卡 4 次省 57%"那个平台级优惠而已。
        #   返回里会如实标注 drafts_as="480p 直出（通道无 Draft 档）"。
        _draft_native = bool(getattr(adapter, "supports_draft", False))
        _draft_as = ""
        if not _draft_native:
            _draft_as = "480p 直出（该通道无平台 Draft 档，按秒计费的最低分辨率）"
            logger.info("通道 %s 无 Draft 档 → 本次按 %s 出样片",
                        provider, _draft_as)

        refs = []
        for cid in (shot.get("character_ids") or []):
            c = db.get_character(cid) if cid else None
            if c and c.get("reference_image_path"):
                r = to_image_ref(c["reference_image_path"])
                if r:
                    refs.append(r)
        # ★★ 2026-09-28 第 74 轮：**场景图也要进**（用户原话：
        #   「生成的视频要用指定的资产里面的角色+场景来生成」）。
        #   原来这里只收角色图 —— 于是场景资产对成片毫无影响。
        try:
            _cinfo = collect_refs(db, shot)
            refs = _cinfo["refs"]          # 角色（前）+ 场景（后），顺序与适配器约定同源
            logger.info("这一镜的参考图：角色 %d/%d、场景 %s",
                        _cinfo["characters_with_image"], _cinfo["characters_total"],
                        "有" if _cinfo["has_scene_image"] else "无")
        except Exception as _e:  # noqa: BLE001
            logger.warning("收参考图失败（沿用只收角色图）：%s", _e)
        if not refs:
            return err(
                "这一镜**一张参考图都没有**（角色/场景都没挂上或都没有图）——"
                "发出去等于让模型自己编人编景。先 relink_shots 关联资产，"
                "或给资产补图，再回来出片。",
                shot_id=shot["id"],
                next_step="relink_shots → list_assets 确认都有参考图 → 再 generate_preview",
            )

        # ★★ 2026-09-29（用户第 2 条：「资产里的角色图**必须真能用上**」）
        #
        #   真跑实测：`doubao-seedance-1-0-pro-250528` 被平台以
        #     `task_type r2v does not support model …` 拒收参考图。
        #   也就是说：**挂着这个模型出片，你的角色/场景图一定用不上**。
        #
        #   旧行为：适配器悄悄摘掉参考图、照样生成 —— 钱花了，
        #   出来的人不像你的人。用户的原话是「必须真能用上」，
        #   所以这里**宁可拦住、也不悄悄花这笔钱**。
        #   要强行出（接受"这一条不带参考图"）就显式给 allow_no_reference=true。
        _rs_gate = _reference_capability_gate(db, shot, refs,
                                              bool(args.get("allow_no_reference")))
        if _rs_gate:
            return _rs_gate

        from core.orchestrator import _compose_full_prompt
        settings = _settings(db)
        dur = int(shot.get("duration_seconds") or 5)
        dur = max(adapter.min_duration, min(dur, adapter.max_duration))
        # ★ 转镜衔接：**按 shot_chain 的判定**决定要不要接上一镜的尾帧。
        #   平台 first_frame 与 reference_image **互斥** —— 接龙时这一镜
        #   就**不能**带角色/场景参考图（身份靠提示词锚定）。
        #   不接龙时相反：带参考图，first_frame 留空。
        _chain_ref, _relay, _relay_why = _chain_first_frame(db, shot)
        _write_chain_constraint(db, shot, _chain_ref)
        if _relay:
            logger.info("镜头 %s 走接龙（%s）", shot.get("id"), _relay_why)
        req = VideoGenRequest(
            prompt=_compose_full_prompt(shot) or (shot.get("layer1_overview") or ""),
            duration=dur,
            aspect_ratio=shot.get("aspect_ratio") or "16:9",
            resolution="480p",
            # ★ 接龙时**不带**参考图（平台互斥）；不接龙时带参考图。
            character_reference=[] if _relay else refs,
            # ★ 2026-10-23（用户实拍：「480P 分镜视频跟白膜视频没什么关系，
            #   就是按提示词生成的」）—— 白膜本来是"先验构图"，但出片只看接龙首帧。
            #   现在兜底：没有接龙首帧时，用**白膜首帧**当 first_frame，
            #   让成片的构图/走位/机位跟白膜一致（用户看到的就是预演过的那个调度）。
            first_frame=(_chain_ref or _previz_first_frame(shot) or None),
            # ★ 2026-10-23：有台词就让**视频模型自己说话**（音画同源、口型自然），
            #   不再依赖出片后的 TTS 配音（用户："台词要在 480p 视频里出现"）。
            with_audio=_shot_has_dialogue(shot),
            # ★ 通道没有 Draft 档时（token6688 网关）传 False：
            #   走普通 480p 生成，不假装是草稿档。
            draft=_draft_native,
        )

        # ★★ 白膜必须**在这一档就喂进去**（G1 → G2 的最后一公里）。
        #   用户原话："我要求白膜就是为了生成视频前先确认要生成的动向之类的，
        #   再用低价的视频 token 覆盖" —— 那么"低价那一档"就必须已经吃到白膜，
        #   否则草稿确认的是一个**没受白膜约束**的东西，升到成片自然也丢了。
        #   在此之前 `t_generate_preview` 里**一个 previz 都没提**，
        #   白膜只在成片那条路（orchestrator/longvideo）生效。
        previz_info: Dict[str, Any] = {}
        try:
            from core import previz_ref as _pr
            ref = _pr.decide(shot, provider, settings=settings)
            previz_info = ref.to_dict()
            if ref.usable or ref.prompt_note:
                _pr.apply_to_request(req, ref)
            previz_info["applied"] = {
                "added_video_ref": bool(ref.usable and ref.video_ref),
                "added_prompt_note": bool(ref.prompt_note),
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("样片白膜注入失败（按不带白膜继续）：%s", e)
            previz_info = {"usable": False, "reason": f"白膜注入失败：{e}"}

        from core.previz_ref import require_reference_quality
        _reference_quality = require_reference_quality(shot, provider, shot.get("model_name") or "", settings)
        if _reference_quality is not None and not getattr(req, "video_url", None):
            raise ValueError("白膜质量通过但参考视频未实际加入请求，已停止付费生成")
        from core.reference_delivery import prepare_reference_spend, record_reference_spend
        _spend = prepare_reference_spend(db, shot, req.duration, req.resolution)
        res = await adapter.generate(req)
        if res.success:
            _charge = record_reference_spend(db, shot, _spend, res.task_id or "")
        await adapter.close()

        if not res.success:
            return err(f"样片生成失败：{res.error}", shot_id=shot["id"],
                       previz=previz_info)

        # ★★ 这一版**到底是不是**低价草稿档？不能想当然（第 70 轮）。
        #   实测：`doubao-seedance-1-0-pro` 在 t2v 下被平台拒 draft，
        #   适配器自愈后**去掉了 draft 重发** —— 片子出来了，但那是**成片档**，
        #   钱也按成片档花掉了。界面上必须如实说，否则用户以为自己走的是
        #   "低价覆盖"那条路，实际上一次都没省。
        really_draft = bool(getattr(res, "is_draft", False))
        draft_note = ""
        if not really_draft and _draft_as:
            # 通道本来就没有 Draft 档（token6688 网关）：这不是"模型拒了 draft"，
            # 而是这条通道按 480p 直出。说清楚即可，不必吓唬用户。
            draft_note = (
                f"说明：这一次是按 **{_draft_as}** 出的，"
                f"不是平台的低价抽卡档。分辨率 480p（按秒计费的最低档），"
                f"适合先看构图/动作；满意后再出 1080p 成片。")
        elif not really_draft:
            ds = {}
            try:
                from core import tiers as _tiers
                ds = _tiers.draft_support(shot.get("model_name") or "",
                                          adapter_says=True)
            except Exception:  # noqa: BLE001
                ds = {}
            draft_note = (
                f"⚠ 这次**不是低价草稿档**：模型 {shot.get('model_name') or provider} "
                f"拒绝了 draft 参数，平台按**成片档**跑完的（钱也按成片档计）。"
                + (f"实测记录：{ds.get('evidence') or ds.get('note') or ''}"
                   if ds else "")
                + " 想要真正的「低价草稿 → 高档成片」，需要换一个支持 draft 的模型。")

        db.update_shot(shot["id"], task_id=res.task_id,
                       previz_status=pv.S_DRAFT_READY)
        # ★★ 2026-09-29（用户第 2 条「资产里的角色图必须真能用上」）：
        #   适配器在平台拒绝时**摘掉过东西**（参考图 / draft），
        #   它把清单记在 `raw["_degraded"]`。**必须往上带** ——
        #   "这一条没有用上你的角色图"是用户最在意的事，
        #   悄悄发生等于又骗了他一次（他原话就是这么抱怨的）。
        _degraded: List[str] = []
        try:
            _raw = getattr(res, "raw", None) or {}
            if isinstance(_raw, dict):
                _degraded = [str(x) for x in (_raw.get("_degraded") or [])]
        except Exception:  # noqa: BLE001
            _degraded = []
        _ref_dropped = any("参考图" in x for x in _degraded)
        _degrade_warning = ""
        if _degraded:
            _degrade_warning = (
                "⚠ 为了让这一条能出来，平台拒绝的参数被**摘掉过**："
                + "、".join(_degraded) + "。"
                + ("**所以这一条没有用上你资产里的角色/场景图** —— "
                   "想真的用上，得换一个支持「参考图生视频（r2v）」的模型。"
                   if _ref_dropped else ""))
        # ★★ 第 69 轮：**立刻落盘并写回分镜**（否则合成器找不到、还会重复花钱）
        saved = await _persist_clip(db, str(shot.get("project_id") or ""), shot["id"],
                                    res.video_url or "", source="generate_preview(480p)",
                                    task_id=res.task_id or "")
        if not saved.get("ok"):
            if saved.get("no_fallback"):
                return err(saved.get("error") or "480P 与白膜不一致，未交付", shot_id=shot["id"],
                           reference_delivery=saved, requires_user_review=True, no_fallback=True)
            return err(f"样片生成成功但**没能存下来**：{saved.get('error')}。"
                       f"（视频在平台上，链接 24 小时内有效，你可以先点开看："
                       f"{str(res.video_url or '')[:200]}）", shot_id=shot["id"])
        return ok(
            shot_id=shot["id"],
            path=saved.get("path"),
            clip_url=saved.get("url"),
            bytes=saved.get("bytes"),
            candidate_id=saved.get("candidate_id"),
            draft_task_id=res.task_id,
            video_url=res.video_url,
            resolution="480p",
            is_draft=really_draft,
            draft_as=_draft_as,
            money_warning=draft_note,
            degraded=_degraded,
            used_reference_images=not _ref_dropped,
            degrade_warning=_degrade_warning,
            previz=previz_info,
            note=("样片已存到本地并写回这个镜头 ✓（合成时直接用它，"
                  "重跑也不会重复花钱）。" if really_draft else
                  ("片段已存到本地并写回这个镜头 ✓（" + _draft_as + "）。"
                   if _draft_as else
                   "片段已存到本地并写回这个镜头 ✓，但它**不是低价草稿档**"
                   "（见 money_warning）。")),
            next_step=("让用户看这版样片。满意 → generate_final(shot_id, draft_task_id=上面这个)；"
                       "不满意 → review_previz 驳回并给理由码，别自己猜。")
            + (f"（白膜：{previz_info.get('reason')}）" if previz_info.get("reason") else ""),
        )
    except Exception as e:
        logger.exception("generate_preview failed")
        return err(f"出样片失败：{type(e).__name__}: {e}")


# ──────────────────────── 4. 成片（贵，G3）────────────────────────


@REGISTRY.register(
    "generate_final",
    "出 1080P 成片。**这是整个流程里最贵的一步**。"
    "如果之前出过草稿样片，把 draft_task_id 带上 —— 这样模型会继承样片的"
    "prompt/参考素材/seed，结构构图动作都跟样片高度一致，不用重抽。"
    "★ 长镜头（超过模型单段上限）会自动分段，段与段之间保证时长守恒。",
    {"shot_id": {"type": "string", "desc": "分镜 id", "required": True},
     "draft_task_id": {"type": "string", "desc": "可选。带上就用这版草稿出成片（推荐）"},
     "confirm": {"type": "boolean", "desc": "必须显式传 true，表示用户已确认要花这笔钱"},
     "acknowledge_risk": {"type": "boolean",
                          "desc": "内容预检判定高风险时，用户明确说「我知道有风险，照发」才传 true。"
                                  "不传就会被拦下并给出修改建议（这是为了省掉「跑完才被拒还计费」的浪费）"}},
    writes=True,
)
async def t_generate_final(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "shot_id")
    if bad:
        return bad
    if not args.get("confirm"):
        return err(
            "出成片是整个流程里最贵的一步，必须先拿到用户的明确同意。"
            "请先用一句话把'要花多少钱、出几段、多长'告诉用户，用户同意后再带 "
            "confirm=true 调用。"
        )
    try:
        db, _, _ = _services()
        shot, e = _shot_or_err(db, args["shot_id"])
        if e:
            return e

        from core import longvideo as lv
        settings = _settings(db)
        provider = shot["model_provider"]
        api_key = (settings.get("api_keys") or {}).get(provider, "")

        # ★★ 出片前的**内容风险预检** —— 这是唯一能防住"白花钱"的环节。
        #   实测被拦一次：`OutputVideoSensitiveContentDetected.PolicyViolation`，
        #   **跑了 93 秒才拒，而且照样计费**。预检免费，能把这种浪费挡在前面。
        #   注意：它是**建议**不是硬拦 —— 只在高风险时要用户明确点头一次。
        risk = _risk_check(db, shot)
        if risk.get("level") == "high" and not args.get("acknowledge_risk"):
            return err(
                "内容预检发现高风险，**这次先不发**（发了很可能被拦、而且被拦也计费）。\n"
                + risk["summary"] + "\n"
                + "\n".join(f"· {a}" for a in risk["advice"])
                + "\n\n命中：" + "、".join(h["term"] for h in risk["hits"][:6])
                + "\n\n请先把建议转告用户、让他决定：\n"
                  "  · 改提示词后再来（推荐）\n"
                  "  · 或明确说「我知道有风险，照发」，那就带 "
                  "acknowledge_risk=true 再调一次。",
                risk=risk, shot_id=shot["id"],
            )

        # ★★ 2026-09-28 第 74 轮：成片这条路同样要过**资产门**与**白膜门**。
        #   用户的两句原话：
        #     「生成的视频要用指定的资产里面的角色+场景来生成」
        #     「要先生成白膜视频，让用户确认；用户确认通过后再生成对应的视频」
        #   成片最贵，所以这两道门在这里更不能少。
        from core import previz as _pv
        _st = _pv.get_state(shot)
        if _st["status"] not in (_pv.S_DRAFT_APPROVED, _pv.S_FINAL_READY):
            return err(
                f"白膜还没确认（当前 {_st['status']}）—— 成片是最贵的一步，"
                f"必须先用白膜把构图/走位定下来、并经用户确认。"
                f"先 render_previz 给用户看，他满意后 review_previz(approve)，再回来出成片。",
                shot_id=shot["id"], previz_status=_st["status"],
            )
        _g = refs_gate(db, shot)
        if _g:
            return _g

        # ★ 成片这一步是**最贵**的一步，更不能用"没有角色/场景"的模型硬出。
        _rsg = _reference_capability_gate(
            db, shot, collect_refs(db, shot)["refs"],
            bool(args.get("allow_no_reference")), stage="出成片")
        if _rsg:
            return _rsg

        # ★★ 2026-09-28 第 8 条（用户的顺序要求）：
        #   「无误后把生成 480p 的视频**升级**到指定的画质，**不是重新生成**，这样节约成本」。
        #
        #   机制本来就有：出成片时带 `draft_task_id`，平台按"**用这版草稿出成片**"
        #   处理（seedance/即梦的 draft_task 语义），不重抽一版。
        #   但它**依赖模型在下一轮记得把 id 传回来** —— 实测这不可靠
        #   （模型会忘、会编，`draft_task_id` 还出现过被塞进说明文字的情况）。
        #   而 `generate_preview` 早就把草稿任务 id 写进了 `shot.task_id`。
        #   ⇒ 这里**默认就用它**：没显式传 draft_task_id 时，自动按"升级"走。
        #   这样"升级"就是**代码保证**的，不靠模型记性，也真的省钱。
        draft_id = str(args.get("draft_task_id") or "").strip()
        draft_from_shot = ""
        if not draft_id:
            try:
                _cands = shot.get("candidates")
                if isinstance(_cands, str):
                    import json as _j
                    _cands = _j.loads(_cands) if _cands.strip() else []
                _has_draft = any(
                    isinstance(c, dict) and "draft" in str(c.get("source") or "").lower()
                    for c in (_cands or []))
                _tid = str(shot.get("task_id") or "").strip()
                # ★ 判据必须**严格**：只有"候选里确实有一条 480P 草稿"时才自动沿用。
                #   第一版顺手把 `previz_status == draft_approved` 也算进来 ——
                #   那是"**白膜**看过并同意"，**不代表出过 480P 草稿**，
                #   会把成片档的 task_id 当草稿传上去（守卫测试当场抓到）。
                if _tid and _has_draft:
                    draft_id = _tid
                    draft_from_shot = _tid
            except Exception as _e:  # noqa: BLE001
                logger.warning("自动取草稿任务 id 失败（按常规路径出成片）：%s", _e)

        if draft_id:
            # 凭草稿出成片：单段，参数继承草稿
            from core.adapters import VideoGenRequest, get_adapter, to_image_ref
            adapter = get_adapter(provider, api_key=api_key,
                                  config={"model_name": shot["model_name"]})
            # ★ 角色（前）+ 场景（后）—— 与草稿那条路同一套收集逻辑，不各写一份
            try:
                refs = collect_refs(db, shot)["refs"]
            except Exception as _e:  # noqa: BLE001
                logger.warning("成片收参考图失败（只收角色图兜底）：%s", _e)
                refs = []
                for cid in (shot.get("character_ids") or []):
                    c = db.get_character(cid) if cid else None
                    if c and c.get("reference_image_path"):
                        r = to_image_ref(c["reference_image_path"])
                        if r:
                            refs.append(r)
            from core.orchestrator import _compose_full_prompt
            # ★ 转镜衔接：成片这条路的首帧也要接上一镜的尾帧，
            #   否则小样是接着的、升到成片反而断开。
            _chain_ref_final, _relay_final, _relay_why_f = _chain_first_frame(db, shot)
            _write_chain_constraint(db, shot, _chain_ref_final)
            req = VideoGenRequest(
                prompt=_compose_full_prompt(shot) or (shot.get("layer1_overview") or ""),
                duration=int(shot.get("duration_seconds") or 5),
                aspect_ratio=shot.get("aspect_ratio") or "16:9",
                resolution="1080p",
                character_reference=[] if _relay_final else refs,
                first_frame=_chain_ref_final or None,
                draft_task_id=draft_id,
            )
            from core.previz_ref import require_reference_quality
            _reference_quality = require_reference_quality(shot, provider, shot.get("model_name") or "", settings)
            if _reference_quality is not None and not getattr(req, "video_url", None):
                raise ValueError("此草稿升级路径没有可核验的原始白膜参考来源，已停止付费生成；请使用带白膜视频的标准生成路径")
            from core.reference_delivery import prepare_reference_spend, record_reference_spend
            _spend = prepare_reference_spend(db, shot, req.duration, req.resolution)
            res = await adapter.generate(req)
            if res.success:
                _charge = record_reference_spend(db, shot, _spend, res.task_id or "")
            await adapter.close()
            if not res.success:
                return err(f"成片生成失败：{res.error}", shot_id=shot["id"])
            # ★★ 第 70 轮：这条路以前**只改状态、不下载**（`video_url` 直接返回，
            #   链接 24 小时过期）→ 合成器找不到素材、重跑还会重复花钱。
            #   这是 `_persist_clip` 唯一漏掉的一条生成路径。
            saved = await _persist_clip(db, str(shot.get("project_id") or ""), shot["id"],
                                        res.video_url or "",
                                        source="generate_final(1080p,from_draft)",
                                        task_id=res.task_id or "")
            if saved.get("ok"):
                db.update_shot(shot["id"], previz_status="final_ready")
            if not saved.get("ok"):
                if saved.get("no_fallback"):
                    return err(saved.get("error") or "成片与白膜不一致，未交付", shot_id=shot["id"],
                               reference_delivery=saved, requires_user_review=True, no_fallback=True)
                return err(f"成片生成成功但**没能存下来**：{saved.get('error')}。"
                           f"（视频在平台上，24 小时内可先点开看："
                           f"{str(res.video_url or '')[:200]}）", shot_id=shot["id"])
            return ok(shot_id=shot["id"], path=saved.get("path"),
                      clip_url=saved.get("url"), bytes=saved.get("bytes"),
                      candidate_id=saved.get("candidate_id"),
                      video_url=res.video_url,
                      resolution="1080p", from_draft=True,
                      upgraded_from_draft=draft_id,
                      draft_source=("调用方显式指定" if not draft_from_shot
                                    else "自动沿用这个镜头的 480P 草稿任务（省钱：升级而不是重抽）"),
                      note="成片已存到本地并写回这个镜头 ✓（不用再下载一遍）。",
                      next_step="成片出来了。逐镜都通过之后就可以拼片了。")

        # 常规路径：走长视频分段（策略/排片/续接/复用都在 core.longvideo）
        from core.orchestrator import generate_long_video, plan_long_video
        plan = await _call(plan_long_video, shot["id"], db, settings)
        res = await _call(generate_long_video, shot["id"], db, None, settings)
        # ★ `previz` 必须透出去。`generate_long_video` 里算了"白膜到底带没带上、
        #   以什么方式带的"，但这里原来没把它放进返回值 —— 于是用户永远看不到
        #   白膜有没有真的起作用（我自己跑 e2e 时看到的就是 `白膜注入: None`）。
        #   白膜流程的价值一半在于"用户知道它生效了"，不透出来等于白做。
        pv_info = res.get("previz") or {}
        return ok(
            shot_id=shot["id"],
            ok=res.get("ok"),
            strategy=res.get("strategy"),
            segment_count=res.get("segment_count"),
            reused=res.get("reused"),
            clips=res.get("clips"),
            plan=plan.get("segments"),
            previz=pv_info,
            error=res.get("error"),
            next_step=("分段都成了，可以拼片。" if res.get("ok")
                       else "有段没成。失败的段可以直接重跑 —— 已成功的段会被复用，不会重复花钱。")
            + (f"（白膜：{pv_info.get('reason')}）" if pv_info.get("reason") else ""),
        )
    except Exception as e:
        logger.exception("generate_final failed")
        return err(f"出成片失败：{type(e).__name__}: {e}")


# ──────────────────────── 5. 进度 ────────────────────────


@REGISTRY.register(
    "get_shot_progress",
    "看某个镜头走到哪一步了：白膜/样片/成片分别是什么状态、重试了几次、"
    "有没有生成好的片段。用户问'做到哪了'就用这个。",
    {"shot_id": {"type": "string", "desc": "分镜 id", "required": True}},
)
async def t_get_shot_progress(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "shot_id")
    if bad:
        return bad
    try:
        from core import previz as pv
        db, _, _ = _services()
        shot, e = _shot_or_err(db, args["shot_id"])
        if e:
            return e

        st = pv.get_state(shot)
        segs = shot.get("segments")
        if isinstance(segs, str):
            import json as _json
            try:
                segs = _json.loads(segs)
            except Exception:
                segs = []
        segs = segs or []
        ok_segs = [s for s in segs if s.get("status") == "ok"]

        label = pv.state_label(st["status"])
        # ★★ 三级链路（G1 白膜 → G2 草稿 → G3 成片）要**如实报**（第 70 轮）。
        #   用户的原话是「做到我能用好」—— 那 agent 在对话里就必须说得出
        #   "现在在哪一档、下一档花多少钱、以及**你的模型到底有没有便宜那一档**"。
        try:
            from core import tiers as _tiers
            tinfo = _tiers.shot_tiers(shot, settings=_settings(db))
        except Exception as e:  # noqa: BLE001
            logger.warning("算三级链路状态失败：%s", e)
            tinfo = {}
        # ★ 状态名 `white_model_running` 有歧义（既表示 G1 渲白膜、也表示
        #   G2 重出样片）。这里补一句"白膜还在不在"，别让用户以为白膜被重渲了。
        previz_kept = bool(st["path"]) and st["status"] in (
            pv.S_DRAFT_READY, pv.S_DRAFT_APPROVED, pv.S_FINAL_RUNNING,
            pv.S_FINAL_READY)
        hint = ""
        if st["status"] == pv.S_DRAFT_RUNNING and st["path"]:
            hint = ("（白膜是现成的、不会重渲 —— 驳回画风/长相类问题只重出样片，"
                    "这正是省钱的开关）")

        return ok(
            shot_id=shot["id"],
            status=st["status"],
            status_text=label + hint,
            tiers=tinfo,
            next_action=tinfo.get("next_action") or "",
            next_why=tinfo.get("next_why") or "",
            money_warning=tinfo.get("money_warning") or "",
            previz_kept=previz_kept,
            previz_path=st["path"],
            final_approved=st["final_approved"],
            retries_used=st["retries"],
            retry_limits=pv.GATE_RETRIES,
            segments={"total": len(segs), "ok": len(ok_segs),
                      "failed": len(segs) - len(ok_segs)},
            clips=[s.get("path") for s in ok_segs if s.get("path")],
            shot_status=shot.get("status"),
            error_message=shot.get("error_message"),
            history=st["history"][-6:],
        )
    except Exception as e:
        return err(f"查进度失败：{type(e).__name__}: {e}")


# ──────────────────────── 6. 可灵 Element（角色一致性）────────────────────────


@REGISTRY.register(
    "register_kling_elements",
    "给项目里的角色注册可灵 Element —— 这是**可灵这条线拿到角色一致性的唯一办法**。"
    "可灵不是靠发参考图锁人物，而是先把这个角色注册成一个 Element，"
    "拿到系统生成的 element_id，生成时在 prompt 里用 `@角色名` 指代。"
    "不注册的话，可灵生成出来的人物每个镜头都不一样。"
    "★ 只有用可灵(kling)做视频模型的项目才需要做这一步。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "force": {"type": "boolean", "desc": "true = 已有 element_id 的也重新注册"}},
    needs_project=True,
    writes=True,
)
async def t_register_kling_elements(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    try:
        from core.kling_element import ensure_character_elements
        db, _, _ = _services()
        settings = _settings(db)
        api_key = (settings.get("api_keys") or {}).get("kling", "")
        if not api_key:
            return err(
                "可灵 API key 没配，注册不了 Element。"
                "请到「设置 → API Key」里填可灵的 key；"
                "或者改用即梦/Seedance（它靠发参考图锁角色，不需要这一步）。"
            )

        res = await ensure_character_elements(
            db, args["project_id"], api_key=api_key,
            force=bool(args.get("force")),
        )
        return ok(**merged(res, project_id=args["project_id"]))
    except Exception as e:
        logger.exception("register_kling_elements failed")
        return err(f"注册 Element 失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "list_kling_elements",
    "看项目里哪些角色已经注册了可灵 Element、哪些还没有。"
    "用可灵做视频之前先查这个，缺的补注册。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True}},
    needs_project=True,
)
async def t_list_kling_elements(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    try:
        db, _, _ = _services()
        chars = await _call(db.list_characters, args["project_id"]) or []
        ready, missing, no_img = [], [], []
        for c in chars:
            name = c.get("name") or "(未命名)"
            eid = (c.get("element_id") or "").strip()
            if eid:
                ready.append({"name": name, "element_id": eid})
            elif not c.get("reference_image_path"):
                no_img.append({"name": name, "reason": "没有参考图，注册不出形象"})
            else:
                missing.append({"name": name})
        return ok(
            project_id=args["project_id"],
            registered=ready,
            not_registered=missing,
            cannot_register=no_img,
            hint=("已注册的才能在可灵里锁定人物形象；"
                  "缺的调 register_kling_elements 补。"
                  "一次生成最多引用 3 个 Element。"),
        )
    except Exception as e:
        return err(f"查 Element 失败：{type(e).__name__}: {e}")


# ──────────────────────── 7. 整片合成 ────────────────────────


@REGISTRY.register(
    "fix_missing_scenes",
    "**给没有场景卡的镜头补场景**。"
    "什么时候用：镜头只写了文字（例如「金殿之上，一场棋局…」），"
    "但资产里没有对应的场景卡 —— 那样场景参考图永远用不上，"
    "出片只能靠模型自己编环境。\n"
    "★ 两步走，**不许一步做完**：\n"
    "  · `apply=false`（默认）→ 只**提草案**（每个镜头的建议名字 + 原文）；"
    "拿到后把草案说给用户听（名字可以让他改）；\n"
    "  · 用户点头后再 `apply=true` + `items=[{\"shot_id\":…, \"name\":\"…\"}]` → 建卡并挂钩。\n"
    "为什么必须让用户过一眼：名字是从分镜文字里抠出来的，可能不准。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "apply": {"type": "boolean", "desc": "true 才真的建卡（默认 false=只提草案）"},
     "items": {"type": "array", "desc": "apply=true 时要建的清单：[{shot_id, name}]"}},
    needs_project=True, writes=True,
)
async def t_fix_missing_scenes(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    try:
        from core import relink as _rl
        db, _, _ = _services()
        pid = args["project_id"]
        if not args.get("apply"):
            drafts = _rl.propose_scenes_for_shots(db, pid)
            if not drafts:
                return ok(count=0, drafts=[],
                          note="所有镜头都已经关联了场景卡，不需要补。")
            return ok(
                count=len(drafts), drafts=drafts,
                ask_user=("这 %d 个镜头还没有场景卡：%s。"
                          "我可以按分镜文字建这几张：%s —— 名字可以改，"
                          "确认后我就建卡并挂上去。"
                          % (len(drafts), "、".join("第%d镜" % d["order"] for d in drafts),
                             "、".join(d["suggested_name"] for d in drafts))),
                next_step=("把草案念给用户听（含建议的名字），等他确认/改名；"
                           "然后再调一次 apply=true + items=[...]。"))
        items = args.get("items")
        if not isinstance(items, list) or not items:
            return err("apply=true 时要给 items：[{shot_id, name}]"
                       "（可以先用 apply=false 拿草案）")
        r = await _call(_rl.create_scenes_for_shots, db, pid, items)
        return ok(**merged(r, next_step=(
            "场景卡建好了。接着给它们出参考图（make_looks），"
            "这样出片时环境参考才会真的带上。")))
    except Exception as e:  # noqa: BLE001
        logger.exception("fix_missing_scenes failed")
        return err(f"补场景失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "merge_characters",
    "把**同一个人的两张角色卡合并成一张**。"
    "什么时候用：`list_assets` / 卡片上提示「疑似重复」时（例如同时存在「兵丁甲」和"
    "「稽查兵丁甲」）。不合并的后果很直接：定妆照那一步的判据是「每张卡都要有图」，"
    "重复卡会让它**永远做不完**，于是每次都问用户一遍。\n"
    "★ 保留哪张**必须由用户指定**（`keep_id`）—— 合并不可逆地删掉另一张，"
    "选错会丢描述。先调 `find_duplicate_characters` 看有哪些重复组。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "keep_id": {"type": "string", "desc": "要保留的那张卡的 id", "required": True},
     "drop_id": {"type": "string", "desc": "要被合并掉的那张卡的 id", "required": True}},
    needs_project=True, writes=True,
)
async def t_merge_characters(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id", "keep_id", "drop_id")
    if bad:
        return bad
    try:
        from core import charmerge as _cm
        db, _, _ = _services()
        r = await _call(_cm.merge_characters, db, args["project_id"],
                        args["keep_id"], args["drop_id"])
        if not r.get("ok"):
            return err(r.get("error") or "合并失败")
        # ★ 用 `merged(...)` 而不是裸展开 `ok(**r, …)`：`r` 里带着 `ok` 键，
        #   展开会与 `ok()` 的关键字撞名（仓库守卫测试专门盯这条）。
        return ok(**merged(r, next_step=(
            "合并完成了。再调 list_assets 看一眼还剩哪些卡没图，"
            "然后给它们出定妆照（make_looks）。")))
    except Exception as e:  # noqa: BLE001
        logger.exception("merge_characters failed")
        return err(f"合并角色失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "find_duplicate_characters",
    "列出**疑似同一个人的多张角色卡**（名字互相包含，例如「兵丁甲」/「稽查兵丁甲」）。"
    "免费、本地。给用户看这个列表，让他决定保留哪张，再用 merge_characters 合并。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True}},
    needs_project=True,
)
async def t_find_duplicate_characters(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    try:
        from core import charmerge as _cm
        db, _, _ = _services()
        groups = await _call(_cm.find_duplicate_characters, db, args["project_id"])
        return ok(count=len(groups), groups=groups,
                  note=("没有发现重复的角色卡" if not groups else
                        f"发现 {len(groups)} 组疑似重复 —— 让用户决定保留哪张，"
                        f"再调 merge_characters(keep_id, drop_id)。"))
    except Exception as e:  # noqa: BLE001
        logger.exception("find_duplicate_characters failed")
        return err(f"查重复角色失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "relink_orphan_clips",
    "★ 把**磁盘上已经生成好、但库里失联了的片段**认回来（按镜头顺序挂回）。"
    "什么时候用：分镜被 `plan_shots(replace=true)` 重建过之后 —— "
    "文件还在磁盘上，但分镜的 candidates 空了，于是配音说「一个镜头都没配上」、"
    "合成说「拼出来会是无声片」。这时调它就能救回来（配音版会优先作为选中片段）。\n"
    "先 `apply=false` 干跑看一眼它打算做什么，确认无误再 `apply=true` 真挂。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "apply": {"type": "boolean",
               "desc": "true=真的写库；false=只报告打算做什么（推荐先干跑）"}},
    needs_project=True, writes=True,
)
async def t_relink_orphan_clips(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    try:
        from core import recover as _rc
        db, _, _ = _services()
        apply = bool(args.get("apply", True))
        r = await _call(_rc.relink_orphan_clips, db, args["project_id"], apply=apply)
        if not r.get("ok"):
            return err(r.get("reason") or r.get("error") or "认不回来", **{
                k: v for k, v in r.items() if k not in ("ok", "reason", "error")})
        # ★ 不写 `ok(**r, next_step=…)` —— `r` 里本身就有 `ok` 键，
        #   展开会与 `ok()` 的关键字撞名（这个仓库有专门的守卫测试盯这条）。
        payload = {k: v for k, v in r.items() if k != "ok"}
        payload["next_step"] = (
            "认回来了。再调 list_shots 确认每个镜头的候选数；"
            "然后就能继续配音/合成（选中项是配音版 ⇒ 成片有声）。")
        return ok(**payload)
    except Exception as e:  # noqa: BLE001
        logger.exception("relink_orphan_clips failed")
        return err(f"认回片段失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "relink_shots",
    "★ 把已有分镜**重新关联**到项目的角色 / 场景（原地补，不重建分镜、"
    "不会丢已经生成好的视频）。\n"
    "什么时候必须调：`plan_shots` / `get_project_status` 告诉你"
    "「有 N 个镜头没关联角色」时。**没关联角色的镜头，出片时拿不到参考图** ——"
    "结果就是「每个镜头一张脸」。\n"
    "调完它会如实告诉你还剩几个没挂上、以及为什么（通常是分镜里的名字和"
    "「角色」页的名字对不上）。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True}},
    needs_project=True,
    writes=True,
)
async def t_relink_shots(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    try:
        from core import relink as _rl
        db, _, _ = _services()
        r = await _call(_rl.relink_project_shots, db, args["project_id"])
        if not r.get("ok"):
            return err(r.get("error") or "重新关联失败")
        return ok(**merged(r, next_step=(
            "关联好了。再调 list_shots 确认每个镜头的 characters 数量；"
            "还有没挂上的话，把 hint 里说的原因如实告诉用户"
            "（通常是分镜里的名字和角色页对不上）。")))
    except Exception as e:
        logger.exception("relink_shots failed")
        return err(f"重新关联失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "dub_project_voices",
    "★出有声成片前先调这个★。给**整个项目**的每个镜头按角色配音："
    "拉各厂商真实音色目录，按角色卡的性别/年龄/定位/personality 打分选音色"
    "（同一角色全片同一把嗓子、尽量不撞音），逐句按情绪算韵律，"
    "再把音轨**挂进这一镜**（只替换音轨，视频流原样 copy）。\n"
    "★ 它**不调用视频模型、不花视频的钱** —— 对已经生成好的素材也有效。"
    "没有台词的分镜不会自动加旁白，会如实说明。\n"
    "想一步到位就直接用 compose_film(with_voice=true)，不必先调这个。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "select": {"type": "boolean",
                "desc": "true（默认）= 把配好音的版本设为该镜选中候选，"
                        "这样后面合成会自动用带声音的那一版"}},
    needs_project=True,
    writes=True,
)
async def t_dub_project_voices(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    try:
        from core import dubbing as _dub
        db, _, _ = _services()
        res = await _dub.dub_project(db, args["project_id"],
                                     select=bool(args.get("select", True)))
        if not res.get("ok"):
            return err(res.get("error") or "配音失败", **{
                k: v for k, v in res.items() if k not in ("ok", "error", "status")})
        return ok(**merged(res, next_step=(
            "配音好了。接下来 compose_film（要字幕就 add_subtitles=true）拼成有声成片；"
            "返回里的 has_voice / audio.kbps 可以核对是不是真的有声音"
            "（静音轨是 2 kb/s 那种）。")))
    except Exception as e:
        logger.exception("dub_project_voices failed")
        return err(f"配音失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "compose_film",
    "把项目里已经生成好的镜头**拼成一条完整片子**（含接缝归一化，可选烧录字幕）。"
    "★ 这是「生成完所有镜头之后」的收尾步骤 —— 没有它，镜头全出来了也没法在对话里出片。"
    "会跳过还没有视频的镜头并如实告诉你跳了几个。\n"
    "★ 想要**有声成片**就传 with_voice=true：它会先按角色配音（不花视频的钱）再拼。"
    "用户明确要求过「每个角色都有自己适配的音色」，所以默认就该走这条路。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "shot_ids": {"type": "string",
                  "desc": "可选。只拼这些镜头，用逗号分隔；不填就拼全部"},
     "add_subtitles": {"type": "boolean", "desc": "是否烧录字幕（台词优先，没有台词才用动作描述）"},
     "with_voice": {"type": "boolean",
                    "desc": "true = 先按角色配音再拼（**推荐**，出有声成片）。"
                            "配音只替换音轨、不重跑视频模型，不额外花视频的钱"},
     "filename": {"type": "string", "desc": "输出文件名，默认 final.mp4"},
     "method": {"type": "string", "desc": "safe=接缝归一化(默认，推荐) / fast=裸拼接(快但可能丢帧)",
                "enum": ["safe", "fast"]},
     "transition": {"type": "string",
                    "desc": "转场风格。auto=逐接缝判断(默认推荐) / cut=全硬切 / "
                            "dissolve=全溶解 / dip=跨场景黑场",
                    "enum": ["auto", "cut", "dissolve", "dip"]},
     "transition_seconds": {"type": "number",
                            "desc": "溶解/黑场的秒数，不填用默认"}},
    needs_project=True,
    writes=True,
)
async def t_compose_film(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    try:
        from core.film import compose_film
        db, _, _ = _services()

        shot_ids = None
        raw = args.get("shot_ids")
        if raw:
            if isinstance(raw, str):
                shot_ids = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
            elif isinstance(raw, list):
                shot_ids = [str(x).strip() for x in raw if str(x).strip()]

        res = await compose_film(
            db, args["project_id"],
            shot_ids=shot_ids,
            output_filename=(args.get("filename") or "final.mp4"),
            add_subtitles=bool(args.get("add_subtitles")),
            method=(args.get("method") or "safe"),
            transition=(args.get("transition") or "auto"),
            transition_seconds=float(args.get("transition_seconds") or 0),
            # 2026-10-20: 原为 bool(args.get("with_voice")) —— 不传即 False，
            #   于是 Agent 走 compose_film 永远是**无声成片**，与工具描述
            #   「默认就该走这条路（每个角色都有自己适配的音色）」相反。
            #   与 main.py 各处 HTTP 端点一致，默认 True。
            with_voice=bool(args.get("with_voice", True)),
        )
        if not res.get("ok"):
            return err(res.get("error") or "合成失败", **{k: v for k, v in res.items()
                                                          if k not in ("ok", "error")})
        res["next_step"] = (
            f"成片已生成：{res.get('total_duration')} 秒、{res.get('clip_count')} 个镜头。"
            + (f"（有 {res['skipped_shots']} 个镜头还没有视频，被跳过了 —— "
               f"要让它们也进片子，先给它们出片再合成。）"
               if res.get("skipped_shots") else "")
            + ("★ 这版**有声音** ✓" if res.get("has_voice") else
               "⚠ 这版**没有真正的配音**（只有静音轨）—— "
               "带 with_voice=true 重跑一次就有声音了（不花视频的钱）。")
        )
        return ok(**res)
    except Exception as e:
        logger.exception("compose_film failed")
        return err(f"合成失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "set_shot_transition",
    "设定**某个镜头**的转场（这一刀怎么接上一个镜头）。"
    "存进分镜表，合成时自动生效 —— 不用每次在 compose_film 里重说一遍。\n"
    "语义：转场属于「进入这个镜头」的那一刀（与**前一个**镜头的接缝）。\n"
    "  · auto（默认）→ 交给拼接器逐接缝判断：能硬切就硬切、跳变大的用溶解摊薄、"
    "跨场景用黑场。**这是最省钱也最好看的行为**，没特别理由就别改。\n"
    "  · cut    → 这一刀强制硬切（显式指定会关掉该接缝的自动判断）\n"
    "  · dissolve → 溶解\n"
    "  · dip    → 黑场过渡（适合时间/空间大跨度）\n"
    "也可以一次给多个镜头批量设（shot_ids 用逗号分隔）。",
    {"shot_id": {"type": "string", "desc": "分镜 id（单个）"},
     "shot_ids": {"type": "string", "desc": "多个分镜 id，逗号分隔"},
     "transition": {"type": "string",
                    "desc": "auto / cut / dissolve / dip",
                    "enum": ["auto", "cut", "dissolve", "dip"], "required": True},
     "seconds": {"type": "number",
                 "desc": "溶解/黑场秒数；不填按镜头时长自动取（约 0.5 秒）"}},
    writes=True,
)
async def t_set_shot_transition(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "transition")
    if bad:
        return bad
    name = str(args["transition"]).strip().lower()
    if name not in ("auto", "cut", "dissolve", "dip"):
        return err(f"不认识的转场：{name}（可选 auto / cut / dissolve / dip）")
    ids: List[str] = []
    if args.get("shot_id"):
        ids.append(str(args["shot_id"]).strip())
    if args.get("shot_ids"):
        raw = args["shot_ids"]
        if isinstance(raw, list):
            ids += [str(x).strip() for x in raw if str(x).strip()]
        else:
            ids += [x.strip() for x in str(raw).replace("，", ",").split(",")
                    if x.strip()]
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return err("要给哪个镜头设转场？传 shot_id 或 shot_ids。")
    try:
        db, _, _ = _services()
        secs = float(args.get("seconds") or 0)
        done, missing = [], []
        for sid in ids:
            shot = db.get_shot(sid)
            if not shot:
                missing.append(sid)
                continue
            db.update_shot(sid, transition=name, transition_seconds=secs)
            done.append({"shot_id": sid,
                         "name": (shot.get("layer1_overview") or "")[:24],
                         "order": shot.get("order_index")})
        if not done:
            return err("这些分镜 id 都不存在：" + "、".join(missing[:5]))
        note = {
            "auto": "交给拼接器逐接缝判断（能硬切就硬切，跳变大的溶解，换场黑场）",
            "cut": "强制硬切 —— 这一刀不做过渡",
            "dissolve": "溶解（叠化）",
            "dip": "黑场过渡（先淡到黑再淡出）",
        }[name]
        return ok(updated=done, missing=missing, transition=name,
                  seconds=secs, meaning=note,
                  next_step=("合成时不填 transition 参数也会带上这些设置；"
                             "想再看一遍/清掉就重新调这个工具设成 auto。"))
    except Exception as e:
        logger.exception("set_shot_transition failed")
        return err(f"设转场失败：{type(e).__name__}: {e}")


# ──────────────────────── 11. 本机出图服务（ComfyUI）────────────────────────


@REGISTRY.register(
    "local_image_service",
    "本机出图服务（ComfyUI 8188 + 出图接口 8189）的状态与重启。\n"
    "★ 为什么需要它：本机出图能出图但**会崩** —— 实测确认的链条是\n"
    "  「系统提交内存/页面文件不够 → ComfyUI 卸/重装模型 → Windows 原生\n"
    "   access violation（Python 捕获不到，进程直接死）」。\n"
    "  崩了之后 8189 还活着、8188 死了，用户只会看到「连接被重置」。\n"
    "  这个工具把状态分开报（接口 / 引擎各自在不在），并且能一键重启。\n"
    "  action=restart 会用 `E:\\出图\\scripts\\start-qwen.bat` 重启\n"
    "  （那个脚本带 --lowvram，是实测下来最稳的启动方式）。",
    {"action": {"type": "string", "desc": "status（默认，只查）/ restart（重启）",
                "enum": ["status", "restart"]}},
    writes=True,
)
async def t_local_image_service(args: Dict[str, Any]) -> ToolResult:
    action = str(args.get("action") or "status").strip().lower()
    try:
        from core import imagegen as IG

        st = await IG.local_service_health("local")
        engine_ok = bool(st.get("ok"))
        if action == "status":
            return ok(
                engine_running=engine_ok,
                detail=st,
                available=engine_ok,
                how_to_start=("到 E:\\出图 跑 scripts\\start-qwen.bat（或 start-all.bat）"
                              if not engine_ok else ""),
                note=("本机出图一张要 2~5 分钟，属正常；失败原因分四类，"
                      "工具会分别讲清楚（服务没起 / 内存不够崩了 / 引擎报错 / 真超时）。"),
            )

        # restart
        import subprocess
        import sys
        bat = r"E:\出图\scripts\start-qwen.bat"
        if not os.path.exists(bat):
            return err(f"找不到启动脚本：{bat}",
                       hint="确认 E:\\出图 那套还在；或手动启动画图引擎。")
        try:
            subprocess.Popen(["cmd", "/c", "start", "", "/min", bat],
                             shell=False,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except Exception as e:  # noqa: BLE001
            return err(f"启动失败：{type(e).__name__}: {e}")
        # 等它就绪（最多 ~90 秒，与脚本里自己的等待一致）
        import asyncio as _a
        for _ in range(30):
            await _a.sleep(3)
            st2 = await IG.local_service_health("local")
            if st2.get("ok"):
                return ok(restarted=True, ready=True,
                          detail=st2,
                          note="本机出图服务已重启并就绪（带 --lowvram 启动）。")
        return ok(restarted=True, ready=False,
                  detail=await IG.local_service_health("local"),
                  note="脚本起来了但引擎还没就绪。再等一会儿用 status 查。")
    except Exception as e:
        logger.exception("local_image_service failed")
        return err(f"本机出图服务操作失败：{type(e).__name__}: {e}")


# ──────────────────────── 8. 白膜参考通道（长期功能）────────────────────────


@REGISTRY.register(
    "check_previz_channel",
    "检查「白膜当参考素材」这条通道通不通：隧道在不在、用的是哪种发布方式、"
    "白膜能不能被厂商取到。★ 想用白膜约束运镜之前先查这个，"
    "比等生成失败再排查省事得多。",
    {},
)
async def t_check_previz_channel(args: Dict[str, Any]) -> ToolResult:
    try:
        from core import publish as _pub
        db, _, _ = _services()
        settings = _settings(db)
        st = _pub.tunnel_status()
        tool = _pub.tunnel_available()
        base = _pub.public_base_url(settings)
        named_ready = _pub.named_tunnel_ready(settings)
        named_host = _pub.named_tunnel_hostname(settings)

        # ★ 2026-09-25 重排优先级：**命名隧道最优先**（永久固定域名），
        #   其次才是"用户自建公网主机"，最后是临时隧道。
        #   同时把"域名会不会变"这件事直接摆在 how 里 ——
        #   这个区别对用户很实际：临时域名的地址发给厂商后，重启就失效。
        if named_ready:
            if st.get("running") and st.get("permanent"):
                mode, usable = "named_tunnel", True
                how = (f"命名隧道 **{named_host}**（域名**永久固定**，重启也不变）"
                       f"—— 这是长期方案，已经就绪。")
            else:
                mode, usable = "named_tunnel", True
                how = (f"配了命名隧道 **{named_host}**（域名永久固定），"
                       f"隧道还没起 —— 第一次发布白膜时会自动起。")
        elif base:
            mode, usable = "base_url", True
            how = (f"用你配的 public_base_url（{base}）—— 域名固定，长期可用。"
                   f"（注意：文件必须真的在那个地址下，见 public_sync_dir）")
        elif st.get("running"):
            mode, usable = "tunnel", True
            how = (f"临时隧道（{st.get('base_url')}）—— 能用，但**域名每次重启都变**；"
                   f"地址发给厂商后下次就失效。要长期固定："
                   f"跑 `scripts\\装永久白膜隧道.ps1` 拿一个固定域名。")
        elif tool:
            mode, usable = "tunnel_available", True
            how = "装了隧道工具但还没起 —— 生成白膜参考时会自动起（临时域名）"
        else:
            mode, usable = "none", False
            how = ("没装隧道工具、也没配公网地址。白膜只能本地给你看，喂不进模型。"
                   "两个修法：① 跑 `scripts\\装永久白膜隧道.ps1` 拿固定域名（推荐）；"
                   "② 走可灵 MCP 的 `kling_white_model` —— "
                   "那条路用 file_upload 传白膜，**完全不需要公网 URL**。")

        return ok(
            usable=usable,
            mode=mode,
            permanent=bool(named_ready or base),
            permanent_url=(named_host or base or ""),
            how=how,
            tunnel=tunnel_status_view(st),
            tunnel_tool=tool or "未安装",
            public_base_url=base or "",
            named_tunnel={"configured": named_ready, "hostname": named_host,
                          "name": _pub.named_tunnel_name(settings)},
            why_needed=("白膜作为参考素材要求参考视频是一个**公网可访问的 URL**"
                        "（实测：base64 和本地路径都被平台拒绝）。"
                        "图片可以走 base64，视频不行。"),
            no_url_alternative=("可灵官方 MCP 的 kling_white_model 用 file_upload "
                                "传白膜，那条路**不需要任何公网 URL**。"),
        )
    except Exception as e:
        logger.exception("check_previz_channel failed")
        return err(f"检查白膜通道失败：{type(e).__name__}: {e}")


def tunnel_status_view(st: Dict[str, Any]) -> Dict[str, Any]:
    """把隧道状态收拾成给模型看的样子（别把内部字段一股脑丢出去）。"""
    return {
        "running": bool(st.get("running")),
        "base_url": st.get("base_url") or "",
        "uptime_seconds": st.get("uptime_seconds") or 0,
        "already_published": st.get("published_count") or 0,
        "restarts": st.get("restarts") or 0,
    }


@REGISTRY.register(
    "stop_previz_channel",
    "关掉白膜的公网隧道，收回公网暴露。"
    "★ 只在出于安全考虑时才需要关 —— 隧道按目录复用，"
    "留着能省掉下次 5-40 秒的握手，所以默认**不必**每次生成完都关。",
    {},
    writes=True,
)
async def t_stop_previz_channel(args: Dict[str, Any]) -> ToolResult:
    try:
        from core import publish as _pub
        before = _pub.tunnel_status()
        _pub.stop_tunnel()
        return ok(
            was_running=bool(before.get("running")),
            closed_url=before.get("base_url") or "",
            note="隧道已关闭。下次要用白膜参考时会自动重起一条（域名会变）。",
        )
    except Exception as e:
        return err(f"关隧道失败：{type(e).__name__}: {e}")


# ──────────────────────── 9. 可灵官方 MCP（免 API key）────────────────────────


@REGISTRY.register(
    "check_kling_mcp",
    "查可灵官方 MCP 通道的状态。**这条路不需要 API key** —— 可灵官方提供了一台"
    "远程 MCP 服务器（https://klingai.com/mcp），用 OAuth 授权，"
    "授权一次之后自动续期。用户问「可灵能不能不用 key」时就查这个。",
    {},
)
async def t_check_kling_mcp(args: Dict[str, Any]) -> ToolResult:
    try:
        from core import kling_mcp as K
        st = K.status()
        if st["token_valid"]:
            how = "已授权且 token 有效 —— 可以直接调可灵，不用 API key"
        elif st["authorized"] and st["has_refresh"]:
            how = "已授权但 token 过期了 —— 调用时会自动续期（不用重新授权）"
        elif st["registered"]:
            how = ("已注册客户端但**还没授权** —— 跑 kling_mcp_auth 拿到授权链接，"
                   "用户在浏览器里点一下「同意」就行，**不需要 API key**")
        else:
            how = ("还没开始 —— 跑 kling_mcp_auth，会注册客户端并给出授权链接。"
                   "全程不需要 API key。")
        # ★ 这里必须用 merged()：`K.status()` 返回的字典里**自带一个 "how" 键**，
        #   原来是 `ok(usable=..., how=how, **st)` —— Python 在调用 ok() 之前就抛
        #   `TypeError: ok() got multiple values for keyword argument 'how'`，
        #   被下面的 except 吞成「查可灵 MCP 失败：TypeError…」。
        #   后果：这个工具**永远失败**，用户永远看不到真实的授权状态。
        return ok(**merged(st, usable=bool(st["token_valid"]), how=how))
    except Exception as e:
        logger.exception("check_kling_mcp failed")
        return err(f"查可灵 MCP 失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "kling_mcp_auth",
    "开始可灵 MCP 的 OAuth 授权（**免 API key**）。"
    "会返回一个授权链接，**把它给用户让他在浏览器里打开并点「同意」**；"
    "点完 token 会自动存下来，以后不用再授权。"
    "★ 如果返回 already_ok=true，说明之前授权过，不用再来一遍。",
    {"force": {"type": "boolean", "desc": "true = 重新授权（换账号或 token 坏了）"}},
)
async def t_kling_mcp_auth(args: Dict[str, Any]) -> ToolResult:
    try:
        from core import kling_mcp as K
        if not args.get("force"):
            st = K.status()
            if st["token_valid"] or (st["authorized"] and st["has_refresh"]):
                return ok(**merged(st, already_ok=True,
                                   how="之前授权过，会自动续期，不用重新授权"))

        # ★ 这里**不能**一直等用户点同意（agent 一轮只有有限步数，
        #   而且用户可能现在不在电脑前）。所以只做"注册 + 出链接"，
        #   把等待放到独立的授权流程里。
        redirect_uri = K.REDIRECT_URI
        st = K.load_state()
        cid = st.get("client_id") or ""
        if not cid or st.get("redirect_uri") != redirect_uri:
            reg = await K.register_client(redirect_uri)
            cid = reg["client_id"]
            st.update({"client_id": cid, "redirect_uri": redirect_uri})
        pkce = K.make_pkce()
        st["pkce"] = pkce
        K.save_state(st)
        url = K.build_authorize_url(cid, pkce, redirect_uri)

        return ok(
            need_user_action=True,
            url=url,
            client_id=cid,
            steps=[
                "1. 把下面的链接给用户，让他在浏览器里打开",
                "2. 点「同意 / Authorize」",
                "3. 授权完成后回来告诉我，我用 kling_mcp_finish_auth 收尾",
            ],
            note=("这条路**不需要 API key** —— 可灵官方 MCP 用 OAuth。"
                  "授权一次之后会有 refresh_token，长期使用自动续期。"),
        )
    except Exception as e:
        logger.exception("kling_mcp_auth failed")
        return err(f"发起可灵授权失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "kling_mcp_finish_auth",
    "用户在浏览器里点完「同意」之后调这个收尾：本机起一个临时服务接住 OAuth 回调、"
    "换 token 并存盘。**必须在用户点完同意之后调**（会等最多 3 分钟）。",
    {"timeout": {"type": "number", "desc": "等回调的秒数，默认 180"}},
    writes=True,
)
async def t_kling_mcp_finish_auth(args: Dict[str, Any]) -> ToolResult:
    try:
        from core import kling_mcp as K
        to = float(args.get("timeout") or 180)
        res = await K.register_and_authorize(timeout=to, open_browser=False)
        if not res.get("ok"):
            return err(res.get("error") or "授权没完成",
                       url=res.get("url") or "",
                       hint="如果用户还没点同意，先让他点；链接过期就重新跑 kling_mcp_auth")
        return ok(**merged(res.get("status"),
                           next_step="授权好了，现在可以用可灵了 —— 而且是**不用 API key** 的官方通道。"))
    except Exception as e:
        logger.exception("kling_mcp_finish_auth failed")
        return err(f"收尾失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "list_kling_mcp_tools",
    "列出可灵官方 MCP 上到底有哪些工具（授权之后才查得到）。"
    "★ 先调这个看清有什么能力，再决定怎么用它生成视频 —— 不要凭猜。",
    {},
)
async def t_list_kling_mcp_tools(args: Dict[str, Any]) -> ToolResult:
    try:
        from core import kling_mcp as K
        ens = await K.ensure_token()
        if not ens.get("ok"):
            return err(ens.get("error") or "没有可用的 token",
                       hint="先跑 kling_mcp_auth 拿授权链接")
        tools = await K.list_tools()
        return ok(
            count=len(tools),
            tools=[{"name": t.get("name"),
                    "description": (t.get("description") or "")[:160],
                    "params": sorted(((t.get("inputSchema") or {}).get("properties") or {}).keys())}
                   for t in tools],
            refreshed=ens.get("refreshed"),
        )
    except Exception as e:
        logger.exception("list_kling_mcp_tools failed")
        return err(f"列可灵 MCP 工具失败：{type(e).__name__}: {e}")


# ──────────────── 10. 可灵 MCP 的**实际生成能力**（免 key）────────────────
# `kling_mcp_auth` 那一组只管"授权"；真正能出片的是下面这一批。
# 放在单独文件里：它依赖 who_am_i 的官方 schema，内容较长，
# 混在一起会让上面这些"闸门类"工具看不清。
from . import agent_tools_kling as _kling_tools  # noqa: E402,F401
