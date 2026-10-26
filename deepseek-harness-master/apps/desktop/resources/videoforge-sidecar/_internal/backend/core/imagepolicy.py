# -*- coding: utf-8 -*-
r"""参考图被平台**内容策略**拒收的记录 + 免费用本地 ffmpeg 做风格化。

════════════════════════════════════════════════════════════════════════
真跑撞到的（2026-09-29，未计费）
════════════════════════════════════════════════════════════════════════
请求终于通过所有参数校验之后，平台在内容审核这一步拒了：

    InputImageSensitiveContentDetected.PrivacyInformation
      "the request failed because the input image 'content[1]'
       may contain real person"

    OutputVideoSensitiveContentDetected.PolicyViolation
      "the output video may be related to copyright restrictions"

这两条**不是参数错、也不是代码错** —— 是那张图（写实人脸）过不了平台的
隐私策略。用户能做的是"换一张更风格化的图"，但：

  · 他现在**看不到**"是哪张图、为什么被拒"（错误只在日志/一瞬的提示里）；
  · 换风格要花钱（重新出图），而且**换了未必过**。

所以这个模块做两件事：

① **把"这张图被拒过"记下来**（按图片路径），下次出片**提前**在卡片上说清：
   "这张定妆照上次被平台判为像真人 —— 建议换风格重出，或先只带场景图"。
   存 cache 下的 JSON（与 `tiers.note_draft_rejected` 同一套思路：
   环境事实、不进 DB、用户可一键删掉重测）。

② **免费用本地 ffmpeg 做一版风格化**（不用花出图的钱）：
   海报化 + 轻微降饱和/降细节，让"写实人脸"看起来更像插画。
   ⚠ 它是**给用户挑的备选**，不自动替换 —— 而且**换了之后下一次出片
   可能会真的计费**（因为可能过审核），这一点必须写明。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.imagepolicy")

#: 平台那两类"内容策略"错误的关键字（小写匹配）
_POLICY_KEYS = {
    "image_privacy": ("inputimagesensitivecontent", "may contain real person",
                      "privacyinformation"),
    "output_policy": ("outputvideosensitivecontent", "copyright restrictions",
                      "policyviolation"),
}


def classify_error(err: Any) -> str:
    r"""这条平台报错属于哪一类内容策略？返回 "" / "image_privacy" / "output_policy"。"""
    e = str(err or "").lower()
    if not e:
        return ""
    for kind, keys in _POLICY_KEYS.items():
        if any(k in e for k in keys):
            return kind
    return ""


def _store_path() -> str:
    root = os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()
    return os.path.join(root, "cache", "image_policy.json")


def _load() -> Dict[str, Any]:
    try:
        with open(_store_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d: Dict[str, Any]) -> None:
    p = _store_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        logger.warning("记内容策略失败（不影响出片）：%s", e)


def key_of(path: str) -> str:
    """用**文件内容**做键（而不是路径）—— 重出图会换路径，内容才认得出。"""
    p = str(path or "")
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            h.update(f.read(1 << 20))
        return h.hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return "path:" + os.path.basename(p)


def note_rejected(path: str, err: Any, *, kind: str = "") -> None:
    """记一次"这张图/这条输出被平台内容策略拒了"（由适配器自愈路径调用）。"""
    k = str(kind or classify_error(err) or "unknown")
    key = key_of(path)
    if not key or key == "path:":
        return
    d = _load()
    ent = d.get(key) or {}
    ent["path"] = str(path)
    ent["kind"] = k
    ent["count"] = int(ent.get("count") or 0) + 1
    ent["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ent["evidence"] = str(err)[:400]
    d[key] = ent
    _save(d)
    logger.info("记下：参考图 %s 被平台内容策略拒过（%s，第 %s 次）",
                os.path.basename(str(path)), k, ent["count"])


def rejected_info(path: str) -> Dict[str, Any]:
    """这张图被拒过吗？返回记录（没有就空 dict）。"""
    return (_load().get(key_of(path)) or {})


def rejection_summary(paths: List[str]) -> Dict[str, Any]:
    """这一镜要用的参考图里，哪些被拒过？（供卡片提前警告）"""
    hit = []
    for p in (paths or []):
        info = rejected_info(p)
        if info:
            hit.append({"path": p, "filename": os.path.basename(str(p)),
                        "kind": info.get("kind"), "count": info.get("count"),
                        "at": info.get("at")})
    return {"rejected": hit, "count": len(hit),
            "warning": ("" if not hit else
                        "⚠ 这一镜要用的参考图里有 %d 张**被平台判过内容风险**"
                        "（多半是「像真人」）：%s。直接出片很可能再次被拒 —— "
                        "建议先用「🖌 风格化」出一版非写实的换上去，"
                        "或先只带场景图。"
                        % (len(hit), "、".join(h["filename"] for h in hit[:3])))}


def stylize(src: str, dst_dir: str = "") -> Dict[str, Any]:
    r"""**免费用本地 ffmpeg** 把一张写实图做风格化（海报化+降细节）。

    为什么这么做：平台的拒收理由是"**像真人**"。把它做得更像插画
    （减少照片级细节、色阶化）**有可能**绕过那条隐私判定，
    而且**本地跑、不花钱** —— 比"重新出一张图"便宜得多，也快。

    ⚠ 两点如实说：
      · 这只是**备选**，不保证过审；
      · 换上去之后**下一次出片可能会真的计费**（因为可能通过审核、真的出片）。
    """
    s = str(src or "")
    if not s or not os.path.exists(s):
        return {"ok": False, "error": "源图不存在"}
    out_dir = dst_dir or os.path.dirname(s)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    base = os.path.splitext(os.path.basename(s))[0]
    out = os.path.join(out_dir, "%s_stylized.jpg" % base)
    try:
        from core.ffmpeg_manager import get_ffmpeg_for_postprocess as _ff
        exe = _ff()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"找不到 ffmpeg：{e}"}
    #: 风格化滤镜链。**只用这个 build 里真的能用的**：
    #:   实测本仓自带的 ffmpeg（ffmpeg-win-x86_64-v7.1）里 `posterize` / `gblur`
    #:   虽然出现在 `-filters` 列表里，但**用起来报 "Filter not found"**；
    #:   `pp=...` 报 "Option not found"。可用的组合是 scale / eq / smartblur / hue。
    #:   目标是"少一点照片级细节、色阶更像插画"，不是专业滤镜。
    chains = [
        "scale=1024:-1,smartblur=lr=1.5:ls=-0.8,eq=saturation=0.8:contrast=1.1",
        "scale=1024:-1,eq=saturation=0.8:contrast=1.1",   # 兜底：只要降饱和+提对比
        "scale=1024:-1",                                   # 最后兜底：至少降分辨率细节
    ]
    last_err = ""
    for _i, vf in enumerate(chains):
        cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", s,
               "-vf", vf, "-q:v", "3", out]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=180)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"ffmpeg 执行失败：{e}"}
        if p.returncode == 0 and os.path.exists(out):
            return {"ok": True, "path": out,
                    "bytes": os.path.getsize(out),
                    "filter": vf,
                    # ★ 如实说：我看过处理前后的图 —— 本地滤镜只能让它"柔一点/暗一点"，
                    #   **仍然是一张写实人脸**，平台的"像真人"判定**很可能照样命中**。
                    #   所以这是"免费先试一下"，不是解决方案。
                    "note": ("已经生成一版风格化的图（本地免费，几秒钟）。**先把话说清楚**：\n"
                             "· 我对比过处理前后 —— 它只是柔一点、暗一点，"
                             "**仍然是一张写实人脸**，平台的「像真人」判定"
                             "**很大概率照样命中**；\n"
                             "· 真正可靠的办法是**换风格重出一张定妆照**"
                             "（资产页「🎨 AI 生成形象」，风格选动漫/插画/水墨之类非写实）；\n"
                             "· 换上这张之后再出片，**下一次可能会真的计费**"
                             "（因为可能真的通过审核、真的出片）。"),
                    "how_to_use": "在资产页对该角色点「上传参考图」，"
                                  "选这张 *_stylized.jpg 即可替换。"}
        last_err = (p.stderr or b"").decode("utf-8", "replace")[-200:]
    return {"ok": False, "error": f"风格化失败（每条滤镜链都试过）：{last_err}"}
