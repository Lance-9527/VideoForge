"""
VideoForge · 白膜参考注入

════════════════════════════════════════════════════════════════════════
要解决的问题
════════════════════════════════════════════════════════════════════════
白膜（previz）流程原本是**断的**：

    render_previz  →  渲出白膜、存进 shots.previz_path   ← 做到了
    review_previz  →  用户确认、状态推进                  ← 做到了
    generate_*     →  真去生成视频…                        ← **完全没用到白膜**

`core/orchestrator.py` 和 `core/longvideo.py` 里**一个 previz 都没提**。
也就是说：白膜渲了、用户确认了、然后生成时那张白膜被丢在硬盘上没人看。
用户看到的"白膜流程"其实只是走个过场 —— 生成结果跟没有白膜**一模一样**。

这个模块负责最后一公里：把白膜真正作为**控制信号**送进生成请求。

════════════════════════════════════════════════════════════════════════
为什么白膜是有用的（而不是多此一举）
════════════════════════════════════════════════════════════════════════
官方把白膜列为一等参考类型（Seedance 2.5 的 `@Clay Render 1`），
它补的是**首帧图补不上**的那一块：

    只给首帧图 → 模型知道"从哪开始"，但**不知道接下来怎么动**，运镜自己编。
    加白膜视频 → 空间关系 / 运镜路径 / 时间节奏 全被约束住。

官方原话大意：用无纹理 3D 模型建立场景的空间结构、人物姿态、运动路径与机位。

════════════════════════════════════════════════════════════════════════
诚实的边界（别把白膜当万能）
════════════════════════════════════════════════════════════════════════
· 官方承认的弱项：复杂运动物理（弹跳/抛物）、多主体互动稳定性、
  数量一致性（"10 只小狗"做不到）→ 分镜设计绕开，别指望白膜解决。
· 也有创作者反馈白膜当视频参考"**很容易被带偏**"，只把它当**运镜可行性验证**用。
  → 所以本模块只做"能带就带上"，并且**明确告诉用户当前这一档带没带**，
    不假装带上了就一定更好。
· **不是所有平台都吃白膜**。各平台对"参考视频"的支持差别很大，
  所以这里按能力判断，不支持就如实说明，**不静默丢弃**。

════════════════════════════════════════════════════════════════════════
一个必须注意的互斥关系
════════════════════════════════════════════════════════════════════════
`first_frame / last_frame` 与 `reference_image / reference_video` 在多数平台是
**互斥场景**（Seedance 官方把 first_frame / last_frame / reference_image 列为
三种互斥场景；可灵、万相、Runway 也有同类限制）。

所以本模块给出的 `mode` 要分三种，而不是"全塞进去"：

    first_frame  只给首帧（最保守，所有模型都支持）
    reference    只给参考素材（角色图 + 场景图 + 白膜视频）
    hybrid       首帧 + 参考图（**只有明确声明支持的模型才走这条**）
"""

from __future__ import annotations
import re

import json
import logging
import os
import hashlib
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.previz_ref")


def reference_quality_preflight(shot: Dict[str, Any]) -> Dict[str, Any]:
    """Fail closed on missing, damaged, failed, or unrelated whitebox evidence.

    A successful render alone is not a content check. This reads the existing
    per-shot framing/consistency receipts, or a managed studio job manifest,
    without synthesizing media, changing state, uploading, or spending money.
    """
    errors: List[str] = []
    evidence: List[Dict[str, Any]] = []
    raw_path = str((shot or {}).get("previz_path") or "").strip()
    if not raw_path:
        return {"ok": True, "not_applicable": True, "errors": [], "evidence": []}
    path = Path(raw_path).resolve()
    if not path.is_file():
        return {"ok": False, "errors": ["白膜文件不存在"], "evidence": []}
    video = white_model_video(shot)
    if not video:
        return {"ok": False, "errors": ["白膜只有静帧或没有配套视频，不能证明固定轨迹"], "evidence": []}
    if "_proxy" in Path(video).stem.lower() or "_proxy" in path.stem.lower():
        return {"ok": False, "errors": ["单帧代理视频不能作为固定轨迹白膜"], "evidence": []}
    if str((shot or {}).get("previz_status") or "").lower() in ("failed", "cancelled", "error"):
        errors.append("白膜状态为失败或取消")

    def read_receipt(receipt: Path) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(receipt.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("报告不是对象")
            return data
        except (OSError, UnicodeError, ValueError):
            errors.append(f"白膜核验报告损坏或无法读取：{receipt.name}")
            return None

    def current_renderer_hash(relative: str) -> str:
        try:
            return hashlib.sha256((Path(__file__).resolve().parents[1] / relative).read_bytes()).hexdigest()
        except OSError:
            errors.append("当前白膜核验渲染器不存在，无法关联报告版本")
            return ""

    folder = path.parent
    job_folder = folder.parent if folder.name == "frames" else folder
    manifest_path = job_folder / "manifest.json"
    if job_folder.name.startswith("job_") or manifest_path.exists():
        manifest = read_receipt(manifest_path)
        if manifest is not None:
            validation = manifest.get("validation") or {}
            evidence.append({"kind": "studio_manifest", "path": str(manifest_path),
                             "job_id": manifest.get("job_id"), "validation": validation})
            if (manifest.get("schema") != "videoforge.render/1" or manifest.get("job_id") != job_folder.name
                    or not isinstance(validation, dict) or validation.get("passed") is not True):
                errors.append("工作台任务manifest未通过校验或与白膜所属任务不符")
            expected_renderer = current_renderer_hash("data/studio_bridge/render_scene.py")
            if not expected_renderer or manifest.get("renderer_sha256") != expected_renderer:
                errors.append("工作台白膜渲染器版本与当前不符，请重新渲染核验")
            outputs = manifest.get("outputs") or []
            if not isinstance(outputs, list):
                outputs = []
                errors.append("工作台manifest输出列表损坏")
            for expected_kind, expected_path in (("video", Path(video).resolve()),
                                                  ("first_frame", path if path.suffix.lower() not in (".mp4", ".mov", ".webm") else job_folder / "frames/frame_000000.png")):
                matches = [item for item in outputs if isinstance(item, dict) and item.get("kind") == expected_kind]
                if len(matches) != 1:
                    errors.append(f"工作台manifest缺少唯一{expected_kind}输出")
                    continue
                item = matches[0]
                try:
                    actual_path = Path(str(item.get("path") or "")).resolve()
                    if actual_path != expected_path.resolve() or not actual_path.is_relative_to(job_folder):
                        raise ValueError("输出不属于这个任务")
                    if not actual_path.is_file() or hashlib.sha256(actual_path.read_bytes()).hexdigest() != item.get("sha256"):
                        raise ValueError("输出hash不符")
                except (OSError, ValueError):
                    errors.append(f"工作台{expected_kind}输出路径或hash与当前白膜不一致")
        return {"ok": not errors, "errors": errors, "evidence": evidence}

    project_folder = folder.parent if re.fullmatch(r"镜\d+", folder.name) else folder
    try:
        index = int(shot.get("order_index") or 0) + 1
        if index <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        return {"ok": False, "errors": ["分镜序号无效，不能关联白膜核验报告"], "evidence": []}

    def inspect_shots(names: tuple[str, ...], kind: str, allowed: tuple[str, ...]) -> None:
        found = [project_folder / name for name in names if (project_folder / name).is_file()]
        if not found:
            errors.append(f"缺少{names[0]}，不能把渲染完成当作内容通过")
            return
        for receipt in found:
            data = read_receipt(receipt)
            if data is None:
                continue
            expected_renderer = current_renderer_hash("data/previz_blender/render_spec.py")
            if (data.get("validator_version") != 2 or not expected_renderer
                    or data.get("renderer_sha256") != expected_renderer):
                errors.append(f"{receipt.name}缺少当前版本内容核验，旧白膜需重新渲染")
            rows = data.get("shots")
            if not isinstance(rows, list):
                errors.append(f"{receipt.name}缺少有效shots列表")
                continue
            selected = [row for row in rows if isinstance(row, dict) and str(row.get("index")) == str(index)]
            if len(selected) != 1:
                errors.append(f"{receipt.name}缺少镜{index:02d}唯一核验记录")
                continue
            row = selected[0]
            verdict = str(row.get("verdict") or "").lower()
            evidence.append({"kind": kind, "path": str(receipt), "index": index, "verdict": verdict})
            if verdict not in allowed:
                errors.append(f"镜{index:02d}{kind}未通过：{verdict or 'missing'}")
            nested = row.get("consistency")
            if nested is not None and not isinstance(nested, dict):
                errors.append(f"镜{index:02d}{kind}嵌套一致性报告损坏")
                nested = {}
            checks = row.get("checks") if kind == "consistency" else (nested or {}).get("checks")
            if kind == "consistency" and (not isinstance(checks, list) or not checks):
                errors.append(f"镜{index:02d}一致性报告没有检查项")
            if isinstance(checks, list) and any(not isinstance(check, dict) or check.get("ok") is not True for check in checks):
                errors.append(f"镜{index:02d}{kind}含失败或未知检查项")

    inspect_shots(("取景报告.json", "framing.json"), "framing", ("ok", "fixed"))
    inspect_shots(("白膜一致性.json", "consistency.json"), "consistency", ("consistent", "adjusted"))
    geometry_path = (folder / path.stem / "geometry-validation.json"
                     if path.suffix.lower() in (".mp4", ".mov", ".webm") else folder / "geometry-validation.json")
    if geometry_path.exists():
        geometry = read_receipt(geometry_path)
        if geometry is not None:
            evidence.append({"kind": "geometry", "path": str(geometry_path), "passed": geometry.get("passed")})
            if geometry.get("passed") is not True:
                errors.append("白膜道具支撑/接触几何检查未通过")
    return {"ok": not errors, "errors": errors, "evidence": evidence}


def require_reference_quality(shot: Dict[str, Any], provider: str, model_name: str,
                              settings: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Require real whitebox evidence for the enabled promo reference pipeline.

    Explicitly disabled reference mode and other providers return None. Missing
    whitebox or failed evidence raises before consumers make any paid request.
    """
    if str(provider or "").lower() != "seedance_t6" or str(model_name or "").lower() != "seedance-2-0-promo":
        return None
    enabled = (settings or {}).get("previz_as_reference")
    if enabled is False or str(enabled).strip().lower() in ("0", "false", "off", "no"):
        return None
    if not str((shot or {}).get("previz_path") or "").strip():
        raise ValueError("白膜参考管线缺少白膜，未提交付费任务；请先渲染并通过核验")
    result = reference_quality_preflight(shot)
    if not result["ok"]:
        raise ValueError("白膜质量预检未通过，未提交付费任务：" + "；".join(result["errors"]))
    return result

# 支持把**视频**当参考素材的模型（白名单，宁可少开不可乱开）。
# 依据：
#   · Seedance 2.5 —— 官方把 clay render 列为一等参考类型，`@Clay Render 1`
#   · 可灵 3.0     —— 官方能力表里有 Video Reference
#   · 万相 2.7     —— `input.media[]` 里 `type: reference_video`
# 其它模型（Runway gen4.5 只有首帧、Sora 什么都没有、Luma/Pika/CogVideoX 未核实）
# 一律**不发**，避免发出去被平台拒或静默忽略。
REFERENCE_VIDEO_PROVIDERS = {"seedance", "jimeng", "kling", "wanx", "seedance_t6"}

#: ★ 2026-10-22：token6688 网关（seedance_t6）**也吃白膜视频**。
#
#   实测记录（这轮真机）：
#     · 网关的请求格式与火山方舟一致 —— `content[]` 里放
#       `{"type":"video_url","video_url":{"url":...},"role":"reference_video"}`，
#       并且 **reference_video 必须是可以下载的 web URL**
#       （base64 data URI 会让网关在 build_body 阶段失败：
#        `{'code':'build_body','message':'生成失败，请稍后重试'}`）。
#     · 参考视频在模型请求中须为 web URL。当前精确 promo 型号由
#       adapter 上传到该中转站 /v1/files，不再要求公网隧道或 data URI。
#     · 它原来根本不在白名单里 ⇒ 白膜一次都没被送出去过。

# 明确支持"首帧 + 参考图"同时存在的模型。
# 默认一律认为**互斥**（发两个会被平台拒，或静默只认一个 —— 后者更糟）。
HYBRID_PROVIDERS: set = set()


@dataclass
class PrevizRef:
    """白膜注入的决策结果。带 `reason` 是为了让每一档都能向用户解释清楚。"""

    usable: bool = False
    mode: str = "first_frame"          # first_frame | reference | hybrid
    video_ref: str = ""                # 白膜视频路径（本地）
    prompt_note: str = ""              # 追加到 prompt 的说明
    reason: str = ""                   # 给人看的一句话
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "usable": self.usable, "mode": self.mode,
            "video_ref": self.video_ref, "reason": self.reason,
            "prompt_note": self.prompt_note, "warnings": self.warnings,
        }


def white_model_video(shot: Dict[str, Any]) -> str:
    """取这个分镜的**白膜视频**（不是帧图）。返回 "" = 只有帧、没有视频。

    ★ 2026-10-22 修：`shot.previz_path` 存的是**首帧 PNG**（界面要用它显示静帧），
      但平台侧「白模参考」只吃**视频**。原来直接把这张 PNG 当参考视频发出去，
      平台要么拒、要么静默忽略 —— 用户以为白膜生效了，其实没有。
      现在的约定：`<输出>/<项目>/previz/镜NN.mp4` 与 `…/镜NN/f0001.png` 同目录，
      由 `core/previz_blender._encode_videos()` 在渲染后自动编码。
    """
    p = str(shot.get("previz_path") or "")
    if not p:
        return ""
    if p.lower().endswith((".mp4", ".mov", ".webm")):
        return p if os.path.exists(p) else ""
    d = os.path.dirname(p)                        # …/previz/镜01
    base = os.path.basename(d.rstrip("/\\"))
    cand = d + ".mp4"                             # …/previz/镜01.mp4
    if os.path.exists(cand):
        return cand
    if base.startswith("镜"):
        alt = os.path.join(os.path.dirname(d), base + ".mp4")
        if os.path.exists(alt):
            return alt
    return ""


def _frames_dir_of(png: str) -> str:
    """首帧 PNG 所在的帧目录（…/previz/镜01/f0001.png → …/previz/镜01）。"""
    d = os.path.dirname(png)
    # 兼容 previz_path 直接指向 f0001.png 的情况
    return d


def ensure_white_model_video(shot: Dict[str, Any], *, fps: int = 12) -> Dict[str, Any]:
    """**任何引擎**的白膜都能拿到 mp4（懒生成，幂等）。

    为什么必须有它（2026-10-24，用户：「彻底解决白膜→480p」）：
      `encode_previz_videos()` 只在 `previz_blender` 那条路被调用（previz_blender.py:374），
      其它引擎（agent_tools_production 的 previz 生成）只写首帧 PNG。
      于是这些项目的 `white_model_video()` 恒为 ""，480p 的"白模参考"永远发不出去 ——
      用户看到的就是"480p 一点都不参考白膜"。

    返回 {"path": mp4 或 "", "mode": "frames"|"proxy"|"existing"|"none", "note": 说明}
    """
    out = {"path": "", "mode": "none", "note": ""}
    p = str((shot or {}).get("previz_path") or "").strip()
    if not p or not os.path.exists(p):
        out["note"] = "这个分镜还没有白膜（先渲一版白膜）"
        return out

    # ① 已经是视频
    if p.lower().endswith((".mp4", ".mov", ".webm")):
        out.update(path=p, mode="existing", note="白膜视频已存在")
        return out

    # ② 已有配套 mp4（Blender 路径产的）
    got = white_model_video(shot)
    if got:
        out.update(path=got, mode="existing", note="白膜视频已存在")
        return out

    # ③ 帧目录 → 用现成编码器编 mp4
    frames_dir = _frames_dir_of(p)
    try:
        siblings = [f for f in os.listdir(frames_dir)
                    if f.lower().endswith((".png", ".jpg", ".jpeg"))]
    except Exception:  # noqa: BLE001
        siblings = []
    target = None
    base = os.path.basename(frames_dir.rstrip("/\\"))
    if len(siblings) >= 3:
        # 命名约定：…/previz/镜01.mp4（与 white_model_video() 的查找规则一致）
        if base.startswith("镜"):
            target = os.path.join(os.path.dirname(frames_dir), base + ".mp4")
        else:
            target = frames_dir.rstrip("/\\") + ".mp4"
        # ★ 直接用 ffmpeg image2 编码：不依赖任何引擎的编码器与命名约定，
        #   只要同目录有一串同模式的图就能编成视频（这才是"任何引擎都吃得下"）。
        try:
            import subprocess
            numbered = sorted(f for f in siblings if re.search(r"\d+", f))
            if numbered:
                first = numbered[0]
                m = re.search(r"(\d+)(?!.*\d)", first)
                digits = len(m.group(1)) if m else 4
                start_no = int(m.group(1)) if m else 1
                pattern = first[:m.start()] + ("%" + f"0{digits}d") + first[m.end():] \
                    if m else first
                ff = os.environ.get("VIDEOFORGE_FFMPEG") or "ffmpeg"
                cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
                       "-framerate", str(int(fps or 12)), "-start_number", str(start_no),
                       "-i", os.path.join(frames_dir, pattern),
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                       "-pix_fmt", "yuv420p", "-movflags", "+faststart", target]
                subprocess.run(cmd, check=True, timeout=300,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(target) and os.path.getsize(target) > 10240:
                    out.update(path=target, mode="frames",
                               note=f"由 {len(numbered)} 帧序列编码成白膜视频（{fps}fps）")
                    return out
        except Exception as e:  # noqa: BLE001
            logger.warning("白膜帧序列用 ffmpeg 编码失败，改用代理视频：%s", e)

    # ④ 只有一张静帧 → 造一个**代理视频**（极轻推近 1.6s），至少能提供构图与机位
    if not p.lower().endswith((".png", ".jpg", ".jpeg")):
        out["note"] = "白膜文件类型不认识，无法做参考视频"
        return out
    proxy = os.path.splitext(p)[0] + "_proxy.mp4"
    if os.path.exists(proxy) and os.path.getsize(proxy) > 2048:
        out.update(path=proxy, mode="proxy", note="代理视频已存在（白膜只有静帧）")
        return out
    try:
        import subprocess
        ff = os.environ.get("VIDEOFORGE_FFMPEG") or "ffmpeg"
        dur, fps2 = 1.6, 24
        vf = (f"scale=iw*1.06:ih*1.06,zoompan=z='min(zoom+0.0009,1.06)':"
              f"d={int(dur * fps2)}:s=1280x720:fps={fps2},format=yuv420p")
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-i", p,
               "-vf", vf, "-t", str(dur), "-r", str(fps2),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", proxy]
        subprocess.run(cmd, check=True, timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(proxy) and os.path.getsize(proxy) > 2048:
            out.update(path=proxy, mode="proxy",
                       note="白膜只有静帧，已生成 1.6 秒代理视频（提供构图/机位，不提供运动轨迹）")
            return out
    except Exception as e:  # noqa: BLE001
        logger.warning("白膜代理视频生成失败：%s", e)
        out["note"] = f"代理视频生成失败：{e}"
        return out

    out["note"] = "白膜既没有帧序列也没有可用的静帧"
    return out


def supports_reference_video_model(provider: str, model_name: str) -> bool:
    """Use specific provider/model capabilities, not a family-wide historical assumption."""
    prov = str(provider or "").lower()
    model = str(model_name or "").lower()
    if prov == "seedance_t6" and model == "seedance-2-0-promo":
        # Relay public logical-model schema, checked 2026-10-25: reference video is supported.
        return True
    if "seedance" in prov or "seedance" in model or "doubao" in model:
        # 2.5 / 2-5 才支持参考视频；2-0 / 2.0 / 1-x 不支持
        return ("2-5" in model) or ("2.5" in model)
    return prov in REFERENCE_VIDEO_PROVIDERS


def white_model_last_frame(shot: Dict[str, Any], *, cache_dir: str = "",
                             at_seconds: float = 0.0) -> str:
    """从白膜视频抽"这一镜结束时刻"的那一帧（带缓存），给首尾帧模式当 last_frame。

    at_seconds：这一镜要几秒。例如镜头 5s，就取白膜的 4.85s 帧作为终点构图，
    这样"首帧 0s → 尾帧 5s"跟白膜这一段完全对齐；不传则取片尾。
    """
    vid = white_model_video(shot)
    if not vid or not os.path.exists(vid):
        return ""
    try:
        import hashlib
        import subprocess
        key = hashlib.md5(
            f"{vid}|{os.path.getmtime(vid):.0f}|{at_seconds:.2f}".encode()).hexdigest()[:12]
        root = cache_dir or os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
        dst_dir = os.path.join(root, "previz_lastframe")
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, f"{key}.png")
        if os.path.exists(dst) and os.path.getsize(dst) > 2048:
            return dst
        ff = os.environ.get("VIDEOFORGE_FFMPEG") or "ffmpeg"
        if at_seconds and at_seconds > 0.2:
            seek = ["-ss", f"{max(0.0, float(at_seconds) - 0.15):.3f}"]
        else:
            seek = ["-sseof", "-0.15"]
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", *seek,
                        "-i", vid, "-frames:v", "1", dst],
                       check=True, timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return dst if os.path.exists(dst) and os.path.getsize(dst) > 2048 else ""
    except Exception as e:  # noqa: BLE001
        logger.warning("抽白膜末帧失败：%s", e)
        return ""
def consistency_for_shot(shot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """读这一镜的「白膜 ↔ 分镜提示词」一致性记录（没有就 None）。

    ★ 2026-10-23（用户）：「得确认白膜视频以后都跟分镜提示词要求一致！」
      render_spec.py 逐镜对账后落在 `<previz>/白膜一致性.json`。
    """
    p = str((shot or {}).get("previz_path") or "")
    if not p:
        return None
    d = os.path.dirname(p)
    if os.path.basename(d.rstrip("/\\")).startswith("镜"):
        d = os.path.dirname(d)
    fp = os.path.join(d, "白膜一致性.json")
    if not os.path.isfile(fp):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return None
    try:
        idx = int((shot or {}).get("order_index") or 0) + 1
    except (TypeError, ValueError):
        idx = 1
    for r in (data.get("shots") or []):
        try:
            if int(r.get("index") or 0) == idx:
                return r
        except (TypeError, ValueError):
            continue
    return None


def decide(shot: Dict[str, Any], provider: str, *, enabled: bool = True,
           settings: Optional[Dict[str, Any]] = None,
           publish: bool = True) -> PrevizRef:
    """判断这个分镜该不该带白膜、以什么方式带。

    默认是纯函数（不碰数据库、不碰网络）。只有 `publish=True` 且确实
    需要发布时，才会尝试把白膜变成公网 URL（见 `core/publish.py` 的说明：
    参考**视频**不能走 base64，必须是公网 URL）。
    """
    if not enabled:
        return PrevizRef(reason="用户关掉了白膜参考（本次按普通方式生成）")

    path = shot.get("previz_path") or ""
    status = shot.get("previz_status") or "none"

    if not path:
        return PrevizRef(reason="这个分镜还没渲白膜 —— 渲一版可以少抽很多次卡（白膜是免费本地的）")
    if not os.path.exists(path):
        return PrevizRef(reason=f"白膜文件不在了（{path}），需要重渲")

    # 只有"用户已确认"的白膜才该被用。
    # ★ 这里**不能**把 `draft_ready` 算进来 —— 它正是"渲好了、还没给人看过"
    #   的那个状态。把没确认的白膜拿去约束成片，等于把用户没点头的东西
    #   固化进最终结果里，而且用户完全不知道。
    #   （早先误把 draft_ready 列进白名单，自测的"渲了但没确认"用例抓到了。）
    # ★ 2026-10-23（用户：「就不能让 480p 按白膜视频做渲染吗」+「做好了就设置好」）：
    #   原来**必须**白膜 approved 才会被使用。但用户要的是"白膜直接约束出片"——
    #   加一个显式开关 `previz_require_approval`（默认 True，保持原安全语义）；
    #   关掉时 draft_ready 也能用，并在 reason 里如实说"这次用的是未确认的白膜"。
    # ★★ 2026-10-24：默认**允许使用未确认的白膜**（用户反复要求"白膜要能当参考视频"）。
    #   原来默认 True（必须 draft_approved/final_ready）→ 渲了白膜但没点确认就整条被拒，
    #   用户看到的是"480p 一点都不参考白膜"。要严格语义就设 previz_require_approval=true。
    _req_approve = False
    try:
        _raw_approve = (settings or {}).get("previz_require_approval")
        if _raw_approve is not None:
            _req_approve = str(_raw_approve).strip().lower() in ("1", "true", "on", "yes")
    except Exception:  # noqa: BLE001
        _req_approve = False
    _ok_states = ("draft_approved", "final_ready")
    if not _req_approve:
        _ok_states = ("draft_ready",) + _ok_states
    # ★ 2026-10-24：不要求确认时（默认），**不再用状态白名单卡它** ——
    #   老项目/非 Blender 引擎的 previz_status 常常是空串，按白名单会被整条拒掉，
    #   那正是"480p 一点都不参考白膜"的原因之一。
    if _req_approve and status not in _ok_states:
        return PrevizRef(
            reason=(f"白膜状态是「{status}」，还没得到你的确认 —— "
                    f"先在对话里看一眼帧图，确认之后它才会被用来约束成片"),
            warnings=["未确认的白膜不会被使用（避免把没看过的东西固化进成片）"],
        )

    # ★★ 2026-10-23（用户）：「得确认白膜视频以后都跟分镜提示词要求一致！」
    #   渲染器逐镜对账落盘了 白膜一致性.json；**不一致的白膜不许当参考素材**，
    #   否则等于把"跟提示词打架的机位"固化进成片。
    _cons = consistency_for_shot(shot)
    if isinstance(_cons, dict) and _cons.get("verdict") == "mismatch":
        _bad = [c for c in (_cons.get("checks") or []) if not c.get("ok")]
        return PrevizRef(
            usable=False,
            reason=("白膜与分镜提示词**不一致**（%s）—— 先重渲白膜或改提示词，"
                    "不能拿它约束成片" % "、".join(str(c.get("name")) for c in _bad)),
            warnings=["白膜与提示词不一致：%s" % "；".join(
                "%s 要求 %s / 实际 %s" % (c.get("name"), c.get("want"), c.get("got"))
                for c in _bad)],
        )

    prov = (provider or "").lower()
    selected_model = str(shot.get("model_name") or (settings or {}).get("default_model_name") or "")
    supports_video_ref = (supports_reference_video_model(prov, selected_model)
                          if selected_model else prov in REFERENCE_VIDEO_PROVIDERS)
    hybrid_ok = prov in HYBRID_PROVIDERS

    if supports_video_ref:
        mode = "hybrid" if hybrid_ok else "reference"
        # ★ 白模参考必须是**视频**；只有帧图就不能发（见 white_model_video 说明）。
        _vid = white_model_video(shot)
        if not _vid:
            # ★ 2026-10-24（用户：「彻底解决白膜→480p」）：
            #   非 Blender 引擎只写首帧 PNG，这里**懒生成**白膜视频（帧序列编码 / 静帧代理），
            #   否则这条链在这些项目上永远不会生效。
            try:
                _ens = ensure_white_model_video(shot)
                if _ens.get("path"):
                    _vid = _ens["path"]
                    logger.info("白膜视频是本次临时生成的：%s", _ens.get("note") or "")
            except Exception as _e:  # noqa: BLE001
                logger.warning("白膜视频懒生成失败：%s", _e)
        if not _vid:
            return PrevizRef(
                usable=False, mode="first_frame",
                prompt_note=_degraded_note(shot),
                reason=("白膜只有帧图、没有视频 —— 平台的白模参考只吃视频，"
                        "这张帧图发不出去"),
                warnings=["重渲一次白膜会自动编码 mp4（免费）；"
                          "在那之前，白膜只能把运镜写进提示词，约束不了运动轨迹"],
            )
        note = (
            "【参考素材】已附带一版本地渲染的白模（clay render）预演视频，"
            "只提供空间结构、人物走位、运镜路径与时间节奏；"
            "主体造型、材质、光线、色彩按剧本和已有参考图执行；"
            "保留剧本明确指定的白色或灰色主体/布景，"
            "不要把调试网格、线框、坐标轴或占位标记带入成片。"
        )

        # ★★ 关键一步：参考视频**必须是公网 URL**。
        #   实测火山方舟：base64 → "reference_video must be provided as a web url"；
        #   本地路径 → "invalid url scheme"。图片能走 base64，视频不行。
        #   这台机器在 NAT 后面，所以默认拿不到公网地址 ——
        #   这时**必须如实说清楚**，绝不能让用户以为白膜生效了。
        video_ref = _vid
        pub_reason = ""
        if publish and prov != "seedance_t6":
            try:
                from core import publish as _pub
                r = _pub.publish_file(_vid, settings or {})
                if r.get("ok"):
                    video_ref = r["url"]
                    pub_reason = r.get("reason") or ""
                else:
                    return PrevizRef(
                        usable=False, mode="first_frame",
                        prompt_note=_degraded_note(shot),
                        reason=("白膜渲好了，但**发不出去**：" + (r.get("reason") or "")),
                        warnings=[r.get("how_to_fix") or "",
                                  "本次会退化成「把白膜定下的运镜写进提示词」，"
                                  "模型仍可能自己编运镜。"],
                    )
            except Exception as e:
                logger.warning("发布白膜失败：%s", e)
                video_ref = path

        return PrevizRef(
            usable=True, mode=mode, video_ref=video_ref, prompt_note=note,
            reason=(f"{prov} 支持视频参考 —— 白膜会作为运镜/节奏的控制信号一起发出去"
                    + (f"（{pub_reason}）" if pub_reason else "")),
            warnings=(["参考视频走的是临时公网隧道，生成完成后可以关掉"
                       "（设置里或重启即回收）"] if pub_reason.startswith("通过") else []),
        )

    # 不支持视频参考的模型：白膜**不能**发出去。
    # 但它的信息还有用（运镜/时长是用户已经确认过的），
    # 所以这里退化成"把白膜里定好的运镜写进 prompt"，并如实说明降级。
    return PrevizRef(
        usable=False,
        mode="first_frame",
        prompt_note=_degraded_note(shot),
        reason=(f"当前未登记{selected_model or prov}的视频参考能力，白膜未作为视频发送 —— "
                f"已退化为「把白膜定下的运镜写进提示词」"),
        warnings=["这一档白膜只能约束文字，约束不了运动轨迹（模型仍可能自己编运镜）"],
    )


def _degraded_note(shot: Dict[str, Any]) -> str:
    """降级路径：白膜发不出去，但至少把它定下的信息写进 prompt。

    ★ 第 70 轮补全（2026-09-27）：原来只写一句运镜名（「【镜头】镜头缓慢推近。」），
      **时长和走位全丢** —— 而用户要白膜正是为了"先确认要生成的动向"。
      白膜渲的时候会把 spec 存成 `<白膜>.spec.json`（见 `render_previz`），
      这里优先用它，按**与白膜同源**的方式翻译成提示词
      （`previz.spec_to_prompt` 就是为此写的：prompt 与白膜必须同源，
      分头演化会让 prompt 承诺一个白膜里根本没有的运镜）。
      sidecar 不存在（老项目）→ 退回"运镜 + 时长"这两条已知信息。
    """
    parts: List[str] = []
    spec: Dict[str, Any] = {}
    path = str(shot.get("previz_path") or "")
    if path:
        try:
            import json
            side = path + ".spec.json"
            if os.path.exists(side):
                with open(side, "r", encoding="utf-8") as f:
                    spec = json.load(f) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("读白膜 spec 失败（退回简单降级）：%s", e)
            spec = {}

    if spec:
        try:
            from core import previz as pv
            txt = pv.spec_to_prompt(spec)
            if txt:
                parts.append("【白膜预演定下的运镜/走位/节奏，按它来】" + txt.strip())
        except Exception as e:  # noqa: BLE001
            logger.warning("白膜 spec 转提示词失败（退回运镜名）：%s", e)

    if not parts:
        cam = ""
        try:
            from core import previz as pv
            cam = pv.LABELS_CAMERA.get(shot.get("previz_camera") or "", "")
        except Exception:  # noqa: BLE001
            cam = ""
        dur = shot.get("duration_seconds")
        bits = []
        if cam:
            bits.append(cam)
        if dur:
            bits.append(f"全长约 {float(dur):g} 秒")
        if bits:
            parts.append("【镜头】" + "，".join(bits) + "。")
    # 不加前导换行：拼接由 `apply_to_request` 统一负责（它会在中间插 "\n\n"）
    return "\n".join(parts)


def apply_to_request(req, ref: PrevizRef) -> Dict[str, Any]:
    """把白膜决策落到 VideoGenRequest 上。返回实际做了什么（给日志/前端用）。"""
    did: Dict[str, Any] = {"added_video_ref": False, "added_prompt_note": False}

    if ref.prompt_note:
        base = req.prompt or ""
        if ref.prompt_note not in base:
            req.prompt = (base + "\n\n" + ref.prompt_note).strip()
            did["added_prompt_note"] = True

    if ref.usable and ref.video_ref:
        # ★★ 2026-10-23 修：**白膜视频必须走 `req.video_url`，不能塞进
        #   `reference_images`**。
        #
        #   原来这里把白膜 append 进 `req.reference_images`（"参考图"列表）——
        #   而适配器里 `reference_images` 是**图片**通道
        #   （`content[].type == "image_url"`），`video_url` 才是视频通道
        #   （`content[].type == "video_url"`，还要 `mode=reference`）。
        #   于是白膜视频被当成一张"图片"发出去：
        #       content: [text, image_url(角色图), image_url(场景图),
        #                image_url(← 其实是 mp4 的 URL)]
        #   平台收到"图片字段里塞了个 mp4" ⇒ 参数非法
        #   ⇒ 网关回 `invalid_request`，白膜参考**从来没真正生效过**。
        #
        #   现在：白膜 → `req.video_url`（适配器会转成 content 里的
        #   video_url + role=reference_video，并自动带上 mode=reference）。
        req.video_url = ref.video_ref
        did["added_video_ref"] = True

        # ★ 互斥处理：这一档要吃参考素材，就不能同时给首帧 —
        #   否则平台会拒（或更糟：静默只认一个，而用户以为两个都生效了）。
        if ref.mode == "reference" and req.first_frame:
            did["dropped_first_frame"] = True
            req.first_frame = None
        if ref.mode == "reference" and req.last_frame:
            did["dropped_last_frame"] = True
            req.last_frame = None

    return did
