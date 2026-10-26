# -*- coding: utf-8 -*-
"""VideoForge · 媒体接入层「真实探测」脚本（**手动运行 · 会产生极小费用**）

⚠️ 这个文件**不是**回归测试（文件名不以 test_ 开头，pytest 不会收集）。
   它用应用库里的**真实 Key** 打真实平台，用来给交付回执留证据。

跑法：
    cd D:\\VideoForge-dev\\backend
    python -X utf8 tests/_live_probe_media_bridge.py

预算与边界（2026-10-25 本次）：
  · 图像：**只做鉴权探测**（GET /v1/models），不发起生成 ——
    理由：拿不到该账号的单价，也拿不到余额端点（/v1/user/info 已 410 下线），
    按任务约束「不能确定价格就只做鉴权探测」，如实写"未做真实生成验证"。
  · 音频：真合成一句 6 字中文（¥0.5/万字档位 → 约 ¥0.0003），上限 ¥0.05。
  · 另外用 MiniMax 打一次真实调用，只为**记录真实错误码**（429 额度用尽 → 不计费）。
  · 全程不碰视频生成；不动数据库（只读打开）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core import image_providers as IP                 # noqa: E402
from core.voice.base import TTSRequest                 # noqa: E402
from core.voice import dispatcher as VD                # noqa: E402


def load_settings_readonly() -> dict:
    """**只读**读应用库的 settings（不用 core.db：它初始化时会建表/迁移，会写库）。"""
    db = Path(os.environ.get("LOCALAPPDATA", "")) / "VideoForge" / "data" / "videoforge.db"
    conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    try:
        rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    finally:
        conn.close()
    settings = {}
    for key, value in rows.items():
        try:
            settings[key] = json.loads(value)
        except Exception:  # noqa: BLE001 - 普通字符串设置
            settings[key] = value
    return settings


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _probe_image(settings: dict) -> dict:
    """图像：免费鉴权探测 + 注册表型号可用性核对（不生成，不花钱）。"""
    out = {"provider": "siliconflow", "generated": False,
           "why_not_generated": ("拿不到单价与余额（/v1/user/info 已 410 下线），"
                                 "按任务约束不发起计费生成")}
    probe = asyncio.run(IP.probe_auth("siliconflow", settings=settings))
    out["auth_probe"] = {k: probe.get(k) for k in
                         ("ok", "status", "url", "key_source", "latency_ms", "error")}
    # 这个账号当前真正可用的图像模型（注册表默认型号在不在里面，是真实交付问题）
    try:
        import httpx
        key = IP.resolve_key("siliconflow", settings)["key"]
        r = httpx.get("https://api.siliconflow.cn/v1/models?type=image",
                      headers={"Authorization": f"Bearer {key}"}, timeout=30)
        models = [m.get("id") for m in (r.json().get("data") or [])]
        out["available_image_models"] = models
        entry = IP.provider_entry("siliconflow") or {}
        declared = [m.get("id") for m in (entry.get("models") or [])]
        out["registry_declared_models"] = declared
        out["registry_models_missing_on_account"] = [m for m in declared if m not in models]
    except Exception as e:  # noqa: BLE001
        out["model_list_error"] = f"{type(e).__name__}: {e}"
    return out


def _ffmpeg_duration(ffmpeg: str, path: str) -> str:
    """用 ffmpeg 读出**真实音频时长**（比按字数估算更硬的证据）。"""
    if not ffmpeg or not os.path.exists(ffmpeg):
        return ""
    try:
        import subprocess
        pr = subprocess.run([ffmpeg, "-i", path, "-hide_banner"],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        for line in (pr.stderr or "").splitlines():
            if "Duration" in line:
                return line.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _probe_audio(settings: dict) -> dict:
    """音频：真合成一句 ≤10 字中文（siliconflow），再记录 minimax 的真实错误码。"""
    cache = Path(str(settings.get("cache_dir") or (BACKEND / "data" / "cache")))
    out_dir = cache / "voice" / "_live_probe"
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    # ★ 2026-10-25 实测结论：
    #   · CosyVoice2-0.5B 连续三次**服务端无响应**（120s/200s/92.6s），临时失败；
    #   · 同 Key、同端点换成 MOSS-TTSD 后 1.5s 返回 78KB mp3（本脚本用这条）。
    cases = [
        ("siliconflow", "siliconflow:fnlp/MOSS-TTSD-v0.5:fnlp/MOSS-TTSD-v0.5:alex",
         "你好，测试。"),
        ("minimax", "minimax:female-chengshu", "你好。"),
    ]
    for provider, voice_id, text in cases:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = out_dir / f"{provider}-{stamp}.mp3"
        t0 = time.time()
        try:
            res = asyncio.run(VD.synthesize(
                TTSRequest(text=text, voice_id=voice_id, output_path=str(dest)),
                settings))
            elapsed = round(time.time() - t0, 2)
            item = {"provider": provider, "voice_id": voice_id, "text": text,
                    "chars": len(text), "ok": bool(res.success),
                    "error": res.error, "latency_s": elapsed,
                    "model": (res.raw or {}).get("model") if res.raw else None}
            if res.success and dest.exists():
                item.update({"path": str(dest), "bytes": dest.stat().st_size,
                             "sha256_16": _sha256(dest),
                             "head_hex": dest.read_bytes()[:8].hex(),
                             "is_mp3": dest.read_bytes()[:3] == b"ID3",
                             "duration": _ffmpeg_duration(
                                 settings.get("ffmpeg_path") or "", str(dest))})
            results.append(item)
        except Exception as e:  # noqa: BLE001 - 探测脚本不能自己炸掉
            results.append({"provider": provider, "ok": False,
                            "error": f"{type(e).__name__}: {e}",
                            "latency_s": round(time.time() - t0, 2)})
    return {"cases": results}


def main() -> int:
    settings = load_settings_readonly()
    report = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "image": _probe_image(settings),
              "audio": _probe_audio(settings),
              "budget": {"image_spent_yuan": 0.0,
                         "note": "图像未生成（0 元）；音频仅 siliconflow 一句 6 字"}}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    out = BACKEND / "data" / "cache" / "media_bridge_live_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print("\n[saved]", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
