# -*- coding: utf-8 -*-
"""
VideoForge · 云端唇形同步（走中转站的 kling-lip-sync）
=====================================================

用户原话（2026-10-23）：
    「音色要保持人物的一致性！再就是要跟视频里面人物的嘴型、唇语同步！」
    「本地太慢了！」        ← 明确否掉了"本地跑 Wav2Lip"

所以唇形同步走**中转站（agentai）**上的 `kling-lip-sync`：可灵官方唇形同步，
云端 GPU，几秒到十几秒出结果，不占本机 CPU。

## 接口形状（本机实测探出来的，不是猜的）

    POST https://agentai.token6688.com/v1/videos/generations
    { "model": "kling-lip-sync", "prompt": "…",
      "videos": ["<公网可下载的视频 URL>"],
      "audios": ["<公网可下载的音频 URL>"] }

字段校验是**按别名**的（探针报错原文给了可接受的名字）：
    videos / video / video_url / video_urls / videoUrls / reference_video / input_video
    audios / audio_url / audio_urls / audioUrls / reference_audio
两个都必填 —— 只给视频会报 `params.audios is required`。

## 为什么必须先 publish

和参考视频一样，平台侧是**服务端去下载**这两个 URL，所以不能传本地路径、
也不能传 base64。这里复用 `core/publish.py`（境内优先走 SiliconFlow OSS，
隧道是兜底），发布失败就**如实报错**，绝不假装同步成功。

## 计费

按次计费（可灵官方口径）。所以**默认关闭**，只在这几种情况触发：
  · 设置里把 `lipsync_mode` 设成 `cloud`；
  · 或者显式调工具/REST（`/api/shots/{sid}/lipsync`）。
不做"偷偷每次合成自动跑一遍"——那会替用户花钱。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://agentai.token6688.com/v1"
MODEL = "kling-lip-sync"


def _key(settings: Dict[str, Any]) -> str:
    """取中转站 Key（与语音同一把：llm_api_keys.openai / api_keys.agentai）。"""
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


def enabled(settings: Dict[str, Any]) -> bool:
    return str((settings or {}).get("lipsync_mode") or "").strip().lower() == "cloud"


def _publish(path: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    from core import publish as _pub
    return _pub.publish_file(path, settings or {})


async def sync_video(video_path: str, audio_path: str, out_path: str,
                     settings: Dict[str, Any] = None, *,
                     timeout: float = 900.0,
                     prompt: str = "") -> Dict[str, Any]:
    """把 `audio_path` 的音频对口型到 `video_path` 上，结果落到 `out_path`。

    Returns: {ok, path?, task_id?, reason?, published?}
    **失败不抛异常**（调用方决定降级），但一定把真实原因带回去。
    """
    settings = settings or {}
    res: Dict[str, Any] = {"ok": False, "reason": "", "task_id": "", "published": {}}
    if not os.path.exists(video_path):
        res["reason"] = f"视频不存在：{video_path}"
        return res
    if not (audio_path and os.path.exists(audio_path)):
        res["reason"] = "这条镜头没有配音音频 —— 没有声音就没有口型可对"
        return res
    key = _key(settings)
    if not key:
        res["reason"] = ("没有中转站 Key（设置里 agentai/openai 那把），"
                        "发不出公网 URL、也调不了 kling-lip-sync")
        return res

    # ① 发布成公网 URL（平台侧要下载）
    pv = _publish(video_path, settings)
    pa = _publish(audio_path, settings)
    res["published"] = {"video": pv, "audio": pa}
    if not (pv.get("ok") and pa.get("ok")):
        res["reason"] = ("文件发布成公网 URL 失败："
                         f"video={(pv.get('reason') or '')[:120]} / "
                         f"audio={(pa.get('reason') or '')[:120]}")
        return res
    vurl, aurl = pv.get("url"), pa.get("url")

    # ② 建任务
    body = {
        "model": MODEL,
        "prompt": prompt or "把音频对口型到画面人物嘴唇上，保持原有画面、构图、光线不变",
        "videos": [vurl],
        "audios": [aurl],
        "client_request_id": "vf-lipsync-%d" % int(time.time()),
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as cl:
            r = await cl.post(f"{API_BASE}/videos/generations", json=body,
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"})
        if r.status_code >= 400:
            res["reason"] = f"建任务失败 HTTP {r.status_code}: {r.text[:300]}"
            return res
        data = r.json() or {}
        d = data.get("data") if isinstance(data.get("data"), dict) else data
        tid = str(d.get("id") or d.get("task_id") or d.get("taskId")
                  or (data.get("data") or {}).get("id") or "")
        res["task_id"] = tid
        if not tid:
            res["reason"] = f"建任务没返回 task_id：{str(data)[:300]}"
            return res
    except Exception as e:  # noqa: BLE001
        res["reason"] = f"建任务异常：{type(e).__name__}: {e}"
        return res

    # ③ 轮询
    deadline = time.time() + float(timeout)
    url = ""
    last = ""
    while time.time() < deadline:
        await asyncio.sleep(5)
        try:
            async with httpx.AsyncClient(timeout=60.0) as cl:
                g = await cl.get(f"{API_BASE}/video/generations/{tid}",
                                 headers={"Authorization": f"Bearer {key}"})
            if g.status_code >= 400:
                last = f"查任务 HTTP {g.status_code}: {g.text[:200]}"
                continue
            j = g.json() or {}
            dd = j.get("data") if isinstance(j.get("data"), dict) else j
            status = str(dd.get("status") or dd.get("state") or "").lower()
            last = status or str(dd)[:160]
            # 结果 URL 可能叫这些名字
            for k in ("url", "video_url", "videoUrl", "output", "result_url"):
                v = dd.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    url = v
                    break
            if not url:
                outs = dd.get("outputs") or dd.get("videos") or dd.get("result")
                if isinstance(outs, list):
                    for it in outs:
                        s = it if isinstance(it, str) else (it.get("url") if isinstance(it, dict) else "")
                        if isinstance(s, str) and s.startswith("http"):
                            url = s
                            break
            if url:
                break
            if status in ("failed", "error", "cancelled", "canceled"):
                res["reason"] = f"唇形同步任务失败（{status}）：{str(dd)[:300]}"
                return res
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            continue

    if not url:
        res["reason"] = f"等结果超时（{int(timeout)}s，最后状态：{last}）"
        return res

    # ④ 下载
    try:
        async with httpx.AsyncClient(timeout=300.0) as cl:
            rg = await cl.get(url)
        if rg.status_code >= 400 or len(rg.content) < 10240:
            res["reason"] = f"下载结果失败 HTTP {rg.status_code}（{len(rg.content)}B）"
            return res
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(rg.content)
    except Exception as e:  # noqa: BLE001
        res["reason"] = f"下载结果异常：{type(e).__name__}: {e}"
        return res

    res.update({"ok": True, "path": out_path, "bytes": os.path.getsize(out_path)})
    return res


async def sync_shot(db, shot: Dict[str, Any], settings: Dict[str, Any],
                    out_dir: str) -> Dict[str, Any]:
    """给**一个分镜**做唇形同步：视频用该镜已选定的成片片段，音频用它的配音轨。"""
    sid = str(shot.get("id") or "")
    video = ""
    for k in ("selected_path", "video_path", "previz_video", "clip_path"):
        v = shot.get(k)
        if isinstance(v, str) and v and os.path.exists(v):
            video = v
            break
    if not video:
        cands = shot.get("candidates") or []
        if isinstance(cands, str):
            try:
                import json as _json
                cands = _json.loads(cands)
            except Exception:  # noqa: BLE001
                cands = []
        for c in (cands if isinstance(cands, list) else []):
            p = c.get("path") if isinstance(c, dict) else None
            if p and os.path.exists(p) and c.get("selected"):
                video = p
                break
    if not video:
        return {"ok": False, "reason": "这一镜还没有选定成片片段，先出片再做唇形同步"}
    # 配音：dub 目录里的 m4a/mp3
    audio = ""
    base = os.path.dirname(video)
    for root, _dirs, files in os.walk(base):
        for fn in files:
            if fn.lower().endswith((".m4a", ".mp3", ".wav")) and "dub" in root.lower():
                audio = os.path.join(root, fn)
                break
        if audio:
            break
    if not audio:
        return {"ok": False, "reason": "没找到这一镜的配音音频（dub/*.m4a）—— 先配音"}
    out = os.path.join(out_dir or base, f"lipsync_{sid or 'shot'}.mp4")
    return await sync_video(video, audio, out, settings, prompt=(
        "把给定音频对口型到画面人物的嘴唇与下颌，保持画面内容、构图、镜头运动、"
        "光线与色调完全不变；不改变人物身份与服装；不要出现画面变形或抖动。"))
