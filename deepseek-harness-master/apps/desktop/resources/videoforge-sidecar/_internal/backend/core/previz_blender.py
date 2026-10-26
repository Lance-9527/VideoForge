# -*- coding: utf-8 -*-
"""
VideoForge 白膜预演入口 —— Blender 3D 版
========================================

★ 为什么换掉原来的 previz（2026-09-29）
────────────────────────────────────────
原来的两档都不满足「渲染视频前的 3D 预演」这个要求，实测都出过硬伤：

  A 档（previz.py，ffmpeg 平面色块）
    · 体块只有矩形，**画不出「伏地的人」** —— 剧本写「陈砚俯卧泥水中」，
      渲出来是站着的方块。
    · 没有厚度、**没有遮挡关系**（谁挡谁），也没有透视。
    · `y` 是方块顶部坐标，调用方按「中心」给值就整体错位 → 人物埋进地面。

  C 档（previz_multi.py，针孔透视 + 画家算法）
    · 相机距离是**按内容包围盒自动反推**的。喂「画面像素」会把画面撑爆，
      喂「米」又把横向跨度过大的人压成角落小方块。
    · 坐标系二义（`x` 像素 / `z` 独立轴 / `height` 既是身高又是体高），
      连着调了三轮才对上（`_ctest` 最小场景才验证通过）。

  Blender（现在这个）
    · **世界单位就是米**，人物 1.72 就是 1.72，没有二义性。
    · 相机/透视/遮挡/影子全部由 Blender 自己解，我只负责按剧本摆模块。
    · 12 镜 5.7 秒渲完，零费用。

★ 白膜的职责边界（照旧，不越界）
  · 白膜 = **空间与运镜预演**（谁在哪、多高、什么姿态、谁挡谁、镜头怎么动）
  · 画面质感那一层由定妆照 + 生成模型负责
  · **绝不上色** —— 上色会渗进成片

对外用法：
    from core.previz_blender import render_project_previz
    res = render_project_previz(db, project_id)     # 渲整个项目
    res = render_project_previz(db, project_id, only=[3, 8])  # 只渲指定镜
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

_LOG = logging.getLogger("videoforge.previz")

_HERE = os.path.dirname(os.path.abspath(__file__))

#: Blender 脚本目录。
#  ★ 为什么放在 `data/` 而不是 `core/`：
#    这三个文件是**交给 Blender 自己的 Python 去读文件执行的**（subprocess），
#    不是被打包的 Python `import` 的。PyInstaller 的 hiddenimports 对它无效 ——
#    必须作为 **datas** 随包发布，否则 exe 里 Blender 拿不到脚本，
#    表现就是「白膜按钮点了没反应 / 报 file not found」。
#    这跟 `data/prompt_library/` 是同一个道理。
def _scripts_dir() -> str:
    # 打包后：_MEIPASS/backend/data/previz_blender
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        p = os.path.join(meipass, "backend", "data", "previz_blender")
        if os.path.isdir(p):
            return p
    # 开发：backend/data/previz_blender
    p = os.path.join(os.path.dirname(_HERE), "data", "previz_blender")
    if os.path.isdir(p):
        return p
    return os.path.join(_HERE, "previz_blender")

#: 找 blender.exe：环境变量 → 常见安装位置
_BLENDER_CANDIDATES = [
    r"D:\Tools\Blender\blender-4.2.1-windows-x64\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.4\blender.exe",
]


def blender_path() -> str:
    """定位 blender.exe。找不到就回空串（调用方要如实报错，不要假装能渲）。"""
    env = os.environ.get("VIDEOFORGE_BLENDER", "").strip()
    if env and os.path.isfile(env):
        return env
    for p in _BLENDER_CANDIDATES:
        if os.path.isfile(p):
            return p
    # 扫 Program Files
    for root in (r"C:\Program Files\Blender Foundation", r"D:\Tools\Blender"):
        if not os.path.isdir(root):
            continue
        try:
            for d in os.listdir(root):
                cand = os.path.join(root, d, "blender.exe")
                if os.path.isfile(cand):
                    return cand
        except Exception:  # noqa: BLE001
            continue
    return ""


def blender_available() -> bool:
    return bool(blender_path())


def _outputs_root(db) -> str:
    try:
        st = db.get_all_settings()
        root = st.get("output_dir") or ""
        if root:
            return root
    except Exception:  # noqa: BLE001
        pass
    return os.path.join(os.getcwd(), "data", "outputs")


def _load_design(project_id: str) -> Any:
    """
    取本项目的白膜设计表。

    ★ 为什么不直接 `from . import previz_blender_design`：
      同一个 `design.py` 还要被 **Blender 的 Python** 读去用（render.py 里
      `import design as DT`），所以它必须作为 data 文件在盘上。
      这里也走文件加载，保证两边**读的是同一份**，不会打包后两边不一致。
    """
    import importlib.util
    path = os.path.join(_scripts_dir(), "design.py")
    spec = importlib.util.spec_from_file_location("vf_previz_design", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DESIGN


def encode_previz_videos(out_dir: str, shots: List[Dict[str, Any]],
                         fps: int = 12) -> int:
    """把每镜的连续帧编码成 MP4（**白膜视频**），返回编码成功的镜数。

    ★ 2026-10-22 新增。为什么必须有这一步：
      · 平台（Seedance / 可灵）的「白模参考」只吃**视频**，不吃 PNG 序列；
      · `core/previz_ref.py` 会把白膜当"参考视频"发出去，而 Blender 路线
        写进 `previz_path` 的是**首帧 PNG** —— 等于把一张图当视频上传，
        平台要么拒、要么静默忽略；用户以为白膜生效了，其实没有；
      · 用户要看的也是"动不动"，不是一张静帧。

    编码参数：12fps（白膜就是这个帧率）、yuv420p、crf 20（白膜不需要高码率）。
    失败**不致命**：返回 0，帧图仍然是好的。
    """
    try:
        from core.ffmpeg_manager import get_ffmpeg_for_postprocess
        ff = get_ffmpeg_for_postprocess()
    except Exception as _e:  # noqa: BLE001
        _LOG.warning("没有可用的 ffmpeg，白膜只出帧图不出视频：%s", _e)
        return 0
    made = 0
    import subprocess as _sp
    for s in shots:
        try:
            d = os.path.join(out_dir, "镜%02d" % int(s["index"]))
            dst = os.path.join(out_dir, "镜%02d.mp4" % int(s["index"]))
        except Exception:  # noqa: BLE001
            continue
        if os.path.exists(dst):
            try:
                os.remove(dst)
            except OSError:
                pass
        cmd = [ff, "-y", "-loglevel", "error",
               "-framerate", str(int(fps)), "-start_number", "1",
               "-i", os.path.join(d, "f%04d.png"),
               "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-crf", "20", "-preset", "veryfast", dst]
        try:
            pr = _sp.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=180,
                         creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            if pr.returncode == 0 and os.path.exists(dst):
                s["video"] = dst
                made += 1
            else:
                _LOG.warning("白膜编码失败（镜%s）：%s", s.get("index"),
                             (pr.stderr or "")[-300:])
        except Exception as _e:  # noqa: BLE001
            _LOG.warning("白膜编码异常（镜%s）：%s", s.get("index"), _e)
    return made


def write_consistency_txt(out_dir: str, consistency: List[Dict[str, Any]]) -> str:
    """把「白膜 ↔ 分镜提示词」逐镜对账写成人类可读的 白膜一致性.txt。"""
    try:
        from core import previz_match as _PM
        _czh = getattr(_PM, "COMPOSITION_ZH", {})
    except Exception:  # noqa: BLE001
        _czh = {}
    _tp = os.path.join(out_dir, "白膜一致性.txt")
    _lines = ["# 白膜 ↔ 分镜提示词 一致性报告（自动生成，逐镜对账）", ""]
    for _r in (consistency or []):
        _rq = _r.get("prompt_req") or {}
        _lines.append("■ 镜%s  [%s]" % (_r.get("index"), _r.get("verdict")))
        _lines.append("  景别：%s　构图：%s　提示词来源：%s" % (
            _rq.get("shot_size_zh") or "?",
            _czh.get(_rq.get("composition"), _rq.get("composition") or "?"),
            _rq.get("source") or "?"))
        for _c in (_r.get("checks") or []):
            _lines.append("  %s %s：要求 %s ｜ 实际 %s" % (
                "✓" if _c.get("ok") else "✗", _c.get("name"),
                _c.get("want"), _c.get("got")))
        for _cf in (_rq.get("conflicts") or []):
            _lines.append("  ⚠ 提示词自身矛盾：%s" % _cf)
        _lines.append("")
    if not consistency:
        _lines.append("（没有一致性数据 —— 白膜可能没走 spec 渲染，或还没重渲）")
    with open(_tp, "w", encoding="utf-8") as _fh:
        _fh.write("\n".join(_lines))
    return _tp


def render_project_previz(db, project_id: str, *,
                          only: Optional[List[int]] = None,
                          out_dir: str = "",
                          timeout: int = 900) -> Dict[str, Any]:
    """
    渲整个项目的白膜（Blender headless）。

    Returns:
        {ok, reason?, exe, out_dir, shots:[{index, frames:[...]}], seconds}
    """
    bl = blender_path()
    if not bl:
        return {"ok": False,
                "reason": "没找到 blender.exe。白膜 3D 预演需要 Blender 4.2+。\n"
                          "  · 可在设置里填环境变量 VIDEOFORGE_BLENDER 指向它\n"
                          "  · 或装到默认位置（当前探测：%s）"
                          % " / ".join(_BLENDER_CANDIDATES[:2])}

    if not out_dir:
        out_dir = os.path.join(_outputs_root(db), project_id, "previz")

    env = dict(os.environ)
    env["VF_BD_OUT"] = out_dir
    # 2026-10-25：把脚本目录显式传给 render_spec.py —— 它被 exec() 注入时 __file__ 不可靠
    env["VF_PREVIZ_DIR"] = _scripts_dir()
    _sd = _scripts_dir()

    def _run_blender(runner_name: str, extra_env: Dict[str, Any] = None):
        """跑一次 Blender。返回 (proc|None, 日志)。"""
        r = os.path.join(_sd, runner_name)
        expr = ("import sys; sys.path.insert(0, r'%s'); sys.argv=%r; "
                "exec(open(r'%s', encoding='utf-8').read())"
                % (_sd, ["previz"] + [str(x) for x in (only or [])], r))
        e2 = dict(env)
        e2.update(extra_env or {})
        try:
            pr = subprocess.run([bl, "-b", "--factory-startup", "-noaudio",
                                 "--python-expr", expr],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=timeout, env=e2)
            return pr, (pr.stdout or "") + (pr.stderr or "")
        except subprocess.TimeoutExpired:
            return None, "__timeout__"
        except Exception as ex:  # noqa: BLE001
            return None, f"{type(ex).__name__}: {ex}"

    def _collect():
        found = []
        for i in (only or range(1, 33)):
            d = os.path.join(out_dir, f"镜{i:02d}")
            if not os.path.isdir(d):
                continue
            fr = sorted(os.path.join(d, f) for f in os.listdir(d)
                        if f.endswith(".png"))
            if fr:
                found.append({"index": i, "frames": fr})
        return found

    # ══════════════════════════════════════════════════════════════
    # ★★★ 2026-10-20：**先按本项目的剧本/分镜生成 3D 规格，再渲**。
    #
    #   在这之前，白膜渲的一直是 `data/previz_blender/design.py` 里
    #   **为另一个项目手写的**模板场景（楼顶/村口…）——
    #   任何新项目点「白膜预演」，看到的都是别人戏里的画面。
    #   于是"花任何钱之前先看机位/走位"这件事根本没发生。
    #
    #   现在：`previz_design.generate_spec()` 按分镜产出 JSON 规格
    #   （场景类型/尺寸/道具/人物站位与移动/相机关键帧），
    #   由通用渲染器 `render_spec.py` 搭 3D。
    #   规格拿不到或渲失败 → 才回退旧模板，并在返回里**如实说明**走的是回退。
    # ══════════════════════════════════════════════════════════════
    design_source = "template"
    design_reason = ""
    shots: List[Dict[str, Any]] = []
    log_tail = ""
    try:
        from core import previz_design as _PD
        gen = _PD.generate_spec(db, project_id)
        if gen.get("ok"):
            _pr, log_tail = _run_blender("render_spec.py",
                                         {"VF_PREVIZ_SPEC": gen["path"]})
            if log_tail == "__timeout__":
                return {"ok": False, "exe": bl,
                        "reason": f"Blender 渲染超时（>{timeout}s）"}
            if _pr is not None and _pr.returncode == 0:
                shots = _collect()
                if shots:
                    design_source = ("script" if gen.get("source") == "llm"
                                     else "script-fallback")
            if not shots:
                _LOG.warning("按剧本规格渲染没出帧，回退内置模板：%s",
                             (gen.get("reason") or "")[:300])
        else:
            design_reason = str(gen.get("reason") or "")
    except Exception as _e:  # noqa: BLE001
        _LOG.warning("白膜规格生成异常，回退内置模板：%s", _e)
        design_reason = f"{type(_e).__name__}: {_e}"

    if not shots:
        # ── 回退：旧的内置模板（design.py）──
        _pr, log_tail = _run_blender("render.py", {})
        if log_tail == "__timeout__":
            return {"ok": False, "exe": bl,
                    "reason": f"Blender 渲染超时（>{timeout}s）"}
        if _pr is None:
            return {"ok": False, "exe": bl,
                    "reason": f"调 Blender 失败：{log_tail}"}
        shots = _collect()
        design_source = "template"

    if not shots:
        return {"ok": False, "reason": "Blender 没产出任何帧",
                "exe": bl, "log": log_tail[-1500:]}

    # ★★ 2026-10-23：取景断言报告（由 render_spec.py 在 Blender 侧落盘）。
    #   "白膜有没有真的拍到主体"不能只靠肉眼 —— 报告里逐镜写着投影指标
    #   （出框比例 / 头部是否在框 / 是否被墙挡住 / 重构图前后的画面占比）。
    #   这里把它带进返回值，界面/回执就能如实说明，坏画面不会静默通过。
    _framing: List[Dict[str, Any]] = []
    try:
        _fp = os.path.join(out_dir, "取景报告.json")
        if os.path.isfile(_fp):
            with open(_fp, "r", encoding="utf-8") as _fh:
                _framing = (json.load(_fh) or {}).get("shots") or []
    except Exception as _e:  # noqa: BLE001
        _LOG.warning("读取取景报告失败（不影响白膜本体）：%s", _e)
    _fr_fixed = [r for r in _framing if r.get("verdict") == "fixed"]
    _fr_bad = [r for r in _framing if r.get("verdict") in ("partial", "assert-only")]
    if _fr_bad:
        _LOG.warning("白膜取景断言：%d 镜仍不合格（镜%s），详见 取景报告.json",
                     len(_fr_bad), ",".join(str(r.get("index")) for r in _fr_bad))

    # ★★ 2026-10-23（用户）：「得确认白膜视频以后都跟分镜提示词要求一致！」
    #   渲染器逐镜把"提示词要求 vs 实际做到"对账写进 白膜一致性.json；
    #   这里再落一份人可读的 白膜一致性.txt，并把结论带回执。
    _consistency: List[Dict[str, Any]] = []
    try:
        _cp = os.path.join(out_dir, "白膜一致性.json")
        if os.path.isfile(_cp):
            with open(_cp, "r", encoding="utf-8") as _fh:
                _consistency = (json.load(_fh) or {}).get("shots") or []
    except Exception as _e:  # noqa: BLE001
        _LOG.warning("读取白膜一致性失败（不影响白膜本体）：%s", _e)
    _cons_bad = [r for r in _consistency if r.get("verdict") == "mismatch"]
    _cons_ok = [r for r in _consistency
                if r.get("verdict") in ("consistent", "adjusted")]
    if _cons_bad:
        _LOG.warning("白膜与分镜提示词：%d 镜不一致（镜%s），详见 白膜一致性.txt",
                     len(_cons_bad),
                     ",".join(str(r.get("index")) for r in _cons_bad))
    try:
        write_consistency_txt(out_dir, _consistency)
    except Exception as _e:  # noqa: BLE001
        _LOG.warning("写白膜一致性.txt 失败（不影响白膜本体）：%s", _e)

    # ★ 2026-10-22：帧 → 白膜视频（平台只吃视频当"白模参考"，见 encode_previz_videos 说明）
    _nv = encode_previz_videos(out_dir, shots,
                               fps=24 if design_source in ("script", "script-fallback") else 12)
    if _nv:
        _LOG.info("白膜视频已编码：%d/%d 镜", _nv, len(shots))

    # ★★ 2026-10-06：白膜 OK → 自动写回 previz_status='draft_approved'。
    #   这样 _compose_full_prompt 会读 previz_camera（如果非空），
    #   并提示模型"白膜已确认"，让真实出片和白膜节奏一致。
    #   之后用户可以 PATCH /api/shots/{sid} 调整 previz_camera 改成具体运镜。
    if shots and db is not None:
        try:
            _shots_in_db = [x for x in (db.list_shots(project_id) or [])
                            if isinstance(x, dict)]
            # ★★ 2026-10-20 修：**按库里的顺序（1 基）映射，不要拿 order_index 当 key**。
            #   渲染侧的 `index` 是 **1 基**（`render.py` 里 `range(1, 13)`，
            #   目录名 `镜01/镜02…`），而 `order_index` 在库里可能是 **0 基**
            #   （实测某项目是 0、1）。
            #   原来的写法 `{order_index: shot}` 再 `.get(s["index"])` 于是：
            #     index=1 → 命中 order_index=1（其实是**第 2 个**镜头，错位）
            #     index=2 → 落空（只有 0、1）
            #   ⇒ 实测后果：2 个镜头只写回了 1 个，`_ev_previz` 判"1/2 渲了"→
            #     **"下一步"永远指回白膜**，用户点确认也走不动（看着像卡死）。
            #   改法：渲染就是按库里顺序一镜一镜渲的，用**位置**映射最稳。
            try:
                _ordered = sorted(
                    _shots_in_db,
                    key=lambda x: (int(x.get("order_index") or 0),
                                   str(x.get("id") or "")))
            except Exception:
                _ordered = list(_shots_in_db)
            _written = 0
            for s in shots:
                try:
                    _pos = int(s["index"]) - 1        # 渲染 index 是 1 基
                except Exception:
                    continue
                _target = _ordered[_pos] if 0 <= _pos < len(_ordered) else None
                _sid = (_target or {}).get("id")
                if _sid:
                    db.update_shot(_sid, previz_status="rendered",
                                     previz_path=s["frames"][0] if s["frames"] else None)
                    _written += 1
            _LOG.info("白膜渲染完成：%d 镜 previz_status=rendered（等待审核）"
                      "（库里共 %d 镜）", _written, len(_ordered))
            # ★ 2026-10-22：导出「白膜规格说明」（Proma/blockout 的三件套之一：
            #   白膜视频 + 每机位静帧 + 自动 prompt.txt）。
            #   没有它，"白膜到底锁定了什么"只躺在 spec.json 里 —— 用户看不到，
            #   下游出片也拿不到，等于白膜白渲。
            try:
                from core import previz_notes as _notes
                from core import previz_design as _pd
                _spec = _pd.load_spec(db, project_id)
                if _spec:
                    _notes.export_notes(_spec, _outputs_root(db), project_id)
                    _LOG.info("白膜规格说明已导出")
            except Exception as _e2:  # noqa: BLE001
                _LOG.warning("导出白膜规格说明失败（不影响白膜本体）：%s", _e2)
        except Exception as _e:
            _LOG.warning("白膜 approve 写库失败（不影响生成）：%s", _e)

    # ★ 2026-10-20：**如实说明白膜是怎么来的**。
    #   现在默认是「按这个项目的剧本/分镜生成 3D 规格」，
    #   只有规格拿不到/渲失败时才回退到内置模板 —— 两种都要说清楚，
    #   不能让用户拿着别人的画面当自己的片子预览。
    if design_source == "script":
        _note = ("白膜是**按你的剧本/分镜生成的 3D 预演**：场景类型与尺寸、"
                 "道具、人物站位/移动、相机关键帧都来自这一版分镜，"
                 "不是通用模板。本地渲染，零费用。")
    elif design_source == "script-fallback":
        _note = ("白膜是按你的分镜做了**确定性推断**（场景类型/时长/人物站位），"
                 "模型这次没给出可用规格，所以细节（道具、运镜）较粗。"
                 "本地渲染，零费用。")
    else:
        _note = ("⚠ 白膜用的是**内置机位模板**（没有按你的剧本重建）"
                 + (f"；原因：{design_reason[:120]}" if design_reason else "")
                 + "。它只能用来定机位/节奏，画面不是你戏里的内容。")
    # ★ 2026-10-23：把取景断言的结论如实写进回执（不再"看起来有画面就算过"）。
    if _framing:
        _fr_ok = [r for r in _framing if r.get("verdict") in ("ok", "off")]
        if _fr_bad:
            _note += ("\n🚨 取景断言：%d 镜仍不合格（镜%s）—— 白膜不能直接当参考视频。"
                      "详见 previz/取景报告.json"
                      % (len(_fr_bad),
                         "、".join(str(r.get("index")) for r in _fr_bad)))
        elif _fr_fixed:
            _note += ("\n🎯 取景断言：%d 镜原机位框不到主体，已按「主体完整入框」"
                      "自动重构图（%d 镜合格）。详见 previz/取景报告.json"
                      % (len(_fr_fixed), len(_fr_ok)))
        else:
            _note += "\n🎯 取景断言：%d/%d 镜主体完整在框内。" % (len(_fr_ok), len(_framing))
    # ★ 2026-10-23：白膜 ↔ 分镜提示词 一致性（用户要求的"确认"）
    if _consistency:
        if _cons_bad:
            _note += ("\n🚨 白膜与分镜提示词：%d/%d 镜**不一致**（镜%s）—— "
                      "不能当参考视频、也不该确认。详见 previz/白膜一致性.txt"
                      % (len(_cons_bad), len(_consistency),
                         "、".join(str(r.get("index")) for r in _cons_bad)))
        else:
            _note += ("\n✅ 白膜与分镜提示词：%d/%d 镜一致（%d 镜按提示词重建了机位/构图）。"
                      "逐项对账见 previz/白膜一致性.txt"
                      % (len(_cons_ok), len(_consistency),
                         len([r for r in _cons_ok if r.get("verdict") == "adjusted"])))
    return {"ok": True, "exe": bl, "out_dir": out_dir, "shots": shots,
            "videos": [s.get("video") for s in shots if s.get("video")],
            "design_source": design_source,
            "design_reason": design_reason,
            "framing": _framing,
            "consistency": _consistency,
            "note": _note}


# ══════════════════════════════════════════════════════════════
#  从剧本生成资产卡内容（把白膜的尺寸/姿态/道具带进 VideoForge）
# ══════════════════════════════════════════════════════════════
def design_to_character_cards(design) -> List[Dict[str, Any]]:
    """
    从白膜设计表导出角色卡内容。

    ★ 这是「白膜 → 正式出片」之间的桥：
      白膜里定的真实身高、姿态、道具，直接写成 VideoForge 的角色卡，
      这样 480p 出片时文字设定与预演一致，不会再冒出体型/比例错误。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vf_previz_design2", os.path.join(_scripts_dir(), "design.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    agg: Dict[str, Dict[str, Any]] = {}
    for i, d in design.items():
        for c in d["cast"]:
            name, x, y, rot, pose, mv = c[0], c[1], c[2], c[3], c[4], c[5]
            e = agg.setdefault(name, {"name": name, "shots": [], "height_cm": mod.H_M.get(name, 170),
                                     "poses": set(), "props": set()})
            e["shots"].append(i)
            e["poses"].add(pose)
            for p in d["props"]:
                if p[0] in ("断钩秤", "铁尺", "钱袋鼓", "钱袋瘪", "木牌", "油灯",
                            "铜钱", "门牙", "鞭痕"):
                    e["props"].add(p[0])
        # 道具跟着人带（如兵丁腰上的秤/铁尺/钱袋）
        for p in d["props"]:
            for c in d["cast"]:
                if abs(p[1] - c[1]) < 0.6 and abs(p[2] - c[2]) < 0.5:
                    e = agg.get(c[0])
                    if e:
                        e["props"].add(p[0])

    out = []
    for name, e in agg.items():
        h = e["height_cm"] / 100.0
        pose_zh = {"stand": "站立", "squat": "蹲姿", "prone": "伏地（身体平躺、头肩胸以上露出）"}
        poses = "、".join(pose_zh.get(p, p) for p in sorted(e["poses"]))
        out.append(dict(
            name=name,
            body_type=f"身高 {h:.2f}m 体型{'偏瘦少年' if h < 1.75 else ('壮实' if h >= 1.73 else '中等')}"
                      f" 出场姿态：{poses}",
            description=f"3D 预演中出现于第 {'、'.join(map(str, sorted(e['shots'])))} 镜",
            props=sorted(e["props"]),
            reference_features=dict(
                silhouette=poses,
                texture="粗布短褐 湿透贴背",
                state=f"出场 {len(e['shots'])} 镜",
            ),
        ))
    return out


def design_to_scene_cards(design) -> List[Dict[str, Any]]:
    """从白膜设计表导出场景卡内容。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vf_previz_design3", os.path.join(_scripts_dir(), "design.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    names = {"set_alley": ("灰巷·巷口", "outdoor", "night", "雨后 地面湿透有积水"),
             "set_stall": ("灰巷·老周棋摊", "outdoor", "night", "阴 湿石板")}
    agg: Dict[str, Dict[str, Any]] = {}
    for i, d in design.items():
        key = d["setg"].__name__
        if key not in names:
            continue
        nm, lot, tod, wea = names[key]
        e = agg.setdefault(nm, {"name": nm, "location_type": lot, "time_of_day": tod,
                                "weather": wea, "shots": [], "props": set()})
        e["shots"].append(i)
        for p in d["props"]:
            e["props"].add(p[0])

    out = []
    for nm, e in agg.items():
        out.append(dict(
            name=nm,
            location_type=e["location_type"],
            space_scale="small",
            lighting="单一油灯 暖黄光只够照亮局部（巷底半墙后 / 案头）",
            time_of_day=e["time_of_day"],
            weather=e["weather"],
            description=f"3D 预演覆盖第 {'、'.join(map(str, sorted(e['shots'])))} 镜",
            reference_features=dict(
                terrain="砖地/湿石板 + 一洼积水（水深 1cm 只反光）",
                architecture="两侧残墙 巷宽 3.5m 墙高 2.45m",
                fixtures="、".join(sorted(e["props"])) or "油灯",
                tone="冷灰偏青 唯一暖色来自油灯",
                camera="机位人眼高度 1.3~1.7m 焦距 28~38mm",
            ),
        ))
    return out
