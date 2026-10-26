# -*- coding: utf-8 -*-
"""VideoForge · 硅基流动（SiliconFlow）ASR 客户端 —— 台词提取的主链路

═══════════════════════════════════════════════════════════════════
为什么单独一个模块
═══════════════════════════════════════════════════════════════════
用户原话（2026-10-24）：「如果是获取台词用我给的硅基流动的 asr」。

于是「从参考视频里抠台词」这件事的**第一步**就是 ASR。这个模块把
硅基流动的转写接口包成 VideoForge 的既有风格：拿不到 Key 就说拿不到、
出错就说清楚、返回值永远带 `ok` 和 `error`（不抛裸异常给上层）。

═══════════════════════════════════════════════════════════════════
★ 关键事实（实测 + 官方 OpenAPI 逐字核对，2026-10-24）
═══════════════════════════════════════════════════════════════════
硅基流动的 `/v1/audio/transcriptions` **只返回纯文本**：

    {"text": "...", "language": "Chinese", "usage": {...}}

**没有时间戳、没有说话人分离、没有分句。**
实测（2026-10-24，SenseVoiceSmall，30s 音频，HTTP 200，耗时 3.3s）返回：

    {"text":"🎼王姐姐，你快来，小雨，你先别跑了，我想跟你说个事儿……"}

⇒ 所以本模块提供**两条**路子，各司其职：

  · `transcribe()`        —— 硅基流动，纯文本。快、准、便宜（花钱）。
                             用于「整段台词稿」「校对」「人味校验」。
  · `transcribe_timed()`  —— 本地 whisper（CPU）出**带时间戳的分句**，
                             再按时间窗去硅基流动取整段文本做**校对**。
                             用于「哪句话在第几秒说」「台词落在哪个镜头里」。

★ 别把两者的职责混了：时间戳靠本地 whisper（免费），
  文本质量靠硅基流动（花钱但更准）。

═══════════════════════════════════════════════════════════════════
Key 从哪来
═══════════════════════════════════════════════════════════════════
复用设置里**已经存在**的那把：`tts_api_keys.siliconflow`
（VideoForge 的语音链路本来就在用它，见 core/voice/dispatcher.py）。
查找顺序：tts_api_keys → api_keys → llm_api_keys → 环境变量。
**不硬编码任何 Key**。

═══════════════════════════════════════════════════════════════════
花钱纪律
═══════════════════════════════════════════════════════════════════
每次调用都花钱，所以：
  · 默认 `dry_run=True` 的 `estimate()` 可以先报「大概多少钱、多长」；
  · 真跑要走 `core.budget.assert_spend_allowed()`（用户级开关 + 紧急停止）；
  · 每笔花费用 `core.budget.add_cost()` 记账，进度条能看见。

纯函数 + 一次 HTTP。零数据库写入（除记账）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.asr.siliconflow")

#: 硅基流动转写端点
API_BASE = "https://api.siliconflow.cn/v1"
TRANSCRIBE_URL = API_BASE + "/audio/transcriptions"
MODELS_URL = API_BASE + "/models"

#: 默认模型。SenseVoiceSmall：带情感标记、能识别中英粤、便宜。
DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"

#: 备用模型（按实测/可用性排序）
FALLBACK_MODELS = [
    "FunAudioLLM/SenseVoiceSmall",
    "TeleAI/TeleSpeechASR",
    "Qwen/Qwen3-ASR-1.7B",
]

#: 本地 whisper 的档位（时间戳用；CPU 跑）
WHISPER_MODELS = ["tiny", "small", "base", "medium"]

#: 计价（粗口径，用于「大概多少钱」的提示，**不是账单**）。
#: 硅基流动公开价按「秒」计，不同模型差异大；这里给一个保守上界，
#: 只用于让用户判断「值不值」，真扣费以官方为准。
COST_HINT_PER_MINUTE = {
    "FunAudioLLM/SenseVoiceSmall": 0.02,
    "TeleAI/TeleSpeechASR": 0.02,
    "Qwen/Qwen3-ASR-1.7B": 0.06,
}


# ────────────────────────────── Key ──────────────────────────────


def get_api_key(settings: Optional[Dict[str, Any]] = None) -> str:
    """从设置里找硅基流动的 Key。找不到就返回空串（**不编造**）。

    查找顺序（第一个非空胜出）：
        tts_api_keys → api_keys → llm_api_keys → 环境变量
    名字匹配大小写不敏感：siliconflow / silicon_flow / silicon-flow / silicon。
    """
    s = settings or {}
    for bucket in ("tts_api_keys", "api_keys", "llm_api_keys"):
        d = s.get(bucket)
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            nk = str(k or "").strip().lower().replace("_", "").replace("-", "")
            if nk in ("siliconflow", "silicon", "硅基流动") and str(v or "").strip():
                return str(v).strip()
    for env in ("SILICONFLOW_API_KEY", "SILICONFLOW_KEY", "SILICON_API_KEY"):
        v = os.environ.get(env, "").strip()
        if v:
            return v
    return ""


def _settings(db=None) -> Dict[str, Any]:
    """拿设置。

    ★ 踩过的坑（2026-10-24）：原来写的是 `from .db import get_db` ——
      **`core/db.py` 里根本没有 `get_db` 这个函数**（只有 `Database` 类），
      于是 ImportError 被吞掉 → 返回 {} → 永远说「没有配置 Key」。
      即使程序在跑也拿不到设置。

      正确做法：**照 main.py 的同一套路径解析**（core/data_root.py 是单一真相源）：
        resolve_data_root() → <root>/videoforge.db → Database(...).get_all_settings()
    """
    if db is not None:
        try:
            return db.get_all_settings() or {}
        except Exception:  # noqa: BLE001
            return {}
    try:
        from .data_root import resolve_data_root
        from .db import Database
        root = resolve_data_root()
        return Database(os.path.join(root, "videoforge.db")).get_all_settings() or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("读设置失败（忽略）：%s", e)
        return {}


def key_status(db=None) -> Dict[str, Any]:
    """给 UI/工具看的 Key 状态（**不回显 Key 本体**）。"""
    key = get_api_key(_settings(db))
    return {
        "configured": bool(key),
        "source": ("settings" if key else "missing"),
        "models": list(FALLBACK_MODELS),
        "default_model": DEFAULT_MODEL,
        "cost_hint_per_minute": COST_HINT_PER_MINUTE.get(DEFAULT_MODEL, 0.02),
    }


# ──────────────────────────── 模型列表 ────────────────────────────


def list_models(api_key: str = "", *, timeout: int = 30) -> Dict[str, Any]:
    """列出账号可用的 ASR / 音频模型。"""
    key = (api_key or get_api_key(_settings())).strip()
    if not key:
        return {"ok": False, "error": "没有配置硅基流动 Key（tts_api_keys.siliconflow）", "models": []}
    try:
        import requests
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "缺 requests：%s" % e, "models": []}
    try:
        r = requests.get(MODELS_URL, params={"type": "audio"},
                         headers={"Authorization": "Bearer %s" % key}, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "请求失败：%s: %s" % (type(e).__name__, e), "models": []}
    if r.status_code != 200:
        return {"ok": False, "error": "HTTP %s: %s" % (r.status_code, r.text[:200]), "models": []}
    try:
        data = r.json().get("data") or []
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "响应不是 JSON：%s" % e, "models": []}
    ids = [str(x.get("id")) for x in data if isinstance(x, dict) and x.get("id")]
    return {"ok": True, "models": ids, "asr_models": [i for i in ids
                                                     if "asr" in i.lower() or "speech" in i.lower()]}


# ──────────────────────────── 纯文本转写 ────────────────────────────


def estimate(audio_seconds: float, *, model: str = DEFAULT_MODEL) -> Dict[str, Any]:
    """预估一次调用的耗时与花费（**不调用 API**）。"""
    try:
        sec = max(0.0, float(audio_seconds or 0))
    except Exception:  # noqa: BLE001
        sec = 0.0
    per_min = COST_HINT_PER_MINUTE.get(model, 0.02)
    cost = round(sec / 60.0 * per_min, 4)
    return {
        "model": model,
        "audio_seconds": round(sec, 2),
        "est_cost_yuan": cost,
        "est_cost_note": "粗口径上界，用于判断值不值；真扣费以硅基流动账单为准",
        "est_seconds": max(3, int(sec * 0.12) + 2),
    }


def transcribe(audio_path: str, *, model: str = DEFAULT_MODEL, api_key: str = "",
               timeout: int = 600, project_id: str = "", db=None) -> Dict[str, Any]:
    """把音频转成**纯文本台词**。

    返回：
        {"ok": bool, "text": str, "language": str, "usage": {...},
         "model": str, "elapsed": float, "cost_yuan": float,
         "error": str, "note": str}
    """
    path = str(audio_path or "").strip()
    if not path or not os.path.isfile(path):
        return {"ok": False, "text": "", "error": "音频文件不存在：%s" % (path or "(空)"),
                "model": model, "elapsed": 0.0, "cost_yuan": 0.0}
    key = (api_key or get_api_key(_settings(db))).strip()
    if not key:
        return {"ok": False, "text": "",
                "error": "没有配置硅基流动 Key（设置里的 tts_api_keys.siliconflow）",
                "model": model, "elapsed": 0.0, "cost_yuan": 0.0}

    # 花钱闸门：用户级开关 + 紧急停止
    try:
        from .budget import assert_spend_allowed
        assert_spend_allowed(db, provider="siliconflow",
                             detail="（ASR 台词提取）")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": "", "error": str(e), "model": model,
                "elapsed": 0.0, "cost_yuan": 0.0}

    try:
        import requests
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": "", "error": "缺 requests：%s" % e,
                "model": model, "elapsed": 0.0, "cost_yuan": 0.0}

    t0 = time.time()
    try:
        with open(path, "rb") as f:
            r = requests.post(
                TRANSCRIBE_URL,
                headers={"Authorization": "Bearer %s" % key},
                files={"file": (os.path.basename(path), f, "application/octet-stream")},
                data={"model": model},
                timeout=timeout,
            )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": "", "error": "请求失败：%s: %s" % (type(e).__name__, e),
                "model": model, "elapsed": round(time.time() - t0, 2), "cost_yuan": 0.0}

    elapsed = round(time.time() - t0, 2)
    if r.status_code != 200:
        return {"ok": False, "text": "",
                "error": "HTTP %s：%s" % (r.status_code, r.text[:300]),
                "model": model, "elapsed": elapsed, "cost_yuan": 0.0}
    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": "", "error": "响应不是 JSON：%s" % e,
                "model": model, "elapsed": elapsed, "cost_yuan": 0.0}

    text = str(data.get("text") or "")
    usage = data.get("usage") or {}
    secs = 0.0
    try:
        secs = float((usage or {}).get("seconds") or 0)
    except Exception:  # noqa: BLE001
        secs = 0.0
    cost = round(secs / 60.0 * COST_HINT_PER_MINUTE.get(model, 0.02), 4)

    # 记账（失败不阻断主流程）
    try:
        from .budget import add_cost
        if cost > 0 and project_id:
            add_cost(project_id, cost, db=db)
    except Exception:  # noqa: BLE001
        logger.warning("ASR 记账失败（忽略）", exc_info=True)

    return {
        "ok": True,
        "text": text,
        "language": str(data.get("language") or ""),
        "usage": usage,
        "model": model,
        "elapsed": elapsed,
        "cost_yuan": cost,
        "error": "",
        "note": "硅基流动只返回纯文本（无时间戳/无说话人）；要时间戳请用 transcribe_timed()",
    }


# ─────────────────────── 带时间戳的转写（本地 whisper）───────────────────────


def _load_whisper(size: str):
    import whisper  # 延迟导入：加载模型要几百 MB 内存
    return whisper.load_model(size)


def transcribe_timed(audio_path: str, *, whisper_size: str = "small",
                     model: str = DEFAULT_MODEL, api_key: str = "",
                     use_siliconflow: bool = True, project_id: str = "",
                     db=None) -> Dict[str, Any]:
    """**带时间戳**的台词转写。

    时间戳来自本地 whisper（CPU、免费、不联网）；
    文本质量用硅基流动校对（要花钱，`use_siliconflow=False` 可关掉）。

    返回：
        {"ok": bool,
         "segments": [{"start": float, "end": float, "text": str}],
         "full_text": str,
         "siliconflow_text": str,
         "whisper_model": str, "elapsed": float, "cost_yuan": float,
         "error": str, "note": str}
    """
    path = str(audio_path or "").strip()
    if not path or not os.path.isfile(path):
        return {"ok": False, "segments": [], "full_text": "",
                "error": "音频文件不存在：%s" % (path or "(空)"), "elapsed": 0.0}
    t0 = time.time()
    segments: List[Dict[str, Any]] = []
    err = ""
    try:
        w = _load_whisper(whisper_size)
        res = w.transcribe(path, language="zh", verbose=False,
                            fp16=False, temperature=0.0)
        for s in (res.get("segments") or []):
            segments.append({
                "start": round(float(s.get("start") or 0), 2),
                "end": round(float(s.get("end") or 0), 2),
                "text": str(s.get("text") or "").strip(),
            })
    except Exception as e:  # noqa: BLE001
        err = "本地 whisper 失败：%s: %s" % (type(e).__name__, e)

    full_text = "".join(s["text"] for s in segments)
    sf_text = ""
    cost = 0.0
    if use_siliconflow:
        r = transcribe(path, model=model, api_key=api_key,
                       project_id=project_id, db=db)
        if r.get("ok"):
            sf_text = str(r.get("text") or "")
            cost = float(r.get("cost_yuan") or 0.0)
        else:
            err = (err + " ｜ " if err else "") + str(r.get("error") or "")

    return {
        "ok": bool(segments) and not err,
        "segments": segments,
        "full_text": full_text,
        "siliconflow_text": sf_text,
        "whisper_model": whisper_size,
        "elapsed": round(time.time() - t0, 2),
        "cost_yuan": cost,
        "error": err,
        "note": ("时间戳=本地 whisper（免费）；siliconflow_text=硅基流动整段校对文本（花钱）。"
                 "两者不一致时以时间戳为准、以硅基流动为准文本，**并列保留**不要互相覆盖。"),
    }


def clean_emotion_tags(text: str) -> str:
    """去掉 SenseVoice 的情感/事件标记（🎼 / 😊 / <|...|> 这类）。

    ★ 实测：SenseVoiceSmall 会在 text 里塞 emoji 与尖括号标记，
      直接拿去当台词会显得莫名其妙。这里统一剥掉，**但保留原标注文本**
      （调用方需要时可以自己比对）。
    """
    import re
    s = str(text or "")
    s = re.sub(r"<\|[^|]*\|>", "", s)
    s = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()
