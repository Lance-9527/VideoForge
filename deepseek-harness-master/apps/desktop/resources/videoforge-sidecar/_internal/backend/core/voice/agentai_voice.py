# -*- coding: utf-8 -*-
"""
VideoForge · 音色克隆 / 音色设计（中转站 /v1/audio/voices）
=========================================================

用户原话：「音色要保持人物的一致性！」「我给你开魔法」（= 网关已开通克隆）。

## 实测出来的真实形状（2026-10-23，不是猜的）

    POST /v1/audio/voices          multipart/form-data
        字段：file（音频样本，mp3/wav/m4a）+ model（如 speech-2.8）+ name
        → 202 {"id": "<voice_id>", "status": "processing", "task_id": "<UUID>"}

    GET  /v1/tasks/{task_id}       ← 任务进度在**这里**，不在 /audio/voices/{id}
        → {"id": "...", "object": "task", "status": "completed|failed", ...}

    GET  /v1/audio/voices          ← 克隆成功后才会出现在这个列表里

## 两条硬约束（踩出来的）

1. **样本时长必须 10~300 秒**。8 秒的样本会被任务判 failed：
   `invalid_audio_duration: Audio duration must be between 10 and 300 seconds`。
   （所以这里上传前先探测时长，不够就直接给出可执行的提示，不白等一次失败。）
2. **上传本身可能很慢**（实测 400KB 的文件有一次 300 秒没返回）。
   所以超时给到 600 秒，并且**不重试**（克隆是按次计费的，重试=重复花钱）。

## 拿到 voice_id 之后怎么用

    /v1/audio/speech  {model:"speech-2.8", voice:"<voice_id>"}
    在 VideoForge 里写成：`agentai:speech-2.8:<voice_id>`，
    写进 `characters.voice_id` ⇒ **同一角色所有台词都用这把嗓子**（一致性就靠这个）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://agentai.token6688.com/v1"
MIN_SAMPLE_SEC = 10.0
MAX_SAMPLE_SEC = 300.0


def _key(settings: Dict[str, Any]) -> str:
    for bucket in ("tts_api_keys", "llm_api_keys", "api_keys"):
        d = (settings or {}).get(bucket) or {}
        if not isinstance(d, dict):
            continue
        for name in ("agentai", "openai", "token6688"):
            v = str(d.get(name) or "").strip()
            if v:
                return v
            for k, vv in d.items():
                if str(k).strip().lower() == name and str(vv or "").strip():
                    return str(vv).strip()
    return ""


async def _duration(path: str) -> float:
    try:
        from core.ffmpeg_manager import get_ffmpeg_for_postprocess
        ff = get_ffmpeg_for_postprocess()
        proc = await asyncio.create_subprocess_exec(
            ff, "-hide_banner", "-i", path, "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _o, err = await proc.communicate()
        txt = (err or b"").decode("utf-8", "replace")
        import re
        m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", txt)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception as e:  # noqa: BLE001
        logger.warning("探测样本时长失败：%s", e)
    return 0.0


async def clone_voice(sample_path: str, name: str, settings: Dict[str, Any] = None,
                      *, model: str = "speech-2.8", timeout: float = 600.0) -> Dict[str, Any]:
    """上传音频样本 → 克隆音色。返回 {ok, voice_id?, task_id?, reason?}（失败不抛）。"""
    settings = settings or {}
    out: Dict[str, Any] = {"ok": False, "voice_id": "", "task_id": "", "reason": "",
                           "duration": 0.0}
    if not os.path.exists(sample_path):
        out["reason"] = f"样本文件不存在：{sample_path}"
        return out
    dur = await _duration(sample_path)
    out["duration"] = round(dur, 2)
    if dur and not (MIN_SAMPLE_SEC <= dur <= MAX_SAMPLE_SEC):
        out["reason"] = (f"样本 {dur:.1f} 秒 —— 网关要求 **10~300 秒**（实测硬约束，"
                        f"少于 10 秒任务会判 failed）。请给一段 15~60 秒的干净人声。")
        return out
    # ★★ 2026-10-23（用户账单实测后叫停）：网关 **voice-clone ≈ 14.89 算力/次**，
    #   是本项目最贵的单次调用（比一条 12 秒视频还贵）。所以这里**默认拒绝**，
    #   只有设置 `voice_clone_enabled=true` 才允许 —— 防止任何入口误触。
    if str(settings.get("voice_clone_enabled") or "").strip().lower() not in (
            "1", "true", "yes", "on"):
        out["reason"] = ("音色克隆默认关闭（实测 14.89 算力/次，全项目最贵的单项）。"
                        "确需克隆：设置 voice_clone_enabled=true。")
        out["blocked"] = True
        return out
    key = _key(settings)
    if not key:
        out["reason"] = "没有中转站 Key（设置里 agentai/openai 那把）"
        return out
    try:
        files = {"file": (os.path.basename(sample_path), open(sample_path, "rb"),
                          "audio/mpeg" if sample_path.lower().endswith((".mp3", ".m4a"))
                          else "audio/wav")}
        data = {"model": model, "name": name or "vf-voice"}
        async with httpx.AsyncClient(timeout=timeout) as cl:
            r = await cl.post(f"{API_BASE}/audio/voices", files=files, data=data,
                              headers={"Authorization": f"Bearer {key}"})
        if r.status_code >= 400:
            out["reason"] = f"上传样本失败 HTTP {r.status_code}: {r.text[:300]}"
            return out
        j = r.json() or {}
        out["voice_id"] = str(j.get("id") or "")
        out["task_id"] = str(j.get("task_id") or "")
        if not out["task_id"]:
            out["reason"] = f"上传没返回 task_id：{str(j)[:200]}"
            return out
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"上传样本异常（克隆按次计费，不自动重试）：{type(e).__name__}: {e}"
        return out

    # 轮询任务（进度在 /v1/tasks/{task_id}）
    deadline = time.time() + max(60.0, timeout)
    last = ""
    while time.time() < deadline:
        await asyncio.sleep(6)
        try:
            async with httpx.AsyncClient(timeout=60.0) as cl:
                g = await cl.get(f"{API_BASE}/tasks/{out['task_id']}",
                                 headers={"Authorization": f"Bearer {key}"})
            body = g.text or ""
            last = body[:200]
            low = body.lower()
            if '"completed"' in low or '"succeeded"' in low or '"ready"' in low:
                out["ok"] = True
                return out
            if '"failed"' in low:
                try:
                    err = (g.json() or {}).get("error") or {}
                    msg = f"{err.get('code')}: {err.get('message')}"
                except Exception:  # noqa: BLE001
                    msg = body[:200]
                out["reason"] = f"克隆任务失败：{msg}"
                return out
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            continue
    out["reason"] = f"等克隆任务超时（最后状态：{last}）"
    return out


async def list_cloned(settings: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """列出已克隆的音色（网关侧音色库）。"""
    key = _key(settings or {})
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=60.0) as cl:
            r = await cl.get(f"{API_BASE}/audio/voices",
                             headers={"Authorization": f"Bearer {key}"})
        if r.status_code >= 400:
            return []
        j = r.json() or {}
        items = j.get("data") or []
        return items if isinstance(items, list) else []
    except Exception:  # noqa: BLE001
        return []
