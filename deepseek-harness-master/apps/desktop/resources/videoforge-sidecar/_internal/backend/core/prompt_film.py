# -*- coding: utf-8 -*-
"""VideoForge · 视频提示词的「电影摄影层」（镜头契约块之后的一切专业化内容）。

═══════════════════════════════════════════════════════════════════
为什么要把这部分单独拆出来（2026-10-25）
═══════════════════════════════════════════════════════════════════
`videoprompt.build_video_prompt` 的职责是"把分镜翻译成一段连续镜头的描述"。
2026-10-25 的实测发现它有三处**会直接毁掉成片**的行为：

  ① 运镜被写反：分镜里同一个字段写了两个运镜词（「固定机位，横移」），
     旧逻辑按分隔符切从句后**只保留第一个** → 提示词写死「固定机位」，
     而剧本与白膜都是 orbit（半径 2.83m / 85mm）。最严重的一处。
  ② 表演模板污染静物：镜头没有角色，但时间线里带着 LLM 生成的 `expression`
     （富化后还有 `performance`），旧逻辑无条件拼 `acting.performance_prompt_lines`
     → 静物镜头出现「眼、唇、肩、呼吸全都收着」「声音：压低声音＝稳定、克制」。
  ③ 负向词不分层：`acting.performance_negatives` 是**人物镜头**的模板
     （面部不崩、嘴不能张大、不烧字幕），静物镜头照单全收 → 纯噪声。

所以这里定义**唯一权威 = `core.shot_contract` 的契约对象**，并把三件事拆干净：
  · 运镜段落 **只**来自 `shot_contract.camera_prompt_block(contract)`；
  · 表演/声音说明 **只在真有演员出镜时**才出现；
  · 负向词分成 通用层 / 静物层 / 人物层，按镜头是否有演员合并。

★ 这里**不发明任何数字**：提示词里出现的焦距、半径、弧角、机位高度、占画比，
  全部来自契约（契约再从白膜 spec 或渲染后的 camera-samples 取数）。
  本模块新增的电影摄影层只用定性但具体的名词（长焦压缩、软光、光比、颗粒…）。

★ 纯函数、无 IO、无网络、无随机、不读时间 —— 同一输入必须逐字节相同。
  （唯一允许的 IO 是 `resolve_contract` 里复用 shot_contract 提供的
   `load_shot_spec` / `load_camera_samples`，那是契约层的既有能力。）
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("videoforge.prompt_film")

#: 本模块的行为版本（写进 meta，便于回查"这条提示词是哪套拼装规则产的"）
FILM_LAYER_VERSION = "2026-10-25.1"

_END_RE = re.compile(r"[。．.；;，,、\s]+$")


def _t(v: Any) -> str:
    """把任意值压成干净字符串（与 videoprompt._t 同语义，避免循环 import）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return " ".join(_t(x) for x in v.values() if x)
    if isinstance(v, (list, tuple)):
        return " ".join(_t(x) for x in v)
    return str(v).strip()


def _aspect(seg: str) -> str:
    """去掉段尾的句读，段与段之间由拼装层统一用「。」连接。"""
    return _END_RE.sub("", (seg or "").strip())


# ══════════════════════════════════════════════════════════════════
# 一、运镜契约 → 提示词块
# ══════════════════════════════════════════════════════════════════

def shot_script_text(shot: Optional[Dict[str, Any]]) -> str:
    """契约层的意图文本：与 `shot_contract.build_contract` 的默认取法一致。"""
    s = shot or {}
    return " ".join(str(s.get(k) or "") for k in
                    ("layer1_overview", "layer2_timeline", "layer3_constraints"))


#: 白膜 spec 用的是 previz 自己的 movement 枚举（见 `previz_design.py` 的 prompt 契约：
#: `static|push_in|push_out|pan|orbit|track|descend|handheld`），
#: 而镜头契约只认 `MOVEMENTS=(orbit,dolly_in,dolly_out,crane,pan_rotate,track,static)`。
#: 真实旧 spec 上实测：`movement=push_in` 的镜头，契约落到 `static` → 提示词写「固定机位」，
#: 而白膜其实在推近（与缺陷 ① 同一类错误，只发生在契约的枚举映射上）。
#: 这里做**输入归一**（只换词表，不改任何数值）：把 previz 枚举映射到契约枚举再交给契约。
#: ★ 这是兼容层，不是新权威；根治应该在 `shot_contract` 里集中映射（已报给 lead）。
_PREVIZ_MOVEMENT_MAP = {
    "push_in": "dolly_in",
    "push_out": "dolly_out",
    "pan": "pan_rotate",
    "descend": "crane",
    "ascend": "crane",
    "orbit": "orbit",
    "track": "track",
    "static": "static",
    "dolly_in": "dolly_in",
    "dolly_out": "dolly_out",
    "crane": "crane",
    "pan_rotate": "pan_rotate",
}


def normalize_spec_movement(spec: Optional[Dict[str, Any]]
                            ) -> Tuple[Dict[str, Any], str, str]:
    """把白膜 spec 的 movement 归一到契约词表；返回 `(新 spec, 原值, 归一值)`。"""
    s = dict(spec or {})
    raw = str(s.get("movement") or "")
    mapped = _PREVIZ_MOVEMENT_MAP.get(raw, raw)
    if mapped and mapped != raw:
        s["movement"] = mapped
    return s, raw, mapped


def resolve_contract(shot: Dict[str, Any], *, shot_spec: Optional[Dict[str, Any]] = None,
                     script_text: str = "",
                     camera_samples: Optional[Sequence[Dict[str, Any]]] = None,
                     subject_height_ratio_measured: Optional[float] = None
                     ) -> Tuple[Optional[Dict[str, Any]], str]:
    """一步拿到某镜的权威契约；返回 `(contract, error)`。

    取数顺序（与契约层一致，这里只是把入口收敛成一个）：
      1. 调用方显式给的 `shot_spec` / `camera_samples` 优先；
      2. 否则复用 `shot_contract.contract_for_shot` 的白膜目录发现能力
         （沿 `shot["previz_path"]` 找 `spec.json` 与 `camera-samples.json`）；
      3. 契约层不可用时返回 `(None, error)`，由调用方降级并如实写进 audit ——
         绝不静默假装契约成功。

    ★ 返回的契约副本额外带三个**下划线私有标注**（只给提示词拼装看，不改变契约语义）：
      `_geometry_source`：`samples` / `path` / `camera` / `none`
        —— 焦距/机位高度这些数字到底是不是真有机位数据支撑；
      `_path_present`：有没有真机位路径（决定能不能写半径/弧角的具体数字）；
      `_spec_movement_raw` / `_spec_movement_mapped`：白膜原始枚举与归一后的枚举。
      没有白膜数据的镜头（比如还没渲染过 previz）拿到的 `lens_mm` 只是契约默认值，
      **不是这个镜头的光学参数** —— 提示词必须据此退化成定性描述，不能把
      「35mm」「半径恒定 0 米」这种假数字写给出片模型。
    """
    from core import shot_contract as SC          # 局部 import：避免顶层耦合

    text = script_text or shot_script_text(shot)
    try:
        # 白膜 spec 的 movement 枚举（push_in/pan…）由契约层负责归一（2026-10-25 已内置）。
        # 只有在契约层**还是旧版**（没有别名表）时才由本层临时归一，保证不把
        # 「白膜在推、提示词写固定机位」重新引回来。
        spec_seen = shot_spec
        if spec_seen is None:
            spec_seen = SC.load_shot_spec(shot) if hasattr(SC, "load_shot_spec") else {}
        samples_seen = camera_samples
        if samples_seen is None and hasattr(SC, "load_camera_samples"):
            samples_seen = SC.load_camera_samples(shot)
        raw_mov = str((spec_seen or {}).get("movement") or "")
        alias_handled = hasattr(SC, "_MOVEMENT_ALIASES") or "handheld" in getattr(SC, "MOVEMENTS", ())
        mapped_mov = raw_mov
        spec_norm = spec_seen
        if hasattr(SC, "_MOVEMENT_ALIASES"):
            # 契约层已内置别名表：只读它来做**上报**（不参与决策）
            mapped_mov = str(SC._MOVEMENT_ALIASES.get(raw_mov, raw_mov) or raw_mov)  # noqa: SLF001
        elif not alias_handled:
            # 旧版契约层没有别名表 → 本层临时归一，避免白膜在推、提示词写固定机位
            spec_norm, raw_mov, mapped_mov = normalize_spec_movement(spec_seen)

        if (camera_samples is None and subject_height_ratio_measured is None
                and hasattr(SC, "contract_for_shot")):
            # ★ lead 指定的一步入口：自动读 spec + 渲染采样（含新鲜度保护），
            #   并从白膜视频量主体占画高。显式 shot_spec 也走它（它支持 spec=）。
            contract = SC.contract_for_shot(shot, spec=shot_spec, script_text=text)
        else:
            contract = SC.build_contract(
                shot, spec_norm, text,
                camera_samples=list(samples_seen) if samples_seen else None,
                subject_height_ratio_measured=subject_height_ratio_measured)

        annotated = dict(contract or {})
        cam = ((spec_seen or {}).get("camera") or {}) if isinstance(spec_seen, dict) else {}
        has_path = bool(cam.get("moves"))
        if annotated.get("camera_samples_stale"):
            src = "path" if has_path else ("camera" if cam.get("position") else "none")
        elif samples_seen:
            src = "samples"
        elif has_path:
            src = "path"
        elif cam.get("position"):
            src = "camera"
        else:
            src = "none"
        annotated["_geometry_source"] = src
        annotated["_path_present"] = bool(has_path or (samples_seen and src == "samples"))
        annotated["_spec_movement_raw"] = raw_mov
        annotated["_spec_movement_mapped"] = mapped_mov
        return annotated, ""
    except TypeError as e:
        # 旧版契约签名（没有 camera_samples / subject_height_ratio_measured）
        try:
            return SC.build_contract(shot, shot_spec or {}, text), f"contract_signature_old:{e}"
        except Exception as e2:                    # noqa: BLE001
            logger.warning("镜头契约不可用（%s: %s）", type(e2).__name__, e2)
            return None, f"{type(e2).__name__}: {e2}"
    except Exception as e:                         # noqa: BLE001
        logger.warning("镜头契约不可用（%s: %s）", type(e).__name__, e)
        return None, f"{type(e).__name__}: {e}"


def _degrade_unmeasured_numbers(block: str, contract: Dict[str, Any]) -> str:
    """把契约块里**没有真实机位数据支撑**的数字拿掉（宁缺勿造）。

    典型场景：这个镜头还没渲染过白膜，契约只能拿到默认值 → 块里会出现
    「35mm 定焦」「机位高度 1.4 米」「做0°弧形环绕横移，半径恒定 0 米」。
    这些数字写进提示词等于告诉出片模型一个假的光学参数，比不写更糟。
    """
    out = block or ""
    src = str(contract.get("_geometry_source") or "")
    if src in ("", "none"):
        out = re.sub(r"镜头规格：[\d.]+mm 定焦", "镜头规格：定焦镜头", out)
        out = re.sub(r"[，,]?机位高度 [\d.]+ 米", "，机位高度稳定", out)
    if not contract.get("_path_present"):
        out = re.sub(r"做[\d.]+°", "做", out)
        out = re.sub(r"[，,]?半径恒定 [\d.]+ 米", "，半径恒定", out)
    # 契约层没给 handheld 的中文名（movement_zh 会原样返回英文），这里补本地化
    out = re.sub(r"\bhandheld\b", "手持跟拍", out, flags=re.I)
    return _aspect(out)


def camera_block_for_prompt(contract: Optional[Dict[str, Any]]) -> str:
    """契约 → 运镜段落。**这是提示词里运镜描述的唯一来源。**

    唯一的两处加工都是"去掉不可信内容"，不新增任何描述：
      ① `subject_height_ratio` 来源是 `analytic`（按 spec 几何 + **声明**焦距折算）
         而焦距又被渲染器改写过时，这个百分比已经不可信 → 把数字从契约块里拿掉
         （传副本、只清零该字段），改用本模块的定性描述；
      ② 契约没有真实机位数据时（`_geometry_source == "none"` / 无路径），
         把块里的焦距/机位高度/半径/弧角数字退化成定性词（见 `_degrade_unmeasured_numbers`）。
    """
    if not isinstance(contract, dict) or not contract:
        # 契约层不可用时的安全降级：不写任何焦距/半径数字（宁缺勿造）
        return ("镜头规格：焦段全程固定；运镜保持单一连续，不切、不叠加第二个运镜；"
                "机位稳定，构图跟随主体")
    from core.shot_contract import camera_prompt_block as _block

    c = contract
    block_contract: Dict[str, Any] = c
    if (c.get("subject_ratio_source") != "measured_frames"
            and c.get("lens_rewritten") and c.get("subject_height_ratio")):
        block_contract = dict(c)
        block_contract["subject_height_ratio"] = 0.0
    return _degrade_unmeasured_numbers(_block(block_contract), c)


# ══════════════════════════════════════════════════════════════════
# 二、演员出镜判定（决定表演块 / 声音块 / 负向层）
# ══════════════════════════════════════════════════════════════════

#: 画面里出现"人"的文本信号。只认明确的指人或职业/称谓词，
#: 不认裸「人」字（"仙人掌""人物简历"这类误伤太多）。
_HUMAN_RE = re.compile(
    r"(?<!其)(?<!吉)他|(?<!其)她|人物|角色|主角|男主|女主|演员|"
    r"用户|男人|女人|男子|女子|男士|女士|男孩|女孩|少年|少女|老人|孩子|"
    r"人群|众人|人们|身影|"
    r"队长|医生|护士|警察|士兵|军人|司机|老师|学生|老板|店员|顾客|路人|观众")
#: 「说话的人」但不是画面里的演员（旁白/画外音不该触发表演层）
_NARRATOR_RE = re.compile(r"^(旁白|解说|画外音|内心独白|narrator|voice\s?over|vo|v\.o\.)$", re.I)


def _dialogue_speaker(it: Dict[str, Any]) -> str:
    d = it.get("dialogue")
    if not isinstance(d, dict):
        return ""
    if not _t(d.get("text")):
        return ""
    return _t(d.get("character") or d.get("speaker") or d.get("name"))


def visible_cast(shot: Optional[Dict[str, Any]], *, timeline: Optional[Sequence[Any]] = None,
                 on_screen_names: Optional[Sequence[str]] = None,
                 action: str = "") -> bool:
    """这一镜**画面上**有没有演员。决定表演块与负向人物层是否出现。

    判定顺序（任一成立即算有演员）：
      1. 角色卡里被点名/被兜底选中的角色（`on_screen_names`，与「人物」段同源）；
      2. 分镜绑定了 `character_ids`；
      3. 动作/概述文本里出现明确的指人词（「他抬头」「李队长」）。
    注意：`expression` / `performance` **不算证据** —— 静物镜头上也可能被
    LLM 富化出表演字段，这正是缺陷 ② 的来源。
    """
    if on_screen_names:
        return True
    s = shot or {}
    if s.get("character_ids"):
        return True
    texts = [action, _t(s.get("layer1_overview"))]
    for it in (timeline or []):
        if isinstance(it, dict):
            texts.append(_t(it.get("action") or it.get("description")))
    if any(_HUMAN_RE.search(t or "") for t in texts):
        return True
    for it in (timeline or []):
        if isinstance(it, dict):
            who = _dialogue_speaker(it)
            if who and not _NARRATOR_RE.match(who):
                return True
    return False


# ══════════════════════════════════════════════════════════════════
# 三、负向词分层
# ══════════════════════════════════════════════════════════════════

#: ① 通用层：与"有没有人"无关的画质/技术约束
GENERAL_NEGATIVES: Tuple[str, ...] = (
    "文字", "字幕", "水印", "logo 角标", "角标",
    "画面畸变", "镜头畸变", "透视错误", "比例失真",
    "切镜", "转场", "闪白", "画面撕裂", "画幅跳变", "黑边", "拉伸变形",
    "画面抖动", "手持晃动", "果冻效应", "频闪",
    "画面卡顿", "低分辨率", "压缩噪点", "马赛克块", "摩尔纹", "过度锐化",
)

#: ② 静物层：没有演员时**替换**人物层，防"多加人/多加道具/主体飘"
STILL_LIFE_NEGATIVES: Tuple[str, ...] = (
    "不得出现人物", "不得出现多余道具", "主体不得位移或变形",
    "不得悬浮或穿模", "台面接触阴影不得缺失", "倒影与投影不得错位",
)

#: ③ 人物层：只在有演员出镜时并入
CAST_NEGATIVES: Tuple[str, ...] = (
    "人物变形", "面部崩坏", "多余手指", "面部比例错误",
    "身份漂移", "口型与台词不同步", "人物瞬移", "位置突变", "肢体扭曲",
)


def negative_layers(*, has_cast: bool, timeline: Optional[Sequence[Any]] = None,
                    constraints: Any = None, max_chars: int = 1500
                    ) -> Tuple[str, List[str]]:
    """按层拼负向提示词；返回 `(negative_prompt, 用到哪几层)`。

    层序固定为：分镜约束 → 通用层 → (静物层 | 人物层)。
    `max_chars` 只做**兜底截断**（正常长度 300 字上下），截断时按逗号边界走，
    不把词切成半个。
    """
    parts: List[str] = []
    layers: List[str] = []

    cons = constraints
    if isinstance(cons, str):
        import json
        try:
            cons = json.loads(cons)
        except Exception:                          # noqa: BLE001
            cons = {}
    if isinstance(cons, dict):
        got: List[str] = []
        for k in ("must_not_appear", "must_not_happen"):
            v = cons.get(k)
            if isinstance(v, (list, tuple)):
                got.extend(_t(x) for x in v if _t(x))
            elif _t(v):
                got.append(_t(v))
        if got:
            parts.extend(got)
            layers.append("constraints")

    parts.extend(GENERAL_NEGATIVES)
    layers.append("general")

    if has_cast:
        parts.extend(CAST_NEGATIVES)
        try:
            from core.acting import performance_negatives as _pn
            parts.extend(_pn(timeline or []))
        except Exception as e:                     # noqa: BLE001
            logger.warning("取微表情负面限制失败（用人物层默认词继续）：%s", e)
        layers.append("cast")
    else:
        parts.extend(STILL_LIFE_NEGATIVES)
        layers.append("still_life")

    text = "，".join(dict.fromkeys(p.strip() for p in parts if p and p.strip()))
    cap = int(max_chars) if max_chars else 1500
    if len(text) > cap:
        cut = text[:max(cap - 1, 0)]
        if "，" in cut:
            cut = cut[:cut.rfind("，")]
        text = cut.rstrip("，") + "…"
    return text, layers


# ══════════════════════════════════════════════════════════════════
# 四、审计补充：契约冲突如实上报
# ══════════════════════════════════════════════════════════════════

_MOVEMENT_ZH = {"orbit": "弧形环绕横移", "track": "平行横移", "static": "固定机位",
                "dolly_in": "推近", "dolly_out": "拉远", "crane": "升降",
                "pan_rotate": "摇镜", "handheld": "手持跟拍", "zoom": "变焦"}


def movement_zh(contract: Optional[Dict[str, Any]]) -> str:
    """契约 movement → 中文名（契约层没给 handheld 的中文，这里补上）。"""
    c = contract or {}
    mov = str(c.get("movement") or "")
    return _MOVEMENT_ZH.get(mov) or _t(c.get("movement_zh"))


def intent_movement(script_text: str = "") -> str:
    """从剧本文本读运镜意图（唯一实现仍在 shot_contract.parse_intent）。"""
    try:
        from core.shot_contract import parse_intent
        return str(parse_intent(script_text or "")["movement"] or "")
    except Exception:                              # noqa: BLE001
        return ""


def has_hard_conflict(contract: Optional[Dict[str, Any]], script_text: str = "") -> bool:
    """只有「剧本意图 vs 白膜 movement」不符才是硬冲突（lead 口径）。

    焦距被渲染器改写（声明 85 / 实际 46.75）、弧角在容差内、焦距恒定，
    都属于正常适配或 info 级记录，不在此列。
    """
    c = contract or {}
    if not c.get("intent_hits"):
        return False                               # 剧本没提运镜 → 以白膜为准，不算冲突
    wanted = intent_movement(script_text or "")
    return bool(wanted and wanted != str(c.get("movement") or "static"))


def contract_issues(contract: Optional[Dict[str, Any]], script_text: str = "",
                    *, raw_incompatible: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
    """把契约为提示词带来的**必须让上层看到**的问题整理成 audit.issues。

    严重级别按 lead 的口径：
      · 只有「剧本意图 vs 白膜 movement」不符才是硬冲突 → `warning`（`P-CAMERA-CONTRACT`）；
      · 焦距被渲染器改写（声明 85 / 实际 46.75）是**正常适配** → `info`（`P-LENS-REWRITTEN`）；
      · 其它契约记录（弧角偏差、焦距不恒定等）→ `info`（`P-CAMERA-CONTRACT`）；
      · 分镜的 `camera` 字段与契约不一致 → `info`（`P-CAMERA-CONTRACT`）。
    `info` 不进打分、不拦生成（与 promptaudit 同一口径），但会留在 `audit.issues` 里。
    """
    out: List[Dict[str, str]] = []
    c = contract or {}
    if not c:
        return out
    mov = str(c.get("movement") or "static")
    hard = has_hard_conflict(c, script_text)
    if hard:
        wanted = intent_movement(script_text or "")
        out.append({
            "code": "P-CAMERA-CONTRACT",
            "severity": "warning",
            "message": (f"剧本意图（{_MOVEMENT_ZH.get(wanted, wanted)}）与白膜机位"
                        f"（{_MOVEMENT_ZH.get(mov, mov)}）不一致。提示词已按白膜的几何数据写"
                        f"（{c.get('lens_mm')}mm、半径 {c.get('radius_m')} 米），"
                        f"但剧本想要的运镜没有落在白膜里。"),
            "fix": "先确认白膜和剧本哪个是对的：改剧本文字，或重生成白膜机位；不要只改提示词。",
        })
    for msg in (c.get("conflicts") or []):
        msg = str(msg)
        if "实际焦距" in msg:
            continue                                # 由下面的 P-LENS-REWRITTEN 统一表达
        if hard and "剧本意图=" in msg:
            continue                                # 已由上面的硬冲突条表达
        out.append({
            "code": "P-CAMERA-CONTRACT",
            "severity": "info",
            "message": f"镜头契约记录：{msg}（提示词按契约数值写）。",
            "fix": "如属数据过期，重生成白膜 spec 后提示词会自动跟随。",
        })
    if c.get("lens_rewritten"):
        out.append({
            "code": "P-LENS-REWRITTEN",
            "severity": "info",
            "message": (f"参考片实际焦距 {c.get('lens_mm')}mm 与剧本/白膜声明 "
                        f"{c.get('lens_declared_mm')}mm 不同（渲染为「主体不切边」改写焦距）。"
                        f"提示词以**实际焦距**为准，否则描述的光学效果与参考片不是同一个镜头。"),
            "fix": "无需修：这是取景适配。若要严格 85mm 效果，需要调白膜取景参数后重渲。",
        })
    raw_mv = str(c.get("_spec_movement_raw") or "")
    mapped_mv = str(c.get("_spec_movement_mapped") or "")
    known = ("orbit", "track", "static", "dolly_in", "dolly_out", "crane", "pan_rotate")
    if raw_mv and mapped_mv not in known:
        out.append({
            "code": "P-CAMERA-CONTRACT",
            "severity": "info",
            "message": (f"白膜 movement={raw_mv} 不在镜头契约的枚举里，"
                        f"提示词按剧本意图（{_MOVEMENT_ZH.get(mov, mov)}）写；"
                        f"两者可能不是同一个运动。"),
            "fix": "把白膜 movement 归一到契约枚举（push_in→dolly_in / pan→pan_rotate …），"
                   "或在剧本里写清运镜。",
        })
    raws = [str(n) for n in (raw_incompatible or []) if str(n).strip()]
    if raws:
        out.append({
            "code": "P-CAMERA-CONTRACT",
            "severity": "info",
            "message": (f"分镜的机位字段写着「{'、'.join(dict.fromkeys(raws))}」，"
                        f"与镜头契约（{_MOVEMENT_ZH.get(mov, mov)}）不符；"
                        f"提示词已以契约为准，不再沿用旧逻辑「只取第一个运镜词」。"),
            "fix": "把分镜的 camera 字段改成与剧本/白膜一致的单一运镜，避免下游再读错。",
        })
    return out


def inject_issues(audit: Optional[Dict[str, Any]], extra: Sequence[Dict[str, str]],
                  prompt: str, negative: str = "") -> Dict[str, Any]:
    """把补充问题并进 audit，并按 promptaudit 的同一套口径重算分数/闸门。"""
    base = dict(audit or {})
    issues = list(base.get("issues") or [])
    seen = {(i.get("code"), i.get("message")) for i in issues}
    for it in (extra or []):
        key = (it.get("code"), it.get("message"))
        if key not in seen:
            issues.append(dict(it))
            seen.add(key)
    errs = [i for i in issues if i.get("severity") == "error"]
    warns = [i for i in issues if i.get("severity") == "warning"]
    if not (prompt or "").strip():
        score = 0
    else:
        score = max(0, min(100, 100 - len(errs) * 15 - len(warns) * 5))
    base.update({
        "issues": issues,
        "errors": errs,
        "warnings": warns,
        "error_count": len(errs),
        "warning_count": len(warns),
        "score": score,
        "valid_for_generation": not errs,
        "valid_for_review": not errs,
    })
    # 摘要沿用 promptaudit 的实现（口径必须一致），失败就退回最简句
    try:
        from core import promptaudit as _pa
        base["summary"] = _pa._summary(errs, warns)     # noqa: SLF001
    except Exception:                                   # noqa: BLE001
        base["summary"] = (f"{len(errs)} 处硬伤 / {len(warns)} 处提醒"
                           if (errs or warns) else "结构完整")
    return base


# ══════════════════════════════════════════════════════════════════
# 五、审计前处理：否定式运镜词
# ══════════════════════════════════════════════════════════════════

#: promptaudit 的运镜词表**不感知否定**：「不变焦」会被当成一个 `zoom` 主运镜，
#: 「固定机位 + 不变焦」会被判成"同时写了固定机位和运镜"的 `P-CAMERA-CONFLICT`
#: 硬伤 —— 而提示词里这些词恰恰是**禁止**语。
#: 契约层已经保证"一个镜头只有一个主运动"，所以送审前把否定式运镜归一成中性词，
#: 让审计器的运镜检查只看到契约块里的那个**真**运镜。
_NEG_CAMERA_SUBS: Tuple[Tuple[str, str], ...] = (
    ("不推拉、不变焦", "焦距固定"),
    ("不推不拉、不摇不移", "机位锁定"),
    ("不切、不推不拉", "连续拍摄"),
    ("不推不拉", "机位锁定"),
    ("不摇不移", "机位锁定"),
    ("而不是变焦", "而不是改变透视"),
    ("而非变焦", "而非改变透视"),
    ("无变焦", "焦距固定"),
    ("不变焦", "焦距固定"),
    ("不推拉", "焦距固定"),
)


def neutralize_negated_camera(text: str) -> str:
    """送审计前把「不变焦 / 不推拉 / 不摇不移」等否定语归一，避免假硬伤。"""
    out = text or ""
    for old, new in _NEG_CAMERA_SUBS:
        out = out.replace(old, new)
    return out


# ══════════════════════════════════════════════════════════════════
# 六、电影摄影层（契约块之后）
# ══════════════════════════════════════════════════════════════════

#: 场景卡 lighting → 布光方向/软硬/色温（只写可观察的性质，不写色温数字）
_LIGHTING_FILM = {
    "studio": "主光从画面上方偏侧打入，辅光压低光比、明暗交界柔和；色温中性偏暖，白平衡全程稳定",
    "natural": "自然光从侧上方漫射，软光为主、硬阴影被环境反弹光柔化；色温随环境中性",
    "golden_hour": "低角度暖光侧逆打亮主体边缘，暗部用冷调补光托住；光比大但过渡柔和",
    "blue_hour": "冷调环境光作主光，主体受光面偏青蓝，暖色实用光作点缀；光比克制",
    "night": "实用光源（灯/窗）作主光，主体受光方向明确，暗部保留层次；整体偏冷调",
    "neon": "彩色霓虹光从两侧打亮，主体边缘带彩色轮廓光；高光收敛、暗部不糊",
    "candle": "低色温烛光作唯一主光，光比大、阴影边缘柔和，暗部保留细节",
    "overcast": "阴天漫射光，软阴影、低反差，色温偏冷、色彩饱和自然",
}
_TOD_TONE = {"day": "色温中性", "noon": "色温中性偏硬", "night": "色温偏冷",
             "dusk": "色温偏暖", "dawn": "色温偏冷偏青"}

#: 场景槽位中文名（与 videoprompt.SCENE_SLOT_ZH 同源语义；这里只用得着色调）
_SLOT_ZH = {"terrain": "地形地面", "architecture": "建筑构筑",
            "furnishings": "陈设物件", "atmosphere": "氛围", "color_tone": "色调"}


def _slots_of(obj: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    import json
    rf = (obj or {}).get("reference_features")
    if isinstance(rf, str):
        try:
            rf = json.loads(rf)
        except Exception:                          # noqa: BLE001
            rf = {}
    if not isinstance(rf, dict):
        return {}
    sl = rf.get("slots")
    return sl if isinstance(sl, dict) else {}


def _focal_usage(lens_mm: float) -> str:
    """焦段的**用途**（不是空话）：长焦压缩、标准自然、广角纵深。"""
    try:
        lens = float(lens_mm or 0.0)
    except (TypeError, ValueError):
        lens = 0.0
    if lens >= 70:
        return "中长焦压缩透视，主体与背景的纵深被压平，背景自然虚化、主体轮廓与材质边缘干净"
    if lens >= 40:
        return "标准焦段透视自然，主体与环境的比例可信，空间关系稳定"
    if lens > 0:
        return "广角透视，前景与纵深的距离感被拉开，空间关系要靠机位而不是焦段来维持"
    return "焦段全程固定，构图随主体与机位的关系自然变化"


def _lighting_layer(scene: Optional[Dict[str, Any]]) -> str:
    s = scene or {}
    key = _t(s.get("lighting")).lower()
    base = _LIGHTING_FILM.get(key)
    if base:
        return "布光：" + base
    tone = _TOD_TONE.get(_t(s.get("time_of_day")).lower(), "色温中性")
    return ("布光：单一主光从画面上方偏侧打入，光比自然、阴影边缘柔和；" + tone +
            "，主体受光面与背光面层次分明，高光不溢出、暗部不死黑")


def _texture_layer(scene: Optional[Dict[str, Any]], art: Optional[Dict[str, Any]]) -> str:
    slots = _slots_of(scene or {})
    bits = ["质感：表面材质呈实拍级微观细节（细微纹理、边缘倒角、接触阴影清晰），"
            "高光与反射有物理依据，不出现塑料感或过度光滑"]
    tone = _t(slots.get("color_tone")) or _t((art or {}).get("color_tone"))
    if tone:
        bits.append(f"调色倾向：{tone}")
    else:
        bits.append("调色倾向：中性低饱和的胶片调色，白平衡稳定，高光收敛、暗部保留色相")
    return "；".join(bits)


def _subject_layers(contract: Dict[str, Any], action: str, has_cast: bool) -> List[str]:
    """没有演员时，用「主体/道具数量 + 主体形态」替代表演层。"""
    if has_cast:
        return []
    if contract.get("subject_on_surface"):
        qty = "主体与道具：画面里只有主体与它的支撑台面，不增加其他道具或背景陈设（倒影与投影除外）"
    else:
        qty = "主体与道具：画面里只有主体本身，不增加其他道具、人物替身或背景陈设"
    out = [qty]
    still = bool(re.search(r"静止|不动|保持|停在|停稳|恒定", action or ""))
    if still:
        out.append("主体形态：轮廓、比例与摆放姿态全程不变，画面变化只来自机位移动与光影移动")
    else:
        out.append("主体形态：轮廓、比例与材质细节在运动过程中保持连续，不出现形变或比例漂移")
    return out


def film_layers(contract: Optional[Dict[str, Any]], *,
                shot: Optional[Dict[str, Any]] = None,
                scene: Optional[Dict[str, Any]] = None,
                art: Optional[Dict[str, Any]] = None,
                action: str = "", style: str = "cinematic",
                shot_size: str = "", has_cast: bool = False) -> List[str]:
    """契约块之后的电影摄影层（景别焦段/布光/质感调色/颗粒动态模糊/亮度层次）。

    ★ 全部是**定性但具体**的名词，不写任何契约里没有的数字；
      场景卡有的信息（布光、色调）优先用场景卡，没有才用通用但不冲突的表述。
    """
    c = contract or {}
    lens = c.get("lens_mm") or 0.0
    ratio_source = str(c.get("subject_ratio_source") or "")
    layers: List[str] = []

    # ① 景别与焦段用途
    size = _t(shot_size)
    seg = "景别与焦段：" + (f"{size}取景，" if size else "") + _focal_usage(lens) + \
          "；对焦点落在主体受光面，构图跟随机位移动，焦段全程固定"
    if ratio_source and ratio_source != "measured_frames" and c.get("lens_rewritten"):
        # 焦距被渲染器改写过，analytic 占画比不可信 → 只写定性
        # （不写「参考片」这类词：审计的 P-REFERENCE-SCOPE 会把无继承/排除声明的
        #  「参考」当成"提到了参考图却没说范围"，那是假阳性。）
        seg += "；主体在画面中的高度占比全片保持一致，以首帧为准，不因运镜而缩放"
    layers.append(seg)

    # ② 布光方向与软硬/色温
    layers.append(_lighting_layer(scene))

    # ③ 质感与调色倾向
    layers.append(_texture_layer(scene, art))

    # ④ 颗粒与动态模糊
    layers.append("颗粒与动态模糊：细颗粒胶片质感，颗粒均匀、不结块、不掩盖材质细节；"
                  "机位移动段按真实快门产生自然运动模糊，不使用果冻效应或频闪")

    # ⑤ 亮度层次
    layers.append("亮度层次：高光收边不过曝、暗部保留材质细节、中间调平滑过渡，"
                  "主体与背景分离清楚，画面有体积感而不发灰")

    # ⑥ 无演员镜头的替代层（主体/道具数量 + 形态）
    layers.extend(_subject_layers(c, action, has_cast))

    return [x for x in (_aspect(s) for s in layers) if x]


# ══════════════════════════════════════════════════════════════════
# 七、运镜来源核对（meta.camera_moves_seen / camera_conflict）
# ══════════════════════════════════════════════════════════════════

#: 运镜族：同一族内的两种说法不算互相矛盾（orbit 与 track 都属横向）
_MOVEMENT_KIND = {"orbit": "lateral", "track": "lateral", "static": "static",
                  "dolly_in": "axial", "dolly_out": "axial", "crane": "vertical",
                  "pan_rotate": "swivel", "handheld": "handheld", "zoom": "zoom"}

#: 分镜 `camera` 字段的旧规范名 → 契约 movement code（顺序敏感：长的/具体的在前）
_CANON_WORDS: Tuple[Tuple[str, str], ...] = (
    ("环绕运镜", "orbit"), ("斯坦尼康稳定跟拍", "track"),
    ("向左横摇", "pan_rotate"), ("向右横摇", "pan_rotate"), ("横摇", "pan_rotate"),
    ("俯仰摇", "pan_rotate"), ("上摇", "pan_rotate"), ("下摇", "pan_rotate"),
    ("摇臂升降", "crane"), ("摇臂", "crane"), ("升降", "crane"),
    ("缓慢推近", "dolly_in"), ("推镜", "dolly_in"), ("推轨", "track"),
    ("缓慢拉远", "dolly_out"), ("拉镜", "dolly_out"), ("拉远", "dolly_out"),
    ("变焦推近", "zoom"), ("变焦拉远", "zoom"), ("变焦", "zoom"),
    ("固定机位", "static"), ("锁定", "static"), ("静止", "static"),
    ("跟拍", "track"), ("跟随", "track"), ("横移", "track"), ("平移", "track"),
    ("手持", "handheld"), ("航拍", "handheld"), ("俯拍", "handheld"), ("仰拍", "handheld"),
)


def movement_of(name: Any) -> str:
    """把旧规范名/中文运镜词映射回契约 movement code；认不出返回空。"""
    s = _t(name)
    if not s:
        return ""
    if s in _MOVEMENT_KIND:
        return s
    for word, mov in _CANON_WORDS:
        if word in s:
            return mov
    return ""


def camera_moves_seen(contract: Optional[Dict[str, Any]],
                      raw_names: Optional[Sequence[str]] = None) -> List[str]:
    """如实上报这一镜**出现过**的运镜：契约认定的那个排第一，其余是分镜里的旧写法。"""
    seen: List[str] = []
    zh = _t((contract or {}).get("movement_zh"))
    if zh:
        seen.append(zh)
    for name in (raw_names or []):
        name = _t(name)
        if name and name not in seen:
            seen.append(name)
    return seen


def incompatible_moves(contract: Optional[Dict[str, Any]],
                       raw_names: Optional[Sequence[str]] = None) -> List[str]:
    """分镜 `camera` 字段里与契约**互相矛盾**的运镜词（同族不算矛盾）。"""
    c = contract or {}
    target = movement_of(c.get("movement") or "static")
    target_kind = _MOVEMENT_KIND.get(target, target)
    out: List[str] = []
    for name in (raw_names or []):
        mov = movement_of(name)
        if not mov or mov == target:
            continue
        kind = _MOVEMENT_KIND.get(mov, mov)
        if kind == target_kind:
            continue                                # 同族：orbit ↔ track 不算冲突
        if mov in ("zoom", "handheld"):
            continue                                # 不是主运动，属于构图修正
        if _t(name) not in out:
            out.append(_t(name))
    return out
