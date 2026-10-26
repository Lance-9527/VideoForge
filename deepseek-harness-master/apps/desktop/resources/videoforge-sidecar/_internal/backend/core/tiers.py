"""
VideoForge · 三级生成链路的**唯一真相源**（G1 白膜 → G2 草稿样片 → G3 成片）

════════════════════════════════════════════════════════════════════════
为什么要有这个模块（2026-09-27 第 70 轮）
════════════════════════════════════════════════════════════════════════
用户的原话：

    「我要求白膜就是为了生成视频前先确认要生成的动向之类的，
      再用**低价的视频 token 覆盖**，没问题再由低价的视频 token 生成的
      内容基础**上升到高画质的视频**。所以你再优化下，做到我能用好，完成为止。」

链路本来就有三道闸（`previz` 的状态机 + `generate_preview` / `generate_final`
两个工具），但**界面上从来没把这三档说清楚过**：

  · 用户不知道"现在在哪一档、下一档要花多少钱、值不值得"；
  · 更糟的是——**当模型不吃 draft 时，界面一声不响**。
    实测原文（火山方舟）：
      `the specified parameter draft is not supported for model
       doubao-seedance-1-0-pro in t2v, must be empty`
    也就是说这台模型**根本没有"便宜那一档"**，只能直接出成片（更贵）。
    用户以为自己走的是"低价→高价"两段式，实际上每次都在直接出贵的那一档。
    ⇒ 必须**如实告诉他**，这是这个模块存在的第一理由。

════════════════════════════════════════════════════════════════════════
"支持 draft" 这件事不能只看静态声明
════════════════════════════════════════════════════════════════════════
`SeedanceAdapter.supports_draft = True` 是**厂商能力表的静态声明**，
而真实的拒绝是按**具体 model 名**发生的（`doubao-seedance-1-0-pro` 就不行）。
所以：

  · 适配器自愈时（被拒就删掉 draft 重发）会调 `note_draft_rejected()`，
    把"这个 model 实测不吃 draft"**记在磁盘上**；
  · 本模块读这份记录，于是界面说的是**实测事实**，不是厂商宣传。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("videoforge.tiers")

#: 档的名字与"花费口径"。花费是**定性**的 —— 本仓没有可核对的价目表，
#: 所以只敢说"免费 / 便宜 / 贵"，并明确标出这是定性口径。
TIERS = {
    "G1": {"key": "G1", "name": "白膜预演", "short": "白膜",
           "cost": "免费", "cost_kind": "qualitative",
           "cost_note": "本机 ffmpeg 渲染，不调用任何付费接口，可以无限次重来",
           "what": "运镜 / 走位 / 遮挡 / 节奏 —— 在花任何钱之前先确认"},
    "G2": {"key": "G2", "name": "草稿样片 480P", "short": "草稿",
           "cost": "便宜", "cost_kind": "qualitative",
           "cost_note": "视频模型的低价档（Draft）。官方数据：抽卡 4 次命中省 57%，20 次命中省 77%",
           "what": "画风 / 表演 / 一致性 —— 确认之后才值得升到成片"},
    "G3": {"key": "G3", "name": "成片 1080P", "short": "成片",
           "cost": "贵", "cost_kind": "qualitative",
           "cost_note": "整个流程里最贵的一步",
           "what": "交付"},
}


def _data_root() -> str:
    return os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()


def _obs_path() -> str:
    return os.path.join(_data_root(), "cache", "draft_support.json")


def _load_obs() -> Dict[str, Any]:
    try:
        with open(_obs_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def note_draft_rejected(model_name: str, evidence: str = "") -> None:
    """记录一次**实测**："这个 model 被平台拒了 draft"。

    由适配器自愈路径调用（seedance.py）。写文件而不是写 DB：
    它是"环境事实"（换了 Key / 换了模型就变），不是项目数据；
    放 cache 下也方便用户一键删掉重测。
    """
    name = str(model_name or "").strip() or "(未命名模型)"
    d = _load_obs()
    d[name] = {"supported": False, "evidence": str(evidence)[:500],
               "at": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time()}
    try:
        os.makedirs(os.path.dirname(_obs_path()), exist_ok=True)
        with open(_obs_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        logger.info("已记录：模型 %s 实测不支持 draft（%s）", name, str(evidence)[:120])
    except Exception as e:  # noqa: BLE001
        logger.warning("记录 draft 不支持失败（不影响生成）：%s", e)


def draft_support(model_name: str, *, adapter_says: Optional[bool] = None) -> Dict[str, Any]:
    """这台模型**到底**能不能走"便宜那一档"。以实测记录优先于静态声明。

    ★ 三态，不许含糊（用户的要求是"如实"，不是"好听"）：
      · `supported=False, source=实测`   —— 被平台拒过，**确定没有**便宜档
      · `supported=True,  source=实测`   —— 真出过 draft（会被记下来）
      · `supported=True,  source=厂商能力表` —— **只是声明支持，本机还没试过**。
        界面必须把这个区别说出来：实测 `doubao-seedance-1-0-pro` 在 t2v 下
        就被拒过，而它的适配器 `supports_draft` 一直是 True。
        把"未实测"说成"能用"，用户就会以为自己在省钱。
    """
    name = str(model_name or "").strip()
    obs = _load_obs().get(name) if name else None
    if obs and obs.get("supported") is False:
        return {
            "supported": False,
            "source": "实测",
            "evidence": obs.get("evidence") or "",
            "at": obs.get("at") or "",
            "note": (f"这台模型（{name}）**不支持草稿档**，只能直接出成片（更贵）。"
                     f"想要「低价草稿 → 高档成片」这条省钱路线，"
                     f"请换成支持 draft 的模型（如 即梦/Seedance 2.5 系列）。"),
        }
    if obs and obs.get("supported") is True:
        return {"supported": True, "source": "实测",
                "at": obs.get("at") or "",
                "note": f"这个模型实测**真的**出过草稿档（{obs.get('at') or ''}）"}
    if adapter_says is False:
        return {"supported": False, "source": "厂商能力表",
                "note": "当前模型不支持草稿模式（Draft）—— 没有便宜那一档。"}
    if adapter_says:
        return {
            "supported": True, "source": "厂商能力表（未实测）",
            "verified": False,
            "note": ("厂商能力表**声明**支持草稿档，但这台机器上还没实测过 —— "
                     "第一次出样片时如果平台拒了 draft，程序会自动去掉 draft 出片，"
                     "并把这次拒绝**记下来**，之后这里就会如实显示「不支持」。"),
        }
    return {"supported": False, "source": "未知",
            "note": "没拿到适配器能力声明，也没有实测记录 —— 不敢替你打包票"}


def _adapter_supports_draft(provider: str, model_name: str, api_key: str = "") -> Optional[bool]:
    try:
        from core.adapters import get_adapter
        a = get_adapter(provider, api_key=api_key or "", config={"model_name": model_name})
        return bool(getattr(a, "supports_draft", False))
    except Exception as e:  # noqa: BLE001
        logger.warning("取适配器 draft 能力失败：%s", e)
        return None


def _has_local_clip(shot: Dict[str, Any]) -> bool:
    cands = shot.get("candidates") or []
    if isinstance(cands, str):
        try:
            cands = json.loads(cands) if cands.strip() else []
        except Exception:  # noqa: BLE001
            cands = []
    for c in (cands if isinstance(cands, list) else []):
        if not isinstance(c, dict):
            continue
        p = c.get("path") or c.get("video_path") or ""
        try:
            if p and os.path.exists(str(p)) and os.path.getsize(str(p)) > 102400:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _segments_ok(shot: Dict[str, Any]) -> int:
    segs = shot.get("segments") or []
    if isinstance(segs, str):
        try:
            segs = json.loads(segs) if segs.strip() else []
        except Exception:  # noqa: BLE001
            segs = []
    if not isinstance(segs, list):
        return 0
    return sum(1 for s in segs if isinstance(s, dict) and s.get("status") == "ok")


def shot_tiers(shot: Dict[str, Any], *,
               settings: Optional[Dict[str, Any]] = None,
               api_key: str = "") -> Dict[str, Any]:
    """一个分镜当前的三档状态 + 下一件该做的事 + **实话**。

    纯只读：不写库、不调网络、不起隧道。
    """
    settings = settings or {}
    provider = str(shot.get("model_provider") or "")
    model_name = str(shot.get("model_name") or "")
    pst = str(shot.get("previz_status") or "none")
    ppath = str(shot.get("previz_path") or "")
    previz_exists = bool(ppath) and os.path.exists(ppath)

    approved = pst in ("draft_approved", "final_ready")
    g1_state = ("done" if (previz_exists and approved) else
                "waiting_confirm" if previz_exists else "todo")
    g1_detail = {
        "done": "已渲好且你已确认 —— 它会作为运镜/节奏的控制信号（拿不到公网地址时退化成提示词）",
        "waiting_confirm": "已渲好，**等你确认**（没确认的白膜不会被用来约束成片）",
        "todo": "还没渲 —— 免费，建议先渲（能省掉大量无效抽卡）",
    }[g1_state]

    # 白膜到底能不能真的约束生成（参考视频要公网 URL）
    pub = {"possible": False, "reason": ""}
    try:
        from core import publish as _pub
        pub["possible"] = bool(_pub.public_base_url(settings)
                               or _pub.named_tunnel_ready(settings)
                               or _pub.tunnel_available())
        if not pub["possible"]:
            pub["reason"] = ("这台机器在 NAT 后面，白膜发不成公网 URL —— "
                             "本次只能退化成「把白膜定下的运镜写进提示词」")
    except Exception as e:  # noqa: BLE001
        pub["reason"] = f"判断公网通道失败：{e}"

    seg_ok = _segments_ok(shot)
    has_clip = seg_ok > 0 or _has_local_clip(shot)
    final_ready = pst == "final_ready" or (has_clip and str(shot.get("status")) == "completed")

    ds = draft_support(model_name, adapter_says=_adapter_supports_draft(provider, model_name, api_key))
    g2_available = bool(ds.get("supported"))

    if final_ready:
        g2_state = "done" if has_clip else "todo"
    else:
        g2_state = "done" if has_clip else ("todo" if g2_available else "unavailable")

    g3_state = "done" if final_ready else "todo"

    out = {
        "G1": {**TIERS["G1"], "state": g1_state, "detail": g1_detail,
               "path": ppath, "previz_status": pst,
               "reference_channel": pub},
        "G2": {**TIERS["G2"], "state": g2_state,
               "detail": ("已经有本地片段了" if has_clip else
                          ("可以出一版便宜样片，确认画风/表演之后再升成片"
                           + ("（厂商声明支持，本机还没实测过）"
                              if ds.get("source") == "厂商能力表（未实测）" else "")
                           if g2_available else
                           "**这一档在你的模型上不可用**")),
               "available": g2_available,
               "verified": bool(ds.get("verified", ds.get("source") == "实测")),
               "note": ds.get("note") or ""},
        "G3": {**TIERS["G3"], "state": g3_state,
               "detail": ("已出成片" if final_ready else "等 G2 确认后再出"),
               "segments_ok": seg_ok, "has_clip": has_clip},
        "model": {"provider": provider, "name": model_name,
                  "supports_draft": g2_available, "draft_support": ds},
    }

    # 下一件该做的事 + 一句人话
    if g1_state == "todo":
        nxt, why = "render_previz", "先渲白膜（免费）—— 在花钱前把运镜和节奏定下来"
    elif g1_state == "waiting_confirm":
        nxt, why = "review_previz", "看一眼白膜的画面，确认运镜/走位/节奏没问题"
    elif not has_clip and g2_available:
        nxt, why = "generate_preview", "出 480P 草稿样片（便宜档），确认画风与表演"
    elif not has_clip:
        nxt, why = "generate_final", ("你的模型不支持草稿档 —— 只能直接出成片（更贵），"
                                      "没有二次确认的机会")
    else:
        nxt, why = "compose_film", "镜头都有片段了，可以配音 + 拼成成片"
    out["next_action"] = nxt
    out["next_why"] = why
    out["draft_note"] = ds.get("note") or ""
    out["money_warning"] = ("" if g2_available else
                            f"⚠ 你这台模型（{model_name or provider}）**不支持草稿档**，"
                            f"走不了「低价草稿 → 高档成片」这条省钱路线，只能直接出成片（更贵）。")
    return out


def project_tiers(db, pid: str, *, settings: Optional[Dict[str, Any]] = None,
                  shots: Optional[list] = None) -> Dict[str, Any]:
    """整片视角的三档汇总（给资产页/分镜页画一条链路）。"""
    settings = settings if settings is not None else (db.get_all_settings() or {})
    keys = settings.get("api_keys") or {}
    shots = shots if shots is not None else (db.list_shots(pid) or [])
    shots = sorted(shots, key=lambda s: s.get("order_index") or 0)

    per = []
    for s in shots:
        prov = str(s.get("model_provider") or "")
        key = ""
        for k, v in keys.items():
            if k and prov and k.lower() == prov.lower():
                key = v or ""
                break
        per.append({"shot_id": s.get("id"),
                    "order_index": s.get("order_index"),
                    "tiers": shot_tiers(s, settings=settings, api_key=key)})

    def _count(key: str, state: str) -> int:
        return sum(1 for p in per if p["tiers"][key]["state"] == state)

    models = {}
    for p in per:
        m = p["tiers"]["model"]
        models[m["name"] or m["provider"]] = m["supports_draft"]

    return {
        "shots": per,
        "count": len(per),
        "summary": {
            "G1_done": _count("G1", "done"),
            "G1_waiting_confirm": _count("G1", "waiting_confirm"),
            "G1_todo": _count("G1", "todo"),
            "G2_done": _count("G2", "done"),
            "G2_available": all(p["tiers"]["G2"]["available"] for p in per) if per else None,
            "G3_done": _count("G3", "done"),
        },
        "models": models,
        "draft_route_usable": all(p["tiers"]["G2"]["available"] for p in per) if per else None,
        "money_warning": next((p["tiers"]["money_warning"] for p in per
                               if p["tiers"]["money_warning"]), ""),
        "legend": [TIERS["G1"], TIERS["G2"], TIERS["G3"]],
    }
