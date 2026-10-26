"""
VideoForge · FastAPI 主入口

启动方式：
- 开发：uvicorn main:app --reload
- 生产：python main.py（pywebview 嵌 WebView）
"""

import asyncio
import hashlib
import json
import logging
import sys
import subprocess
import shutil
from urllib.parse import quote
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

# 添加 backend 到路径
sys.path.insert(0, os.path.dirname(__file__))

# 本进程的启动时刻 + 关键源文件的指纹。
# 用途：验收脚本连上 8766 之后先看一眼，就知道自己验的是不是**当前代码**。
# 只在导入时算一次，所以它是"这个进程加载的代码"的指纹，不是磁盘现状。
_BOOT_AT = datetime.now().isoformat(timespec="seconds")


def _compute_code_stamp() -> str:
    """关键源文件的指纹。**只在开发/源码运行时有效**。

    ⚠ 打包版（PyInstaller）里源码在 PYZ 里，这几个文件读不到，
    指纹会退化成六个 `<missing>` 的固定哈希 —— 它在那里**不能**用来判断新旧。
    它的用途就是"对着源码起的开发服务器"：验收脚本连上后先核对一次，
    避免拿旧进程的结果下结论（2026-09-13 真踩过）。
    """
    h = hashlib.sha256()
    for rel in ("main.py", "core/promptaudit.py", "core/videoprompt.py",
                "core/voicecast.py", "core/compose.py", "core/seamfix.py"):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except Exception:
            h.update(b"<missing>")
    return h.hexdigest()[:12]


_CODE_STAMP = _compute_code_stamp()

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query, BackgroundTasks, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from models import (
    APIResponse, ProjectCreate, ProjectUpdate, Project,
    ScriptGenerateRequest, ScriptUpdate,
    CharacterCreate, CharacterUpdate, Character,
    SceneCreate, SceneUpdate, Scene,
    PropCreate, PropUpdate, Prop,
    ShotCreate, ShotUpdate, Shot,
    TaskInfo, GenerateVideoRequest, AbstractionRequest,
    AppSettings, SettingsUpdate,
)
from core.db import Database
from core.task_queue import TaskQueue
from core import orchestrator  # 注册任务处理器
from core import adapters  # 触发所有适配器子模块的 @register 装饰器
from core.adapters import list_providers, get_adapter
from core.llm import LLMClient, ScriptService, AbstractionService
from core.imagegen import generate_image, list_image_providers
from core.ffmpeg_manager import find_or_install_ffmpeg, get_ffmpeg_for_postprocess
from core import voice as voice_service
from core.voice import (
    TTSRequest,
    TTSResult,
    synthesize as voice_synthesize,
    list_voices as voice_list_voices,
    list_providers as voice_list_providers,
    is_no_voice as voice_is_no_voice,
    estimate_no_voice_duration as voice_estimate_duration,
)
from core.postprocess import (
    check_ffmpeg, stitch_videos, burn_subtitles,
    export_aspect_ratio, mix_bgm, get_video_info,
    generate_srt_from_timeline,
)
from api.chat import router as chat_router


# ──────────── 应用状态（全局单例）────────────

_app_state: Dict[str, Any] = {}


def get_app_state() -> Dict[str, Any]:
    """获取应用状态（供任务处理器使用）"""
    return _app_state


# ──────────── 日志 ────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("videoforge")


def paragraphs_to_scenes(paras: List[str], max_scenes: int = 40) -> List[str]:
    """把纯文字段落粗切成场次（每段一场；段落过长再按句号细切）。

    用于**导入剧本但 LLM 结构化失败**时的兜底 —— 宁可粗糙也要让下游能推进，
    而不是让用户卡在"导入了但什么都做不了"。
    """
    out: List[str] = []
    for p in paras:
        p = (p or "").strip()
        if not p:
            continue
        if len(p) <= 600:
            out.append(p)
            continue
        buf = ""
        for sent in re.split(r"(?<=[。！？!?；;])", p):
            if len(buf) + len(sent) > 500 and buf:
                out.append(buf.strip())
                buf = sent
            else:
                buf += sent
        if buf.strip():
            out.append(buf.strip())
        if len(out) >= max_scenes:
            break
    return out[:max_scenes]


# 各视频厂商的默认模型 / 接入点（用户没填时用它兜底；火山的接入点 ID 需用户自填）
DEFAULT_MODEL_IDS: Dict[str, str] = {
    "kling": "kling-1.6",
    "wanx": "wanx2.1-t2v-turbo",
    "hailuo": "MiniMax-Hailuo-02",
    "minimax": "MiniMax-Hailuo-02",
    "jimeng": "jimeng-video-3.0",
    "seedance": "doubao-seedance-1-0-pro-250528",
    "cogvideox": "cogvideox",
    "luma": "ray-2",
    "pika": "pika-2.2",
    "runway": "gen4_turbo",
    "sora": "sora-2",
}

# 各厂商的**特征关键词**：用于识别"这个模型名属于哪一家"
# ⚠ 只用来判断"明显属于其他厂商"，**不能反过来**要求"必须含本厂商关键词"——
#   海螺的真实版本名 video-01 / T2V-01 / I2V-01 / video-01-live2d 里
#   一个 hailuo/minimax 字样都没有，用白名单式判断会把用户**选对的版本改掉**。
_MODEL_OWNER_HINTS = {
    "kling": ("kling",),
    "hailuo": ("hailuo", "minimax", "video-01", "t2v-01", "i2v-01", "s2v-01", "abab"),
    "minimax": ("hailuo", "minimax", "video-01", "t2v-01", "i2v-01"),
    "seedance": ("seedance", "doubao"),
    "jimeng": ("jimeng", "doubao"),
    "wanx": ("wanx", "wan2", "wanx2"),
    "cogvideox": ("cogvideo",),
    "luma": ("ray-", "ray2", "luma"),
    "pika": ("pika",),
    "runway": ("gen3", "gen4", "runway"),
    "sora": ("sora",),
}


def _snap_legal_duration(adapter, provider: str, model_name: str,
                         resolution: str, want: int) -> int:
    """把想要的段长吸附到「该模型在该分辨率下**真正允许**」的时长。

    ★ 为什么需要它：真实约束是"某分辨率下只允许某些时长"，
      而适配器只暴露一个标量 `max_duration`，表达不了这件事。
      线上实测 `MiniMax-Hailuo-2.3`：{'768P': [6, 10], '1080P': [6]}，
      用户按 1080P 生成 10 秒分镜时，标量校验会放行（标量是 10），
      请求发出去被厂商拒绝或**静默截成 6 秒** —— 表现就是"视频根本没生成"。

    吸附规则：精确命中 > 不小于 want 的最小合法值 > 最大的合法值。
    模型没登记能力表时，退回原来的标量 min/max（保持旧行为）。
    """
    try:
        from core.model_catalog import caps_for
        caps = {}
        for k, v in (caps_for(provider, model_name) or {}).items():
            try:
                caps[str(k).upper()] = sorted({int(x) for x in v})
            except Exception:
                continue
        allowed = caps.get(str(resolution or "").upper()) or []
        if allowed:
            if int(want) in allowed:
                return int(want)
            bigger = [d for d in allowed if d >= int(want)]
            return int(bigger[0] if bigger else allowed[-1])
    except Exception:
        pass
    return int(max(adapter.min_duration or 1,
                   min(int(want), adapter.max_duration or int(want))))


def model_name_matches_provider(provider: str, model_name: str) -> bool:
    """判断模型名是否**明显属于其他厂商**（只做黑名单判断，不做白名单强制）。

    历史脏数据的来源：早期「换模型」弹窗不联动模型名，
    库里留下了 `provider=hailuo, model_name=kling-1.6` 这种组合 ——
    拿 kling 的名字去调 MiniMax 必然失败。

    但**纠正必须是保守的**：
      · provider=hailuo, model=kling-1.6     → kling 是别家 → 纠正 ✔
      · provider=hailuo, model=video-01      → 认不出是哪家 → **放过，不要动** ✔
      · provider=hailuo, model=ep-2024xxxx   → 接入点 ID → **放过** ✔
    早先写成"必须含本厂商关键词"，结果把用户选对的
    video-01 / T2V-01 / I2V-01 全改回了 Hailuo-02。
    """
    if not provider or not model_name:
        return True
    m = str(model_name).strip().lower()
    if not m:
        return True
    # 接入点 ID / 空值 / 自定义名 → 一律放过
    if m.startswith("ep-") or m in ("default", "auto"):
        return True
    prov = provider.strip().lower()
    own = _MODEL_OWNER_HINTS.get(prov, ())
    # 含本厂商特征 → 通过
    if any(k in m for k in own):
        return True
    # 含**别家**特征 → 判定为不匹配（需要纠正）
    for other, kws in _MODEL_OWNER_HINTS.items():
        if other == prov:
            continue
        if any(k in m for k in kws):
            return False
    # 认不出属于谁 → 保守放过（宁可不动，也不能改错）
    return True

# ──────────── FastAPI 应用 ────────────

def create_app(db_path: str = None) -> FastAPI:
    app = FastAPI(title="VideoForge", version="1.0.0")

    # CORS（开发用，生产 pywebview 无需）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ⚠ 必须在任何子进程调用之前注入：隐藏控制台窗口（防闪屏）+ 清理环境（防 0xc0000142）
    try:
        from core.proc import install_asyncio_patch
        install_asyncio_patch()
    except Exception as _e:
        logger.warning("子进程补丁注入失败（可能仍会闪屏）：%s", _e)

    # 数据目录优先从环境变量读（launcher / v3 桌面端注入），保证跨会话持久。
    # ★ 2026-10-21：规则收在 core/data_root.py（单一真相源），别再在这里内联一份。
    #   优先级：VIDEOFORGE_DATA_DIR → %LOCALAPPDATA%\VideoForge\data（用户装的那份库，
    #   要求该目录里已有真的 videoforge.db）→ <cwd>\data（开发模式）。
    from core.data_root import describe_data_root, resolve_data_root
    data_root = resolve_data_root()
    logger.info("数据目录解析：%s", describe_data_root(data_root))
    os.makedirs(data_root, exist_ok=True)

    # ★ 技能扩展环境（2026-10-25 客户刚需）：安装版是冻结包，**没有 pip**，且打包时
    #   主动裁掉了 cv2/pandas/numba/cryptography/llvmlite 等依赖；用户自蒸馏的 skill
    #   只要 import 这些就崩。这里开一个**用户可写**的扩展目录并注入 sys.path/PATH，
    #   再配 `/api/env` 体检 + 离线 wheel 安装。详见 core/env_ext.py 的文件头。
    #   失败**不阻断**后端启动（环境扩展是增强项，不是主流程前提）。
    try:
        from core.env_ext import activate as _activate_skillenv
        _skillenv_info = _activate_skillenv(data_root)
        if _skillenv_info.get("ok"):
            logger.info("技能扩展目录已注入：%s", _skillenv_info.get("python_path"))
        else:
            logger.warning("技能扩展目录不可用：%s", _skillenv_info.get("error"))
    except Exception as _skillenv_err:  # noqa: BLE001
        logger.warning("技能扩展目录初始化失败（不影响主流程）：%s", _skillenv_err)

    if db_path is None:
        db_path = os.path.join(data_root, "videoforge.db")

    # 初始化
    db = Database(db_path)

    # Seed 默认设置（首次启动）。输出/缓存目录固定到 data_root，保证历史持久
    existing_settings = db.get_all_settings()
    if "output_dir" not in existing_settings:
        default_out = os.path.join(data_root, "outputs")
        os.makedirs(default_out, exist_ok=True)
        db.set_setting("output_dir", default_out)
    if "cache_dir" not in existing_settings:
        default_cache = os.path.join(data_root, "cache")
        os.makedirs(default_cache, exist_ok=True)
        db.set_setting("cache_dir", default_cache)
    queue = TaskQueue(db, max_concurrent=3)

    _app_state["db"] = db
    _app_state["queue"] = queue

    # 注册对话路由
    app.include_router(chat_router)
    from core.vf_accounts import create_account_router
    app.include_router(create_account_router())
    from core.studio_bridge import create_studio_router
    app.include_router(create_studio_router(db))
    # The workbench is part of this release, served from the same managed payload.
    studio_dir = Path(__file__).resolve().parent / "data" / "studio_workbench"
    if (studio_dir / "index.html").is_file():
        class _StudioStatic(StaticFiles):
            async def get_response(self, path, scope):
                response = await super().get_response(path, scope)
                response.headers["Cache-Control"] = "no-cache"
                return response
        app.mount("/studio", _StudioStatic(directory=str(studio_dir), html=True), name="studio-workbench")

    # FFmpeg 智能获取（优先使用 launcher 传入的环境变量）
    import os as _os
    detected_ffmpeg = _os.environ.get("VIDEOFORGE_FFMPEG") or find_or_install_ffmpeg()
    existing = db.get_all_settings()
    configured = (existing.get("ffmpeg_path") or "").strip()
    configured_ok = bool(configured) and configured != "ffmpeg" and _os.path.exists(configured)
    # 程序自带（安装目录内）的 ffmpeg 优先 —— 避免换机器/升级后残留旧安装路径
    _bundle_root = getattr(sys, "_MEIPASS", None) or _os.path.dirname(
        _os.path.dirname(_os.path.abspath(__file__)))
    detected_in_bundle = bool(detected_ffmpeg) and str(detected_ffmpeg).startswith(str(_bundle_root))
    if detected_ffmpeg and _os.path.exists(detected_ffmpeg) and (not configured_ok or detected_in_bundle):
        if configured and configured != detected_ffmpeg:
            logger.info(f"FFmpeg 路径切换：{configured} → {detected_ffmpeg}")
        db.set_setting("ffmpeg_path", detected_ffmpeg)
        logger.info(f"FFmpeg ready: {detected_ffmpeg}")
    elif configured_ok:
        logger.info(f"FFmpeg (user-configured): {configured}")
    else:
        logger.warning("FFmpeg not available - post-processing will fail")

    # ──── 静态文件（前端）────────

    frontend_dir = _resolve_frontend_dir()
    if frontend_dir.exists():
        class _NoCacheStatic(StaticFiles):
            """前端资源一律 `no-cache`：**桌面应用更新后必须立刻拿到新 JS**。

            ★ 2026-10-20 修（用户实机复现）：静态资源原来**没有 Cache-Control 头**，
              浏览器就按「启发式缓存」处理（Freshness = Last-Modified 距今的 10%）。
              实测：app.js 上午 09:33 改的，浏览器到 09:55 还在用**旧副本** ——
              于是「我在源码里改好了，界面上完全没变」，白查半天。
              `no-cache` 不是「不缓存」，而是「每次都回源核对」：ETag/Last-Modified
              还在，没改就回 304（几乎零流量），改了就是新文件。
            """

            def file_response(self, *args, **kwargs):
                resp = super().file_response(*args, **kwargs)
                try:
                    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
                except Exception:
                    pass
                return resp

        # 挂载静态资源
        app.mount(
            "/css",
            _NoCacheStatic(directory=str(frontend_dir / "css")),
            name="css",
        )
        app.mount(
            "/js",
            _NoCacheStatic(directory=str(frontend_dir / "js")),
            name="js",
        )
        if (frontend_dir / "assets").exists():
            app.mount(
                "/assets",
                _NoCacheStatic(directory=str(frontend_dir / "assets")),
                name="assets",
            )

        @app.get("/", response_class=HTMLResponse)
        async def serve_index():
            """首页。

            ★ 2026-10-20 加：**给脚本/样式加版本戳**（`?v=<文件 mtime>`）。
              为什么：桌面应用升级后，浏览器可能还拿着**旧 JS** 的缓存副本
              （启发式缓存：Freshness = Last-Modified 距今的 10%，可以新鲜几十
              分钟到几小时）—— 实测「源码里改好了，界面上完全没变」。
              版本戳让**文件一改，URL 就变**，浏览器必然重新取。
              同时首页本身也 `no-store`，保证 index.html 永远是最新的那份。
            """
            idx = frontend_dir / "index.html"
            try:
                html = idx.read_text(encoding="utf-8", errors="replace")
                import re as _re_stamp

                def _mtime_of(rel: str) -> int:
                    try:
                        return int((frontend_dir / rel).stat().st_mtime)
                    except Exception:
                        return 0

                html = _re_stamp.sub(
                    r'(?:src|href)="((?:js|css)/[^"?]+\.(?:js|css))"',
                    lambda m: f'{m.group(0)[:-1]}?v={_mtime_of(m.group(1))}"',
                    html)
                return HTMLResponse(html, headers={
                    "Cache-Control": "no-store, must-revalidate"})
            except Exception:
                return FileResponse(str(idx))

        @app.get("/favicon.ico")
        async def favicon():
            ico = frontend_dir / "favicon.ico"
            if ico.exists():
                return FileResponse(str(ico))
            return HTMLResponse(status_code=204)

    # ──────────── 工具函数 ────────────

    def success(data: Any = None, message: str = "success") -> Dict[str, Any]:
        return {"code": "000000", "message": message, "data": data}

    def error_response(message: str, code: str = "000001", status: int = 400) -> HTTPException:
        return HTTPException(status_code=status, detail={"code": code, "message": message})

    # ──────────── 根 & 健康 ────────────

    @app.get("/api/about")
    async def api_about():
        """版本 / 版权 / 许可 —— 界面顶栏徽标与「关于」页的数据源。

        ★ 2026-10-20 补：前端 `frontend/js/api.js` 早就写好了 `about: () => request('GET', '/api/about')`，
          `app.js` 启动时也会去拉它来刷新左上角的版本徽标 —— 但**后端从来没注册这个路由**，
          于是请求 404 → 前端 `.catch(() => {})` 静默吞掉 → 顶栏一直显示
          `index.html` 里硬编码的 `v2.0.0`，后端升到 2.1.0 了界面也不知道。
          （实测：用户界面上就是 v2.0.0，而 `core/about.py` 里是 2.1.0。）
        """
        from core import about as _about
        try:
            return success(_about.info())
        except Exception as e:  # noqa: BLE001
            return success({"product": "VideoForge", "version": "0.0.0",
                            "error": f"{type(e).__name__}: {e}"})

    @app.get("/api/health")
    async def health():
        settings = db.get_all_settings()
        ffmpeg_path = settings.get("ffmpeg_path", "ffmpeg")
        if not check_ffmpeg(ffmpeg_path):
            # 兜底自愈：配置可能指向已失效路径，实时探测并写回
            alt = _os.environ.get("VIDEOFORGE_FFMPEG") or find_or_install_ffmpeg()
            if alt and check_ffmpeg(alt):
                ffmpeg_path = alt
                db.set_setting("ffmpeg_path", alt)
        return success({
            "status": "ok",
            "ffmpeg_available": check_ffmpeg(ffmpeg_path),
            "ffmpeg_path": ffmpeg_path,
            "providers_count": len(list_providers()),
            # ★ 进程启动时间 + 代码指纹：**验证脚本必须能判断自己连的是不是旧进程**。
            #   踩过（2026-09-13）：改完后端拿接口验证，结果 8766 上跑的是 11:00 起的
            #   旧进程，新加的告警"没出现"，差点被判成"代码没生效"。
            #   现在任何验收脚本都可以先比 `started_at` 和源文件的 mtime。
            "started_at": _BOOT_AT,
            "pid": _os.getpid(),
            "code_stamp": _CODE_STAMP,
        })

    @app.get("/api/env")
    async def env_report():
        """技能环境体检：解释器 / 可写扩展目录 / 客户 skill 常缺的包。

        ★ 客户排障第一站：「我的 skill 导入失败」→ 先看这里缺什么、按 hint 补。
        """
        from core.env_ext import diagnose
        return success(diagnose(data_root))

    @app.post("/api/env/install-wheel")
    async def env_install_wheel(payload: Dict[str, Any] = None):
        """离线安装 wheel（冻结版没有 pip）：需要本地 .whl 的绝对路径。

        只解压、不联网、不执行包内代码；ABI 不匹配会在 `/api/env` 里如实标 missing。
        """
        from core.env_ext import install_wheel
        wheel = str((payload or {}).get("path") or "").strip()
        if not wheel:
            raise error_response("必须给 path（本地 .whl 的绝对路径）", status=400)
        try:
            return success(install_wheel(wheel, data_root))
        except FileNotFoundError as exc:
            raise error_response(str(exc), status=404)
        except ValueError as exc:
            raise error_response(str(exc), status=400)

    @app.post("/api/env/install-wheels-dir")
    async def env_install_wheels_dir(payload: Dict[str, Any] = None):
        """一键安装 `skillenv/wheels` 目录里的全部 .whl（给不会敲命令的用户）。"""
        from core.env_ext import install_wheels_dir
        return success(install_wheels_dir(data_root))

    @app.post("/api/env/probe-import")
    async def env_probe_import(payload: Dict[str, Any] = None):
        """真 import 一次（含版本/报错）——「装上」不等于「能用」，ABI 不匹配只能这样暴露。"""
        from core.env_ext import activate, probe_import
        activate(data_root)
        module = str((payload or {}).get("module") or "").strip()
        if not module:
            raise error_response("必须给 module（import 名，例如 cv2）", status=400)
        return success(probe_import(module))

    @app.get("/api/providers")
    async def providers():
        from core.adapters import ADAPTERS
        entries = list_providers()
        for entry in entries:
            adapter = ADAPTERS.get(entry["name"])
            entry["default_model"] = str(getattr(adapter, "DEFAULT_MODEL", "") or "")
            entry["base_url"] = str(getattr(adapter, "BASE_URL", "")
                                    or getattr(adapter, "API_BASE", "") or "")
        return success({"providers": entries})

    @app.get("/api/llm/providers")
    async def script_llm_providers():
        """Expose the endpoints actually consumed by script generation, without credentials."""
        from core.llm import PROVIDER_ENDPOINTS
        from core.model_catalog import LLM_CATALOG
        entries = []
        for name, config in PROVIDER_ENDPOINTS.items():
            catalog = LLM_CATALOG.get(name, {})
            entries.append({"name": name, "display_name": catalog.get("display") or name,
                            "default_model": config.get("default_model", ""),
                            "base_url": config["base_url"], "path": config["path"],
                            "available_models": [model["id"] for model in catalog.get("models", [])
                                                 if isinstance(model, dict) and model.get("id")]})
        entries.append({"name": "ollama", "display_name": "Ollama", "base_url": "http://localhost:11434",
                        "path": "/api/chat", "default_model": "qwen2.5:7b", "available_models": []})
        return success({"providers": entries})

    # ──────────── 项目 ────────────

    @app.post("/api/projects")
    async def create_project(req: ProjectCreate):
        project = db.create_project(
            title=req.title, description=req.description, settings=req.settings,
        )
        return success(project)

    @app.get("/api/projects")
    async def list_projects(limit: int = Query(100)):
        return success({"projects": db.list_projects(limit)})

    @app.get("/api/projects/{pid}")
    async def get_project(pid: str):
        project = db.get_project(pid)
        if not project:
            raise error_response("Project not found", status=404)
        return success(project)

    @app.patch("/api/projects/{pid}")
    async def update_project(pid: str, req: ProjectUpdate):
        kwargs = req.model_dump(exclude_unset=True)
        project = db.update_project(pid, **kwargs)
        if not project:
            raise error_response("Project not found", status=404)
        return success(project)

    @app.delete("/api/projects/{pid}")
    async def delete_project(pid: str):
        if not db.delete_project(pid):
            raise error_response("Project not found", status=404)
        return success({"deleted": True})

    @app.get("/api/projects/{pid}/pace")
    async def project_pace(pid: str):
        """镜长体检：平均几秒一镜、比目标档位慢多少、按目标该切几镜。

        ★ 2026-10-24（用户反馈「倍速好慢」）：以前只有"总时长对不对"的检查，
          总时长对得上但 5 个 12 秒长镜拼 60 秒，节奏依然是错的。
          这个接口直接量**切点密度**，出片前就能看见"慢"。
        """
        from core import scriptplan as _sp
        proj = db.get_project(pid)
        if not proj:
            raise error_response("Project not found", status=404)
        settings = proj.get("settings") or {}
        if isinstance(settings, str):
            try:
                settings = json.loads(settings) or {}
            except Exception:  # noqa: BLE001
                settings = {}
        pacing = str(settings.get("pacing") or "") or None
        shots = db.list_shots(pid) or []
        report = _sp.pacing_report(shots, pacing)
        report["project_id"] = pid
        report["settings_pacing"] = pacing
        return success(report)

    # ──────────── 剧本 ────────────

    @app.get("/api/projects/{pid}/script")
    async def get_script(pid: str):
        return success(db.get_script(pid))

    @app.post("/api/projects/{pid}/script/generate")
    async def generate_script(pid: str, req: ScriptGenerateRequest, background_tasks: BackgroundTasks):
        # 同步等待结果（前端用 SSE 流式进度更好，这里先简化）
        settings = db.get_all_settings()

        # 检测是否配置了 LLM key
        llm_provider = settings.get("llm_provider") or settings.get("remote_llm_provider")
        llm_keys = settings.get("llm_api_keys", {})
        use_local = settings.get("enable_llm_local")
        if not llm_provider:
            raise error_response(
                "尚未配置 LLM Provider。点右上角设置 → AI 对话模型 → 选 Provider 并填 API Key",
                status=400,
            )
        if not use_local and not llm_keys.get(llm_provider):
            raise error_response(
                f"LLM Provider `{llm_provider}` 未填 API Key。点右上角设置 → AI 对话模型 → 填 `{llm_provider}` 的 Key",
                status=400,
            )

        payload = req.model_dump()
        payload["project_id"] = pid
        task_id = db.create_task("generate_script", payload)
        payload["_task_id"] = task_id

        llm = _make_llm(settings)
        svc = ScriptService(llm)

        try:
            # ★★ 2026-09-14：场次数由**我们**定（`scriptplan`），不交给 LLM 估。
            #   用户实测「选 5 秒却生成 60 秒」——旧实现只在提示词里"请求"模型
            #   按时长写，没有任何强制。现在提示词里给死场次数与每场秒数，
            #   返回后再由 `fit_scenes` 强制重排，并对不上的地方**如实报告**。
            from core import scriptplan as _sp
            # ★ 2026-10-24（用户反馈「视频倍速好慢」）：场次数按**节奏档位**算，
            #   不再固定 12 秒/镜。档位来源优先级：本次显式指定 > 项目设置 > 意图/风格推断。
            #   实测旧默认：60 秒只有 5 镜（12s/镜），而短剧正常是 18~24 镜（2.5s/镜）。
            _proj_pacing = ""
            try:
                _p0 = db.get_project(pid) or {}
                _s0 = _p0.get("settings") or {}
                if isinstance(_s0, str):
                    import json as _json0
                    _s0 = _json0.loads(_s0) or {}
                _proj_pacing = str(_s0.get("pacing") or "")
            except Exception:  # noqa: BLE001
                _proj_pacing = ""
            _pacing = _sp.pacing_for(style=req.style, intent=req.user_prompt or "",
                                    explicit=str(getattr(req, "pacing", "") or "") or _proj_pacing)
            _psych = _sp.pacing_spec(_pacing)
            _want_n = _sp.scene_count_for(int(req.total_duration_seconds), pacing=_pacing)
            logger.info("节奏档位=%s（%s），%s 秒 → %s 镜",
                        _pacing, _psych.get("label"),
                        req.total_duration_seconds, _want_n)
            # ★ 2026-10-23：把 **settings**（用户手册/提示词规范）与**用户原话意图**
            #   传进剧本生成 —— 之前两者都没进去，所以剧本"不围绕意图""不遵照规范"。
            # ★ 用户**刚输入的**话优先（实测踩过：反过来用项目旧标题，
            #   用户这次说"产品宣传片"、项目却叫"雪夜"，模型就会照旧标题写剧情）。
            _intent = str(req.user_prompt or "").strip()
            try:
                _proj0 = db.get_project(pid) or {}
                if not _intent:
                    _intent = (str(_proj0.get("description") or "").strip()
                               or str(_proj0.get("title") or "").strip())
            except Exception:  # noqa: BLE001
                pass
            result = await svc.generate_script(
                user_prompt=req.user_prompt,
                total_duration=req.total_duration_seconds,
                style=req.style,
                auto_split=req.auto_split_scenes,
                scene_count=_want_n,
                settings=settings,
                intent=_intent,
            )
            # ★ 强制：总时长 = 用户要的秒数（做不到就裁场次）
            fit = _sp.fit_scenes(result.get("scenes"), int(req.total_duration_seconds), pacing=_pacing)
            if fit.get("scenes"):
                result["scenes"] = fit["scenes"]
            # 把"目标/实际/被裁掉的场次"一起存进 outline，剧本工作台要显示它
            if isinstance(result, dict):
                result["duration_plan"] = _sp.duration_brief(fit)
                result["duration_plan"]["pacing"] = _pacing
                result["duration_plan"]["pacing_label"] = _psych.get("label")
                result["duration_plan"]["shot_seconds"] = _psych.get("seconds")
            from core.method_gate import audit_final_script
            from core.scriptskills import genre_of
            result = audit_final_script(result, settings=settings,
                                        genre=genre_of(_intent),
                                        pacing=_pacing, total=req.total_duration_seconds,
                                        phase="final")
            if ((result.get("_method_gate_report") or {}).get("duration_validation") or {}).get("ok") is False:
                raise ValueError("剧本场次或时长不满足目标，请调整后重试；已停止后续生成，未提交视频任务")
            # ★ 目标时长**存进项目设置** —— 这是"选了 5 秒却给 60 秒"的另一个断点：
            #   主页选完没有落库，之后剧本页/分镜页一律 `|| 60`。
            try:
                _proj = db.get_project(pid) or {}
                _pset = dict(_proj.get("settings") or {})
                if isinstance(_pset, str):
                    try:
                        _pset = json.loads(_pset) or {}
                    except Exception:
                        _pset = {}
                _pset["default_total_duration"] = int(req.total_duration_seconds)
                _pset["pacing"] = _pacing
                _pset["shot_seconds"] = float(_psych.get("seconds") or 12)
                db.update_project(pid, settings=_pset)
            except Exception as _e:
                logger.warning("把总时长写进项目设置失败：%s", _e)

            # 生成三层提示词（用**已重排**的秒数 —— 否则分镜时长又会跟目标对不上）
            three_layer = {}
            for scene in result.get("scenes", []):
                scene_id = f"scene_{scene['scene_number']}"
                layer = await svc.generate_three_layer_prompt(
                    scene, result.get("characters", []),
                    next((s for s in result.get("scenes_meta", []) if s.get("name") == scene.get("title")), None),
                )
                three_layer[scene_id] = layer

            db.upsert_script(
                project_id=pid,
                user_prompt=req.user_prompt,
                outline=json.dumps(result, ensure_ascii=False),
                scenes=result.get("scenes", []),
                three_layer_prompts=three_layer,
            )
            db.update_task(task_id, status="completed", progress=1.0,
                            result={"title": result.get("title"), "scenes_count": len(result.get("scenes", []))},
                            finished_at=datetime.now().isoformat())
            return success({"title": result.get("title"),
                            "scenes_count": len(result.get("scenes", [])),
                            "duration": _sp.duration_brief(fit)})
        except Exception as e:
            logger.exception("Script generation failed")
            db.update_task(task_id, status="failed", error=str(e),
                            finished_at=datetime.now().isoformat())
            raise error_response(str(e), status=500)

    # ───── 剧本体检（按用户提供的成品剧本量出来的写法检查）─────

    @app.get("/api/projects/{pid}/script/audit")
    async def audit_project_script(pid: str):
        """给这份剧本"体检"：标题钩子 / 每场镜头+台词 / 前史动机 / 反派行为 /
        群像摇摆 / 金句收尾 / 时长对齐。

        ★ 依据是用户提供的那份**真实成品剧本**
          （《40集农村微短剧剧本：寒潮抢收玉米》，40 集全量实测签名），
          见 `core/scriptaudit.py` 顶部注释。方法只有能被检验，才算真的融进去。
        """
        script = db.get_script(pid)
        if not script:
            raise error_response("这个项目还没有剧本", status=404)
        from core import scriptaudit as _sa
        proj = db.get_project(pid) or {}
        pset = proj.get("settings") or {}
        if isinstance(pset, str):
            try:
                pset = json.loads(pset) or {}
            except Exception:
                pset = {}
        target = pset.get("default_total_duration")
        try:
            target = int(target) if target else None
        except Exception:
            target = None
        _res = _sa.audit_project_script(script, target)
        # ★ 2026-10-24：导演门（节奏可判定项）——与剧本体检并列返回，不改原结构
        try:
            from core.director_gate import audit_script as _dg_audit
            _scenes = script.get("scenes") or []
            if isinstance(_scenes, str):
                try:
                    _scenes = json.loads(_scenes or "[]")
                except Exception:  # noqa: BLE001
                    _scenes = []
            _pacing = str(pset.get("pacing") or "") or "promo"
            _res["director"] = _dg_audit({"scenes": _scenes}, pacing=_pacing, total=target)
        except Exception as _e:  # noqa: BLE001
            logger.warning("导演门（剧本体检）失败（不影响原体检）：%s", _e)
        return success(_res)

    # ───── 剧本场次细节（按 Skill 手册场次模板）─────

    @app.post("/api/projects/{pid}/script/scenes/{scene_number}/generate-detail")
    async def generate_scene_detail(pid: str, scene_number: int):
        """为指定场次生成按秒动作/对白/光影等结构化细节。"""
        script = db.get_script(pid)
        if not script or not script.get("scenes"):
            raise error_response("该项目还没有剧本", status=404)
        scene = next((s for s in script["scenes"] if s.get("scene_number") == scene_number), None)
        if not scene:
            raise error_response(f"未找到第 {scene_number} 场", status=404)

        chars = []
        try:
            chars = db.list_characters(pid) or []
        except Exception:
            pass

        scenes_meta = []
        if script.get("outline"):
            try:
                outline = json.loads(script["outline"]) if isinstance(script["outline"], str) else script["outline"]
                scenes_meta = outline.get("scenes_meta", []) if isinstance(outline, dict) else []
            except Exception:
                pass
        scene_ctx = next((s for s in scenes_meta if s.get("name") == scene.get("title")), None)

        settings = db.get_all_settings()
        llm = _make_llm(settings)
        svc = ScriptService(llm)

        try:
            detail = await svc.generate_scene_detail(scene, chars, scene_ctx)
            saved = db.upsert_scene_detail(pid, scene_number, detail)
            return success(saved)
        except Exception as e:
            logger.exception("Scene detail generation failed")
            raise error_response(str(e), status=500)

    @app.post("/api/projects/{pid}/script/scene-details/generate-all")
    async def generate_all_scene_details(pid: str, force: bool = False):
        """一次把**所有场次**的细节生成完（逐场调用 LLM，已存在的默认跳过）。

        用户反馈"剧本细节生成总是报错"——前端原来是逐场点、且用默认 20 秒超时，
        必然超时失败。这里做成一次请求、逐场推进，前端给长超时 + 进度。
        """
        script = db.get_script(pid)
        if not script or not script.get("scenes"):
            raise error_response("该项目还没有剧本", status=404)
        chars = []
        try:
            chars = db.list_characters(pid) or []
        except Exception:
            pass
        scenes_meta = []
        if script.get("outline"):
            try:
                outline = json.loads(script["outline"]) if isinstance(script["outline"], str) else script["outline"]
                scenes_meta = outline.get("scenes_meta", []) if isinstance(outline, dict) else []
            except Exception:
                pass

        settings = db.get_all_settings()
        llm = _make_llm(settings)
        svc = ScriptService(llm)
        existing = {d.get("scene_number") for d in (db.list_scene_details(pid) or [])}

        done, skipped, failed = [], [], []
        for scene in script["scenes"]:
            num = scene.get("scene_number")
            if not num:
                continue
            if not force and num in existing:
                skipped.append(num)
                continue
            scene_ctx = next((s for s in scenes_meta if s.get("name") == scene.get("title")), None)
            try:
                detail = await svc.generate_scene_detail(scene, chars, scene_ctx)
                db.upsert_scene_detail(pid, num, detail)
                done.append(num)
            except Exception as e:
                logger.warning("第 %s 场细节生成失败：%s", num, e)
                failed.append({"scene_number": num, "error": str(e)[:200]})
        return success({"generated": done, "skipped": skipped, "failed": failed,
                        "total": len(script["scenes"])})

    @app.get("/api/projects/{pid}/script/scene-details")
    async def list_scene_details(pid: str):
        return success({"details": db.list_scene_details(pid)})

    @app.get("/api/projects/{pid}/script/scenes/{scene_number}/detail")
    async def get_scene_detail(pid: str, scene_number: int):
        detail = db.get_scene_detail(pid, scene_number)
        if not detail:
            raise error_response("未生成细节", status=404)
        return success(detail)

    # ──────────── 角色 ────────────

    @app.post("/api/characters")
    async def create_character(req: CharacterCreate):
        char = db.create_character(**req.model_dump())
        return success(char)

    @app.get("/api/projects/{pid}/characters")
    async def list_characters(pid: str):
        return success({"characters": db.list_characters(pid)})

    # ★★ 2026-10-21：把某个角色标成「基准角色」。
    #
    #   为什么需要：一致性基准原来**只**用"项目内多数派"
    #   （core.consistency.canonical_from_shots）。但用户**主动给定**的
    #   角色（上传了参考图 / 明确写了长相）如果只出现 1 次，
    #   多数派会把它当少数派忽略 —— 用户的意图被稀释。
    #
    #   标了之后：
    #     · collect_refs() 会**每一镜**都带上这个角色的参考图
    #       （哪怕分镜的 character_ids 漏挂了）
    #     · 角色提示词也会优先用这份设定
    #   这就是「角色一致性以用户给定的为准」的落地点。
    @app.post("/api/projects/{pid}/characters/{cid}/canonical")
    async def set_character_canonical(pid: str, cid: str,
                                       payload: Dict[str, Any] = None):
        payload = payload or {}
        flag = payload.get("is_canonical")
        want = True if flag is None else bool(flag)
        char = db.get_character(cid)
        if not char or char.get("project_id") != pid:
            raise error_response("角色不存在", status=404)
        updated = db.set_character_canonical(cid, want)
        if not updated:
            raise error_response("标记失败", status=500)
        return success(updated)

    @app.get("/api/projects/{pid}/characters/canonical")
    async def list_canonical_characters(pid: str):
        """项目里被用户标为基准的角色。"""
        return success({"characters": db.get_canonical_characters(pid)})

    # 场景同理（用户可以指定"这个场景是基准"）
    @app.post("/api/projects/{pid}/scenes/{sid}/canonical")
    async def set_scene_canonical(pid: str, sid: str,
                                   payload: Dict[str, Any] = None):
        payload = payload or {}
        flag = payload.get("is_canonical")
        want = True if flag is None else bool(flag)
        sc = db.get_scene(sid)
        if not sc or sc.get("project_id") != pid:
            raise error_response("场景不存在", status=404)
        updated = db.set_scene_canonical(sid, want)
        if not updated:
            raise error_response("标记失败", status=500)
        return success(updated)

    @app.get("/api/projects/{pid}/scenes/canonical")
    async def list_canonical_scenes(pid: str):
        return success({"scenes": db.get_canonical_scenes(pid)})

    # ──────────── 资产库（跨剧本复用）+ 与剧本联动 ────────────

    @app.get("/api/library/characters")
    async def library_characters(exclude_project: Optional[str] = None):
        """资产库：所有历史角色（供其他剧本选择性复用）"""
        return success({"characters": db.list_all_characters(exclude_project=exclude_project)})

    @app.get("/api/library/scenes")
    async def library_scenes(exclude_project: Optional[str] = None):
        """资产库：所有历史场景"""
        return success({"scenes": db.list_all_scenes(exclude_project=exclude_project)})

    # ──────────── 图像生成（角色形象 / 场景概念图）────────────

    @app.get("/api/image/providers")
    async def image_providers():
        """可用的图像生成模型（MiniMax / 通义万相 / OpenAI 兼容）。

        ★ 每条都带上 `has_key` / `usable`：以前前端只拿到名字，
        用户在下拉里选了一个**没配 Key** 的模型，点生成后静默失败、回落到文字卡，
        界面上完全看不出原因 —— 这就是"图像模型是摆设"的由来。
        另外补上每个模型"适合什么"，让选择有意义而不是碰运气。
        """
        settings = db.get_all_settings()
        provs = list_image_providers()
        default = settings.get("image_provider") or "minimax"

        # 每个模型的定位说明（用户看得懂，而不是一串模型 ID）
        NOTES = {
            "minimax": {"best_for": "中文提示词理解好、出图快，默认首选",
                        "note": "复用你已配的 MiniMax Key，不用另外申请。"},
            "qwen": {"best_for": "中文场景/古风/写实人物，画面干净",
                     "note": "走阿里云 DashScope，需要单独配 DashScope 的 Key。"},
            "wanx": {"best_for": "同通义万相（qwen 的别名）",
                     "note": "与「通义万相」是同一个服务。"},
            "openai": {"best_for": "英文提示词、插画与概念图",
                       "note": "需要 OpenAI Key，且国内网络需可访问。"},
        }
        for p in provs:
            _, key = _resolve_image_credentials(settings, {"provider": p["name"]})
            p["has_key"] = bool(key)
            p["usable"] = bool(key)
            p["requires_api_key"] = True
            meta = NOTES.get(p["name"], {})
            p["best_for"] = meta.get("best_for", "")
            p["note"] = meta.get("note", "")
            if not key:
                p["unusable_reason"] = (
                    f"没找到「{p['display_name']}」的 API Key。"
                    f"到「设置 → 对话 API Keys」填上就能用。")
        return success({"providers": provs, "default_provider": default})

    @app.post("/api/image/test")
    async def test_image_provider(payload: Dict[str, Any] = None):
        """用一张很小的测试图验证「这个图像模型到底能不能用」。

        为什么需要：图像模型只在一个很窄的条件下才会被用到
        （分镜没有现成画面 且 勾了「缺画面时 AI 生成」），
        用户选完模型后往往**要跑完一整个分镜**才知道它行不行。
        这里给一个按钮，几秒出结果，失败就把厂商原始报错原样返回。
        """
        payload = payload or {}
        settings = db.get_all_settings()
        provider, api_key = _resolve_image_credentials(settings, payload)
        if not api_key:
            raise error_response(
                f"没找到「{provider}」的 API Key。到「设置 → 对话 API Keys」填上就能用。",
                status=400)
        prompt = (payload.get("prompt") or "").strip() or \
            "一张用于连通性测试的图：纯色背景中央一个简洁的几何图形，无文字，无水印"
        cache_root = settings.get("cache_dir") or os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
        out_dir = os.path.join(cache_root, "images", "_selftest")
        size = payload.get("size") or "1024x1024"
        t0 = time.time()
        result = await generate_image(
            provider=provider, api_key=api_key, prompt=prompt, out_dir=out_dir,
            size=size, # ★ 2026-10-20：尊重「设置」里选的图像模型（原来只看本次请求传的）
            model=payload.get("model") or settings.get("image_model") or "",
            base_url=payload.get("base_url") or "", timeout=120.0)
        if not result.get("ok"):
            return success({"ok": False, "provider": provider,
                            "error": result.get("error") or "生成失败",
                            "elapsed": round(time.time() - t0, 1)})
        return success({
            "ok": True, "provider": provider, "model": result.get("model"),
            "path": result.get("path"),
            "url": f"/api/local-image?path={quote(str(result.get('path') or ''))}",
            "size": size, "elapsed": round(time.time() - t0, 1),
            "note": "图像模型可用。生成分镜画面时会用这个模型。",
        })

    @app.get("/api/local-image")
    async def local_image(path: str):
        """安全返回本地图片（限制在数据/缓存/输出目录内）—— WebView 中 file:// 常被禁用，故走 HTTP"""
        settings = db.get_all_settings()
        roots = [
            os.path.abspath(str(settings.get("cache_dir") or "")),
            os.path.abspath(str(settings.get("output_dir") or "")),
            os.path.abspath(os.environ.get("VIDEOFORGE_DATA_DIR", "")),
        ]
        roots = [r for r in roots if r and r != os.path.abspath("")]
        target = os.path.abspath(path or "")
        if not target or not any(target.startswith(r + os.sep) or target == r for r in roots):
            raise error_response("路径不在允许的目录范围内", status=403)
        if not os.path.exists(target):
            raise error_response("图片文件不存在（可能已被清理）", status=404)
        return FileResponse(target)

    def _lookup_key_ci(keys: Any, provider: str) -> str:
        """大小写不敏感地查 provider 的 key（设置里可能存成 MiniMax / minimax / Qwen 等）"""
        if not isinstance(keys, dict):
            return ""
        p = (provider or "").strip().lower()
        for k, v in keys.items():
            if str(k).strip().lower() == p and v:
                return str(v).strip()
        return ""

    def _resolve_image_credentials(settings: Dict[str, Any], payload: Dict[str, Any]):
        """找图像模型要用的 Key。

        ★ 必须把 `tts_api_keys` 也算进来：硅基流动这类厂商是**账号级 Key**，
        语音合成、语音识别、图像生成共用同一把。用户已经在「语音合成 API Keys」
        填过了，如果这里不认，他就会遇到"我明明填了 Key 却说没配"
        —— 同一把 Key 让人填两遍是设计缺陷，不是用户的问题。
        """
        provider = (payload.get("provider") or settings.get("image_provider") or "minimax").strip()
        api_key = (payload.get("api_key") or "").strip()
        if api_key:
            return provider, api_key
        for bucket in ("llm_api_keys", "api_keys", "tts_api_keys"):
            k = _lookup_key_ci(settings.get(bucket) or {}, provider)
            if k:
                return provider, k
        # 硅基流动：语音那边存的名字可能就是这个 provider
        if provider.lower() == "siliconflow":
            for bucket in ("tts_api_keys", "llm_api_keys", "api_keys"):
                for name in ("siliconflow", "silicon", "硅基流动"):
                    k = _lookup_key_ci(settings.get(bucket) or {}, name)
                    if k:
                        return provider, k
        return provider, str(settings.get(f"image_api_key_{provider.lower()}") or "").strip()

    @app.post("/api/projects/{pid}/{kind}/{aid}/refine-image")
    async def refine_asset_image(pid: str, kind: str, aid: str,
                                 payload: Dict[str, Any] = None):
        """**真·后期 AI 再修改**：拿现有的图 + 一句话，让模型把图改掉。

        与「重新生成」的区别（这是用户明确要的能力）：
          - 重新生成 = 从提示词重画一张，长相/构图会变
          - 本接口   = **指令式图像编辑**（Qwen-Image-Edit 之类），
                       原图作为输入，只改你要求改的地方

        实测（`tests/test_siliconflow_image_edit.py`）：给一张画着色块的图，
        要求"把衣服换成灰蓝粗布军装、裤脚加绑腿；保持轮廓与背景不变"，
        模型真的换上了军装与绑腿，且背景与构图保持原样。

        body: {instruction, keep:[...], base_image?(路径，默认用资产当前形象),
               provider?, model?, reference2?(第二张参考图)}
        """
        payload = payload or {}
        kind = (kind or "").lower()
        if kind in ("characters", "character"):
            item = db.get_character(aid)
            is_char = True
        elif kind in ("scenes", "scene"):
            item = db.get_scene(aid)
            is_char = False
        else:
            raise error_response("kind 只能是 characters 或 scenes", status=400)
        if not item:
            raise error_response("资产不存在", status=404)
        if item.get("project_id") != pid:
            raise error_response("该资产不属于这个项目", status=400)

        instruction = str(payload.get("instruction") or "").strip()
        if not instruction:
            raise error_response("请写清楚「要改什么」", status=400)
        keep = payload.get("keep") or []
        if isinstance(keep, str):
            keep = [x.strip() for x in re.split(r"[，,、]", keep) if x.strip()]

        # 底图：优先用调用方给的，否则用资产当前形象
        base = str(payload.get("base_image") or "").strip() \
            or str(item.get("reference_image_path") or "")
        if not base or not os.path.exists(base):
            raise error_response(
                "没有可修改的底图。请先给这个资产生成一张形象图，或上传一张参考图。",
                status=400)

        settings = db.get_all_settings()
        provider = (payload.get("provider") or "siliconflow").strip()
        _, api_key = _resolve_image_credentials(settings, {"provider": provider})
        if not api_key:
            raise error_response(
                f"「{provider}」没有配置 API Key。图像编辑需要能接受参考图的模型"
                f"（推荐硅基流动的 Qwen-Image-Edit，用你已配的 SiliconFlow Key）。",
                status=400)

        # 组提示词：写清"改什么"+"保留什么"。实测只写"改什么"容易连带改掉别的。
        prompt = instruction
        if keep:
            prompt += "。必须保留：" + "、".join(str(k) for k in keep) + "。"
        prompt += "。只修改上面提到的部分，其余保持与参考图一致，不要改变构图与背景。"

        refs = [base]
        second = str(payload.get("reference2") or "").strip()
        if second and os.path.exists(second):
            refs.append(second)

        model = str(payload.get("model") or "").strip()
        if not model:
            from core.imagegen import IMAGE_PROVIDERS, is_edit_model
            conf = IMAGE_PROVIDERS.get(provider) or {}
            model = (conf.get("edit_models") or [conf.get("default_model") or ""])[0]
        from core.imagegen import is_edit_model as _is_edit
        if not _is_edit(model):
            raise error_response(
                f"「{model}」不是图像编辑模型，无法「在原图上修改」。"
                f"请选用带 Edit 的模型（如 Qwen/Qwen-Image-Edit-2509），"
                f"否则只能用「重新生成」——那会重画一张，长相和构图都会变。",
                status=400)

        cache_root = settings.get("cache_dir") or os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
        sub = "characters" if is_char else "scenes"
        out_dir = os.path.join(cache_root, "images", pid, sub, "refined")
        os.makedirs(out_dir, exist_ok=True)
        t0 = time.time()
        result = await generate_image(
            provider=provider, api_key=api_key, prompt=prompt, out_dir=out_dir,
            size=payload.get("size") or "", model=model,
            reference_images=refs, timeout=300.0)
        if not result.get("ok"):
            return success({"ok": False, "error": result.get("error") or "修改失败",
                            "provider": provider, "model": model,
                            "elapsed": round(time.time() - t0, 1)})

        # 保留旧图（改坏了能退回），新图写回资产
        old = item.get("reference_image_path") or ""
        if old and os.path.exists(old):
            try:
                shutil.copy2(old, os.path.join(out_dir, f"prev_{int(time.time())}_"
                                                    + os.path.basename(old)))
            except Exception:
                pass
        if is_char:
            db.update_character(aid, reference_image_path=result["path"])
        else:
            db.update_scene(aid, reference_image_path=result["path"])

        return success({
            "ok": True, "path": result["path"],
            "url": f"/api/local-image?path={quote(result['path'])}",
            "provider": provider, "model": model,
            "elapsed": round(time.time() - t0, 1),
            "applied": {"instruction": instruction, "keep": keep,
                        "base_image": base, "reference_count": len(refs)},
            "note": "已在原图基础上按要求修改（不是重画）。旧图已备份到 refined/prev_*。",
        })

    @app.post("/api/projects/{pid}/characters/{cid}/generate-image")
    async def gen_character_image(pid: str, cid: str, payload: Dict[str, Any] = None):
        """用图像模型生成角色形象图，并写回角色的 reference_image_path"""
        payload = payload or {}
        char = db.get_character(cid)
        if not char:
            raise error_response("角色不存在", status=404)
        settings = db.get_all_settings()
        provider, api_key = _resolve_image_credentials(settings, payload)
        if not api_key:
            raise error_response(
                f"未找到「{provider}」的 API Key。请到「设置 → 对话 API Keys」填写该 Provider 的 Key，"
                f"或在生成弹窗中临时填入。", status=400)

        # 提示词：优先用用户手填的；否则用结构化人物卡拼一条**能照着画**的提示词
        style = payload.get("style") or settings.get("default_style") or "cinematic"
        if not (payload.get("prompt") or "").strip():
            try:
                from core.assets import build_portrait_prompt
                merged = dict(char)
                try:
                    rf = json.loads(char.get("reference_features") or "{}")
                    if isinstance(rf, dict):
                        merged.update({k: v for k, v in rf.items() if v and not merged.get(k)})
                except Exception:
                    pass
                prompt = build_portrait_prompt(
                    merged, style=style,
                    view=payload.get("view") or "bust")
            except Exception as e:
                logger.warning("拼装角色提示词失败，回落简单模板：%s", e)
                prompt = (
                    f"角色设定图：{char.get('name', '')}。{char.get('description') or ''} "
                    f"服装：{char.get('costume_main') or '符合角色身份'}。"
                    f"正面半身像，单人，简洁背景，电影级光影，高清写实，构图干净"
                )
        cache_root = settings.get("cache_dir") or os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
        out_dir = os.path.join(cache_root, "images", pid, "characters")

        result = await generate_image(
            provider=provider, api_key=api_key, prompt=prompt, out_dir=out_dir,
            size=payload.get("size") or "1024x1024",
            # ★ 2026-10-20：尊重「设置」里选的图像模型（原来只看本次请求传的）
            model=payload.get("model") or settings.get("image_model") or "",
            base_url=payload.get("base_url") or "",
        )
        if not result.get("ok"):
            raise error_response(result.get("error") or "图像生成失败", status=400)

        updated = db.update_character(
            cid,
            reference_image_path=result["path"],
            reference_features=result.get("prompt", ""),
        )
        return success({**result, "character": updated})

    @app.post("/api/projects/{pid}/scenes/{sid}/generate-image")
    async def gen_scene_image(pid: str, sid: str, payload: Dict[str, Any] = None):
        """用图像模型生成场景概念图，并写回场景的 reference_image_path"""
        payload = payload or {}
        scene = db.get_scene(sid)
        if not scene:
            raise error_response("场景不存在", status=404)
        settings = db.get_all_settings()
        provider, api_key = _resolve_image_credentials(settings, payload)
        if not api_key:
            raise error_response(
                f"未找到「{provider}」的 API Key。请到「设置 → 对话 API Keys」填写该 Provider 的 Key。", status=400)

        prompt = (payload.get("prompt") or "").strip() or (
            f"场景概念图：{scene.get('name', '')}。{scene.get('description') or ''} "
            f"类型：{scene.get('location_type') or 'outdoor'}，光线：{scene.get('lighting') or 'natural'}，"
            f"时间：{scene.get('time_of_day') or 'day'}，天气：{scene.get('weather') or 'clear'}。"
            f"无人物，广角电影感，高清写实，构图干净"
        )
        cache_root = settings.get("cache_dir") or os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
        out_dir = os.path.join(cache_root, "images", pid, "scenes")

        # ★★ 场景图必须**按项目画幅**生成，不能一律方形（第三个真实事故）。
        #   以前这里是硬编码的 `or "1024x1024"`，于是所有场景图都是 1:1。
        #   而场景图会被当**视频首帧**传给海螺，海螺跟随首帧画幅出片 ——
        #   10 张方形场景图 = 10 个方形镜头（768x768 / 720x720 / 1080x1080），
        #   拼进 16:9 成片只能裁掉 44% 画面。源头在生成这一刻，就在这一刻修。
        _ar = "16:9"
        try:
            _proj = db.get_project(pid) or {}
            _ps = _proj.get("settings")
            if isinstance(_ps, str):
                _ps = json.loads(_ps or "{}")
            _ps = _ps if isinstance(_ps, dict) else {}
            # 分镜上的画幅最准（同一项目里可能逐镜设置），其次项目设置
            _shot_ar = next((s.get("aspect_ratio") for s in
                             (db.list_shots(pid) or []) if s.get("aspect_ratio")), "")
            _ar = (_shot_ar or _ps.get("default_aspect_ratio")
                   or (settings.get("art_direction") or {}).get("aspect_ratio")
                   or "16:9")
        except Exception:
            pass
        _size_by_ar = {"16:9": "1280x720", "9:16": "720x1280", "1:1": "1024x1024"}
        _size = payload.get("size") or _size_by_ar.get(str(_ar), "1024x1024")

        result = await generate_image(
            provider=provider, api_key=api_key, prompt=prompt, out_dir=out_dir,
            size=_size,
            # ★ 2026-10-20：尊重「设置」里选的图像模型（原来只看本次请求传的）
            model=payload.get("model") or settings.get("image_model") or "",
            base_url=payload.get("base_url") or "",
        )
        if not result.get("ok"):
            raise error_response(result.get("error") or "图像生成失败", status=400)

        updated = db.update_scene(
            sid,
            reference_image_path=result["path"],
            color_palette=result.get("prompt", ""),
        )
        return success({**result, "scene": updated})

    @app.post("/api/projects/{pid}/scenes/{sid}/generate-video")
    async def generate_scene_video(pid: str, sid: str, payload: Dict[str, Any] = None):
        """给场景生成一段**环境空镜视频**（走视频 API 模型）。

        按用户要求：场景本身也可能需要视频（空镜/建立镜头），
        这时就调用视频模型；没有可用 Key 或调用失败时自动回退本地运镜合成，
        保证一定拿得到可播放的 mp4。
        """
        payload = payload or {}
        sc = db.get_scene(sid)
        if not sc:
            raise error_response("场景不存在", status=404)
        settings = db.get_all_settings()

        # 组装提示词：场景设定 + 美术基调（严格来自剧本，不额外脑补）
        try:
            from core.assets import build_scene_prompt
            art = {}
            raw = settings.get("art_direction")
            if isinstance(raw, str):
                try:
                    art = json.loads(raw)
                except Exception:
                    art = {}
            prompt = (payload.get("prompt") or "").strip() or build_scene_prompt(
                sc, style=(art.get("style_id") or settings.get("default_style") or "cinematic"))
        except Exception:
            prompt = (payload.get("prompt") or "").strip() or f"场景空镜：{sc.get('name')}。{sc.get('description') or ''}"

        provider = payload.get("model_provider") or settings.get("default_model_provider") or "seedance"
        model_name = payload.get("model_name") or settings.get("default_model_name") or \
            DEFAULT_MODEL_IDS.get(provider, "")
        duration = max(5, int(payload.get("duration_seconds") or 5))
        api_key = _lookup_key_ci(settings.get("api_keys") or {}, provider)
        mode = (payload.get("mode") or "auto").lower()
        want_api = mode in ("auto", "api") and bool(api_key)

        out_dir = os.path.join(_outputs_root(), pid, "scenes")
        os.makedirs(out_dir, exist_ok=True)
        warnings: List[str] = []

        if want_api:
            try:
                from core.adapters import get_adapter, VideoGenRequest, to_image_ref
                adapter = get_adapter(provider, api_key=api_key, config={"model_name": model_name})
                ref = to_image_ref(sc.get("reference_image_path") or "")
                # ★ 视频参考（白膜/Previz 驱动运镜）。从场景的
                #   reference_features.video_reference.path 读白膜视频路径，
                #   转成可被 adapter 接受的 URL 形式（to_image_ref 同样支持
                #   视频路径，会按需上传到厂商 CDN）。adapter 端会按模型能力
                #   决定是否真把 video_url 传给后端（仅 H3 / Seedance 2.5 等
                #   真支持视频参考的模型会用；其它模型会清晰报错而不是静默丢）。
                _vref = (sc.get("reference_features") or {}).get("video_reference") or {}
                _video_path = _vref.get("path") or ""
                video_url = to_image_ref(_video_path) if _video_path else None
                req = VideoGenRequest(
                    prompt=prompt,
                    duration=max(adapter.min_duration or 1, min(duration, adapter.max_duration or 10)),
                    aspect_ratio=payload.get("aspect_ratio") or "16:9",
                    resolution=payload.get("resolution") or "1080p",
                    first_frame=ref or None,
                    video_url=video_url or None,
                )
                verr = adapter.validate_request(req)
                if verr:
                    raise RuntimeError(f"请求参数不合法：{verr}")
                result = await asyncio.wait_for(adapter.generate(req), timeout=900)
                try:
                    await adapter.close()
                except Exception:
                    pass
                if not result.success:
                    raise RuntimeError(result.error or "视频模型返回失败")
                local = os.path.join(out_dir, f"{sid}_{int(time.time())}.mp4")
                await adapter.download_to_local(result.video_url, local)
                if os.path.exists(local) and os.path.getsize(local) > 10240:
                    db.update_scene(sid, color_palette=prompt)
                    return success({"ok": True, "source": "api", "provider": provider,
                                    "model": model_name, "path": local,
                                    "url": f"/api/media?path={quote(local)}",
                                    "duration": result.duration_seconds or duration,
                                    "prompt": prompt})
                raise RuntimeError("模型返回成功但文件未能下载")
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                logger.warning("场景视频走真实模型失败：%s", err)
                if mode == "api":
                    raise error_response(
                        f"真实模型失败：{err}\n建议：{_api_error_hint(provider, str(e))}", status=400)
                warnings.append(f"真实模型（{provider}）失败，已改用本地运镜合成：{err}")

        # 本地兜底：用场景图 + 运镜合成一段空镜
        try:
            from core.compose import render_clip, make_text_card, _res_from_shot, probe_duration
            img = sc.get("reference_image_path") or ""
            w, h = (1280, 720) if (payload.get("aspect_ratio") or "16:9") == "16:9" else (720, 1280)
            if not img or not os.path.exists(img):
                img = os.path.join(out_dir, f"card_{sid}.png")
                make_text_card(img, title=sc.get("name") or "场景",
                               body=(sc.get("description") or "")[:120], w=w, h=h, badge="场景")
                warnings.append("该场景没有画面，已用文字卡兜底（建议先「AI 生成场景图」）")
            out = os.path.join(out_dir, f"{sid}_local_{int(time.time())}.mp4")
            warnings.extend(await render_clip(img, "", duration, out, w, h, 30, True))
            if not os.path.exists(out) or os.path.getsize(out) < 10240:
                raise RuntimeError("本地合成未产出有效文件")
            db.update_scene(sid, color_palette=prompt)
            return success({"ok": True, "source": "local", "path": out,
                            "url": f"/api/media?path={quote(out)}",
                            "duration": await probe_duration(out) or duration,
                            "prompt": prompt, "warnings": warnings})
        except Exception as e:
            logger.exception("场景视频本地合成失败")
            raise error_response(f"生成场景视频失败：{e}", status=500)

    @app.post("/api/projects/{pid}/characters/import")
    async def import_characters(pid: str, payload: Dict[str, Any]):
        """从资产库把选中的角色复制进当前项目（保留原内容，生成新记录）"""
        ids = payload.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise error_response("请提供要导入的角色 ids 列表", status=400)
        created = db.clone_characters(ids, pid)
        return success({"imported": created, "count": len(created)})

    @app.post("/api/projects/{pid}/scenes/import")
    async def import_scenes(pid: str, payload: Dict[str, Any]):
        """从资产库把选中的场景复制进当前项目"""
        ids = payload.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise error_response("请提供要导入的场景 ids 列表", status=400)
        created = db.clone_scenes(ids, pid)
        return success({"imported": created, "count": len(created)})

    # ──────────── 单分镜出片（「生成」页按钮的真实实现）────────────

    _shot_render_state: Dict[str, Any] = {}

    def _shots_out_dir(pid: str) -> str:
        return os.path.join(_outputs_root(), pid, "shots")

    def _script_sources_for(pid: str) -> List[Dict[str, Any]]:
        """这一项目的**所有**剧本版本里"有台词的场"（用于给分镜回填台词）。

        ★ 用户实测「第一个镜头没有声音（剧本是有对话的）」的真实原因是：
          分镜的 `layer2_timeline` 里没有 dialogue 字段，而剧本那一场写着对话。
          这里把两版剧本（生成分镜用的 `scripts.scenes` + 工作台当前稿
          `script_scene_details`）都交给 `core.scriptlines`，
          **由它按"说话人是否真的出现在这一镜里"决定填不填**。
        """
        try:
            from core import scriptlines as _slg
            return _slg.collect_scene_sources(db.get_script(pid),
                                              db.list_scene_details(pid))
        except Exception as e:
            logger.warning("收集剧本台词失败（不做回填）：%s", e)
            return []

    def _shot_script_ctx(shot: Dict[str, Any]) -> Dict[str, Any]:
        """一镜回填台词要用的上下文：场景名 + 本镜角色名。"""
        name = ""
        try:
            sid = shot.get("scene_id")
            if sid:
                for sc in (db.list_scenes(shot.get("project_id")) or []):
                    if sc.get("id") == sid:
                        name = str(sc.get("name") or "")
                        break
        except Exception:
            name = ""
        names: List[str] = []
        try:
            ids = shot.get("character_ids")
            if isinstance(ids, str):
                try:
                    ids = json.loads(ids)
                except Exception:
                    ids = []
            wanted = {str(i) for i in (ids or [])}
            for c in (db.list_characters(shot.get("project_id")) or []):
                if str(c.get("id")) in wanted:
                    names.append(str(c.get("name") or ""))
        except Exception:
            pass
        return {"scene_name": name, "character_names": names}

    async def _measure_shot_audio(shot: Dict[str, Any], settings: dict,
                                  characters: Optional[List[Dict[str, Any]]] = None) -> float:
        """实测这个分镜的台词**念完要多久** → 交给 `plan_shot` 当音频主轴。

        ★ 为什么必须实测而不是估算：`duration_seconds` 是规划稿，不是事实。
          MoneyPrinterTurbo 整条流水线就干一件事 —— 量出配音音频的真实长度，
          然后让画面去盖住它。我们把它用到单个分镜这一层：
          台词只要 6.8s 时就不去要 10s，台词要 13.5s 时就规划成两段 10s。
          "分层定 10s 但模型只给 5s / 画面跟描述不相干"就是这么消掉的。
        细节见 `core.voicecast.measure_lines_duration`（逐句用它自己角色的音色量）
        """
        try:
            from core.dialogue import timeline_lines
            from core.voicecast import measure_lines_duration
            nominal = float(shot.get("duration_seconds") or 5)
            lines = timeline_lines(shot.get("layer2_timeline"), nominal)
            if not lines:
                return 0.0
            return float(await measure_lines_duration(
                lines, characters, settings or {}) or 0.0)
        except Exception as e:
            logger.info("实测分镜音频时长失败（按标称时长规划）：%s", e)
            return 0.0

    def _selected_video_of(shot: Dict[str, Any]) -> str:
        """取一个分镜**当前选中的**视频文件路径（没有就返回空）。"""
        cands = shot.get("candidates") or []
        if isinstance(cands, str):
            try:
                cands = json.loads(cands)
            except Exception:
                cands = []
        cands = [c for c in cands if isinstance(c, dict)]
        sel = next((c for c in cands if c.get("is_selected")), None) \
            or (cands[-1] if cands else None)
        p = (sel or {}).get("path") or ""
        return p if p and os.path.exists(p) else ""

    async def _chain_first_frame(shot: Dict[str, Any], settings: dict,
                                 all_shots: List[Dict[str, Any]]) -> Dict[str, Any]:
        """算出"这一镜要不要接上一镜的尾帧"，要的话把尾帧抽出来。

        ★ 这是解决"镜头之间一眼AI"最直接的一招：
          用上一镜的**尾帧**当这一镜的**首帧**（image-to-video），
          画面就从上一镜里"长出来"，而不是重新开一个毫不相干的镜头。

        参考项目的结论（都查过源码/文档）：
          - MoneyPrinterTurbo：明确**没有**这类处理
          - Pavo：只有首帧范式，**尾帧串联文档未提及**
        所以这里是自研。

        规则：
          1. 上一个镜头有可用视频 + 当前模型支持首帧 → 用它的尾帧
          2. 否则回退到场景图（保证场景一致性）
          3. 同场景内的镜头**优先串联**（一场戏内部的镜头最需要连贯）
        """
        out: Dict[str, Any] = {"path": "", "from_shot_id": "", "why": ""}
        try:
            from core.continuity import continuity_brief, supports_first_frame
            idx = next((i for i, s in enumerate(all_shots) if s["id"] == shot["id"]), -1)
            if idx <= 0:
                out["why"] = "全片第一个镜头，没有可承接的画面"
                return out
            prev = all_shots[idx - 1]
            prev_video = _selected_video_of(prev)
            provider = shot.get("model_provider") or ""
            model = shot.get("model_name") or ""
            # ★ 决策抽到 core.continuity.chain_decision（纯函数、可单测）。
            #   原来这段判断塞在闭包里，没法单独测 —— 而它是"像不像一镜到底"的总开关。
            from core.continuity import chain_decision
            # ★ 2026-10-24：把**地点**一起传进去 —— 1 场戏=1 镜时 scene_id 必不同，
            #   只有按地点判断才判得对"同空间换机位要不要接尾帧"。
            def _loc_of(_s: Dict[str, Any]) -> str:
                try:
                    _sc = db.get_scene(_s.get("scene_id")) if _s.get("scene_id") else None
                    if _sc:
                        return str(_sc.get("location") or _sc.get("name") or "").strip()
                except Exception:  # noqa: BLE001
                    pass
                return ""
            _dec = chain_decision(
                prev, shot,
                prev_has_video=bool(prev_video),
                model_supports_first_frame=supports_first_frame(provider, model),
                prev_location=_loc_of(prev), cur_location=_loc_of(shot))
            if not _dec.get("chain"):
                out["why"] = _dec.get("why") or "不接尾帧"
                out["skipped_reason"] = _dec.get("skipped_reason") or ""
                if _dec.get("skipped_reason") == "scene_changed":
                    logger.info("跨场景不串联：分镜 %s 的场景 %s ≠ %s",
                                shot.get("id"), str(prev.get("scene_id"))[:8],
                                str(shot.get("scene_id"))[:8])
                return out
            # 抽尾帧（按上一镜视频的 mtime+路径缓存，避免每次重抽）
            import hashlib
            key = hashlib.md5(
                f"{prev_video}|{os.path.getmtime(prev_video):.0f}".encode()).hexdigest()[:12]
            cache_root = settings.get("cache_dir") or os.path.join(
                os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
            dst = os.path.join(cache_root, "chain", shot.get("project_id") or "", f"{key}.jpg")
            if not os.path.exists(dst):
                from core.continuity import extract_last_frame
                got = extract_last_frame(prev_video, dst)
                if not got:
                    out["why"] = "抽上一镜尾帧失败（视频可能已损坏）"
                    return out
            out["path"] = dst
            out["from_shot_id"] = prev.get("id")
            out["why"] = (f"接上一镜（第 {(prev.get('order_index') or 0) + 1} 个）的尾帧，"
                          f"画面从这里继续")
            return out
        except Exception as e:
            logger.warning("尾帧串联计算失败：%s", e)
            out["why"] = f"尾帧串联不可用：{e}"
            return out

    async def _try_api_video(shot: Dict[str, Any], settings: dict,
                             provider: str, model_name: str,
                             duration_override: Any = None,
                             layers: Optional[Dict[str, bool]] = None,
                             chain: bool = True,
                             ref_priority: str = "auto",
                             with_voice: bool = True) -> Dict[str, Any]:
        """**直接**调用真实视频模型（不走任务队列），返回结构化结果。

        这样「生成」按钮可以先试真实模型，失败时立刻回退本地合成，
        而不是把错误丢进队列后用户什么都看不到。

        `ref_priority` 决定**用哪张图当首帧**（模型一般只吃一张图）：
          · `auto`（默认）—— 有上一镜尾帧就用它（衔接最好），否则用场景图；
          · `scene`       —— 永远用**场景图**（牺牲衔接，换"场景跟场景图一致"）；
          · `character`   —— 用**第一个角色的形象图**（换"人物跟角色图一致"）。
        为什么必须让用户能选：这是**真实的取舍**（一张图不可能同时保证
        衔接、场景一致、人物一致）。以前这个取舍是写死的、而且用户看不见。
        """
        import time as _t
        _generation_cost_recorded = False
        _paid_task_ids: List[str] = []
        try:
            from core.adapters import get_adapter, to_image_ref, VideoGenRequest
            # 相邻分镜（用于层间衔接：承接上一镜、为下一镜留白）
            _all: List[Dict[str, Any]] = []
            try:
                _all = sorted(db.list_shots(shot.get("project_id")) or [],
                              key=lambda s: s.get("order_index") or 0)
                _idx = next((i for i, s in enumerate(_all) if s["id"] == shot["id"]), -1)
                payload_ctx = {
                    "_prev_shot": _all[_idx - 1] if _idx > 0 else None,
                    "_next_shot": _all[_idx + 1] if 0 <= _idx < len(_all) - 1 else None,
                }
            except Exception:
                payload_ctx = {}
            from core.orchestrator import _compose_full_prompt
            api_key = _lookup_key_ci(settings.get("api_keys") or {}, provider)
            if not api_key:
                return {"ok": False, "error": f"未配置「{provider}」的 API Key"}
            adapter = get_adapter(provider, api_key=api_key, config={"model_name": model_name})
            if adapter is None:
                return {"ok": False, "error": f"未知的视频模型 provider：{provider}"}

            char_refs = []
            char_cards: List[Dict[str, Any]] = []
            _dangling_cids: List[str] = []
            for cid in (shot.get("character_ids") or []):
                c = db.get_character(cid)
                if not c:
                    # ★ 悬空引用（角色卡被删/重建）—— 以前**静默跳过**，
                    #   于是提示词里少了主体、参考图也不发，模型自己编一个人。
                    _dangling_cids.append(str(cid))
                    continue
                char_cards.append(c)
                if c.get("reference_image_path"):
                    r = to_image_ref(c["reference_image_path"])
                    if r:
                        char_refs.append(r)
            scene = None
            scene_ref = None
            scene_ref_raw = ""
            if shot.get("scene_id"):
                scene = db.get_scene(shot["scene_id"])
                if scene and scene.get("reference_image_path"):
                    scene_ref_raw = scene["reference_image_path"]
                    scene_ref = to_image_ref(scene_ref_raw)

            # ★★ 2026-09-14 **生成前的引用体检**（学自参考方法论的 `MISSING_REFERENCE` gate）：
            #   花这笔钱之前先确认"该出现的人确实被关联上了"。实测事故：
            #   分镜关联的是旧 AI 神 ID（卡已被重建）→ 提示词只剩 1 个主体
            #   → 模型自己编了个发光壮汉，用户看到"关联了角色却不对"。
            ref_check: Dict[str, Any] = {}
            try:
                from core import refcheck as _rc
                ref_check = _rc.check_shot_refs(
                    shot, db.list_characters(shot["project_id"]) or [],
                    db.list_scenes(shot["project_id"]) or [])
                if _dangling_cids and not ref_check.get("dangling_ids"):
                    ref_check = dict(ref_check)
                    ref_check["dangling_ids"] = _dangling_cids
                for _i in (ref_check.get("issues") or []):
                    if _i.get("level") == "error":
                        warn_list.append(f"⚠ {_i['message']}")
                if ref_check.get("severity") == "error":
                    warn_list.append(ref_check.get("hint") or "")
            except Exception as _e:
                logger.warning("生成前引用体检失败（继续生成）：%s", _e)

            dur = int(shot.get("duration_seconds") or 5)
            # ⚠ 「生成」页选的"每段时长"必须真的生效：
            #   旧代码只读分镜自身的 duration_seconds，页面上选的值被完全忽略，
            #   用户看到的就是"时长是个摆设"。
            if duration_override not in (None, "", "auto"):
                try:
                    dur = int(float(duration_override))
                except (TypeError, ValueError):
                    pass
            dur = max(5, dur)
            # ★★ 这里必须**按能力表吸附**，不能只用标量 max_duration。
            #    踩到的真事（线上实测 20 秒权游分镜）：分镜标称 20s、分辨率 1080p、
            #    模型 MiniMax-Hailuo-02（1080P 只允许 [6]）。
            #    标量算式给出 `min(20, max_duration=10) = 10`，看着没问题；
            #    但真正的约束是"1080P 下只允许 6s" ——
            #    于是下面那次**规划前**的 validate_request 直接被拒：
            #      "海螺 Hailuo 在 1080P 下只支持 6s，不支持 10s"
            #    而规划器本来会把分辨率降到 768P 去出 10s，**根本没机会跑到那一步**。
            #    也就是说：能力校验本身是对的，但**位置错了** ——
            #    它卡在了"还没规划"的时刻。这里先吸附成该分辨率下的合法值，
            #    让规划前的这次体检能通过；真正的请求时长由规划器 + 段级吸附决定。
            dur = _snap_legal_duration(
                adapter, provider, model_name,
                shot.get("resolution") or "720p", dur)

            # ⚠ 提示词必须按**视频模型**的习惯构造（连续镜头的电影化描述），
            #   而不是把分镜表 + 约束清单原样倒进去 —— 那样出来的是"念稿子"。
            #   角色外形/服装、场景环境**严格取自角色卡与场景卡**（不做 AI 自定义）。
            from core.videoprompt import build_video_prompt
            art: Dict[str, Any] = {}
            raw_art = settings.get("art_direction")
            if isinstance(raw_art, str):
                try:
                    art = json.loads(raw_art)
                except Exception:
                    art = {}
            elif isinstance(raw_art, dict):
                art = raw_art
            vp = build_video_prompt(
                shot, scene=scene, characters=char_cards, art=art,
                style=(art.get("style_id") or settings.get("default_style") or "cinematic"),
                provider=provider, duration=dur,
                aspect_ratio=shot.get("aspect_ratio") or "16:9",
                resolution=shot.get("resolution") or "720p",
                prev_shot=payload_ctx.get("_prev_shot"),
                next_shot=payload_ctx.get("_next_shot"),
                layers=layers or None,
                # ★ 有角色参考图 → 提示词里必须写明"只继承身份、排除原图构图/背景/光线"
                #   （Hell-Grind `reference-asset-control.md:64-81` /
                #    审计规则 `P-REFERENCE-SCOPE`）。参考图不写范围 = 不可控指令。
                ref_scope=bool(char_refs or scene_ref),
            )
            # ★ 首帧优先级：**上一镜的尾帧 > 场景图**
            #   为什么不是直接用场景图：场景图只保证"环境一致"，
            #   但镜头之间还是各拍各的；用上一镜的尾帧才能让画面"长出来"。
            #   这也正是"衔接一眼AI"最有效的解药。
            chain_info: Dict[str, Any] = {"path": "", "why": "未启用串联"}
            # ★★ 首帧必须**先归一到分镜画幅**（第二个真实事故）：
            #   项目里 10 张场景参考图全是 1024×1024（1:1），而分镜是 16:9。
            #   海螺在有首帧时**跟随首帧画幅**出片 —— 同模型同分辨率，
            #   没传首帧的出 1280×720，传了方形首帧的出 720×720 / 768×768 /
            #   1080×1080。这不是模型不听话，是我们喂了一张方图。
            _want_ar = shot.get("aspect_ratio") or "16:9"
            _ff_notes: List[str] = []
            first_ref = scene_ref
            if scene_ref_raw:
                try:
                    from core import aspect as _asp
                    _conf = _asp.conform_image(scene_ref_raw, _want_ar)
                    if _conf and _conf != scene_ref_raw:
                        _loss = _asp.crop_loss(*_asp.probe_size(scene_ref_raw), _want_ar)
                        first_ref = to_image_ref(_conf) or first_ref
                        _ff_notes.append(
                            f"场景参考图是 {_asp.probe_size(scene_ref_raw)[0]}x"
                            f"{_asp.probe_size(scene_ref_raw)[1]}（"
                            f"{_asp.classify(*_asp.probe_size(scene_ref_raw))}），"
                            f"与分镜画幅 {_want_ar} 不一致。已裁成 {_want_ar} 当首帧"
                            f"（丢掉约 {_loss * 100:.0f}% 画面）——"
                            f"海螺在有首帧时会跟着首帧的画幅出片，不裁就会整片变成方形。"
                            f"到「场景」页按项目画幅重新生成场景图可以免掉这一步裁切。")
                except Exception as _e:
                    logger.warning("首帧画幅归一化失败（沿用原图）：%s", _e)
            if chain:
                chain_info = await _chain_first_frame(shot, settings, _all)

            # ★★ 首帧到底用哪张图：**用户可选的取舍**（纯函数在适配器层，可单测）。
            #   一张首帧不可能同时保证"跟上一镜衔接""跟场景图一致""跟角色图一致"，
            #   以前这个取舍是写死的（串联 > 场景），用户看不到、也改不了。
            #   （此刻 `first_ref` 已经是"按分镜画幅裁好的场景图"。）
            from core.adapters import pick_first_frame as _pick_ff
            _chain_ref = ""
            if chain_info.get("path"):
                _chain_ref = to_image_ref(chain_info["path"]) or ""
            _pick = _pick_ff(scene_ref=first_ref or "", char_refs=char_refs,
                             chain_ref=_chain_ref,
                             scene_path=scene_ref_raw,
                             chain_path=chain_info.get("path") or "",
                             priority=ref_priority)
            if _pick["first_frame"]:
                first_ref = _pick["first_frame"]
            _anchor_kind, _anchor_path = _pick["kind"], _pick["path"]
            if _anchor_kind == "chain":
                warn_list_anchor = (
                    f"首帧取自**上一镜尾帧**（{os.path.basename(_anchor_path)}）—— 衔接最好。"
                    f"若你更在意「场景/人物跟你在项目里生成的图一致」，"
                    f"可把「首帧参考」改成**场景图**或**角色图**。")
            elif _anchor_kind == "character":
                warn_list_anchor = ("首帧取自**角色形象图**（按你的设置）——"
                                    "人物会贴近这张图，衔接与场景会弱一些。")
            elif _anchor_kind == "scene" and _anchor_path:
                warn_list_anchor = (f"首帧取自**场景图**（{os.path.basename(_anchor_path)}）——"
                                    f"场景与构图会贴近这张图。")
            else:
                warn_list_anchor = ("本镜**没有可用的首帧参考图**（场景图/角色图都没有）——"
                                    "模型只能凭文字生成，画面很难和你的图对上。")

            # ══════════════════════════════════════════════════════════
            # ★★ 2026-10-23（用户：「白膜要能当参考视频」「台词要在 480p 视频里出现」）：
            #   这里是**界面按钮真正走的那条路**（render_shot → _try_api_video），
            #   之前白膜参考/原生语音只接在 agent 工具 generate_preview 上 ——
            #   两条路不合一，用户在界面上按"生成"永远拿不到这些能力。
            # ══════════════════════════════════════════════════════════
            _previz_video_url = ""
            _previz_why = ""
            req_note_extra = ""   # ★ 2026-10-24：先初始化，避免"通道不支持视频参考"分支 NameError
            try:
                from core import previz_ref as _prx
                _ref = _prx.decide(shot, provider, settings=settings)
                _previz_why = _ref.reason or ""
                if _ref.usable and _ref.video_ref:
                    _previz_video_url = _ref.video_ref
                    warn_list_anchor = ((warn_list_anchor or "") +
                        "白膜已作为**参考视频**一起提交（提供走位/运镜/节奏）。")
                elif _ref.prompt_note:
                    # 平台/通道不支持视频参考时：至少把白膜定下的运镜写进提示词，并如实说
                    req_note_extra = str(_ref.prompt_note)
            except Exception as _e:  # noqa: BLE001
                logger.warning("白膜参考注入失败（按普通方式继续）：%s", _e)
            # ★★ 2026-10-24（真机对照后修）：白膜参考视频已附上时，首帧**必须**取白膜首帧。
            #   真机证据：首帧给了场景图 → 成片跟着场景图走，白膜（机位/走位）被完全架空。
            #   首帧是强约束、参考视频是弱约束，两者必须同源，白膜才可能被遵循。
            #   仅当调用方**显式**指定了 ref_priority（角色/场景）时才让位。
            # ★★ 2026-10-24（真机判定实验后的关键分支）：
            #   seedance 2.0 不支持 reference_video（发出去被静默忽略，还会抢掉首帧约束），
            #   但**首尾帧模式实测能钉住灰模布局**（SSIM 0.91 / 0.91）。
            #   所以：型号不支持视频参考时 → 不发 video_url，改用 first_frame + last_frame。
            _pv_use_lastframe = False
            _lf = ""
            if _previz_video_url:
                try:
                    from core import previz_ref as _prx2
                    if not _prx2.supports_reference_video_model(provider, model_name):
                        _lf = _prx2.white_model_last_frame(shot, at_seconds=float(dur or 0))
                        if _lf:
                            _pv_use_lastframe = True
                            _previz_video_url = ""      # 别发：2.0 会忽略它并抢掉首帧
                            # ★ 关键：首帧也换成白膜首帧。否则 first=场景图、last=灰模末帧，
                            #   两者风格对立，模型只会跟场景图走（实测 SSIM 0.52 / 0.38）。
                            try:
                                from core.agent_tools_production import _previz_first_frame as _pfx3
                                from core.adapters import to_image_ref as _tir3
                                _pf3 = _pfx3(shot)
                                if _pf3:
                                    _c3 = _tir3(_pf3)
                                    if _c3:
                                        first_ref = _c3
                                        # ★ 关键：首尾帧模式下**不发参考图**。
                                        #   判定实验证明：只给灰模首帧+末帧时 SSIM 0.91/0.91（跟得住）；
                                        #   一旦同时发场景图/角色图，模型就改跟参考图（端到端实测 0.44/0.37）。
                                        #   外观改由提示词里的角色锚定短语承担。
                                        char_refs = []
                                        # ★ 首尾帧模式下把"锚点优先"写进提示词（判定实验证明：
                                        #   中文剧情描述会把首帧约束冲淡，SSIM 掉到 0.45）。
                                        req_note_extra = ((req_note_extra or "") + "\n\n[FRAME ANCHOR — MUST FOLLOW] The first frame and the last frame are STRICT composition anchors from a previz (grey-block) video. Keep the subject count, prop positions, staging, shot size and screen direction exactly as they appear in those two frames; the subject moves from the start position to the end position with continuous natural motion and the camera performs the movement implied between them. Do NOT redesign the set, do NOT add or remove props, do NOT change the camera framing, no cuts, no teleportation. Only replace the grey-block look with photorealistic materials, lighting and costumes.")
                                        if "ref_priority" not in str(payload.get("note") or ""):
                                            warn_list_anchor = ((warn_list_anchor or "") +
                                                "；为避免参考图抢掉首帧约束，本镜**不发场景图/角色图**，"
                                                "人物长相与场景质感由提示词锚定（布局/走位优先）。")
                            except Exception as _e3:  # noqa: BLE001
                                logger.warning("首尾帧模式：白膜首帧取用失败：%s", _e3)
                            warn_list_anchor = ((warn_list_anchor or "") +
                                "该型号不支持参考视频，已改用**首尾帧模式（2.0 唯一可行）**："
                                "首帧=白膜首帧、尾帧=白膜末帧，构图与走位的端点由白膜钉死。")
                except Exception as _e_cap:  # noqa: BLE001
                    logger.warning("首尾帧模式判定失败：%s", _e_cap)
            if _previz_video_url and str(ref_priority or "auto") == "auto":
                try:
                    from core.agent_tools_production import _previz_first_frame as _pfx_pv
                    _pf_pv = _pfx_pv(shot)
                    if _pf_pv:
                        from core.adapters import to_image_ref as _tir_pv
                        _cand_pv = _tir_pv(_pf_pv)
                        if _cand_pv:
                            first_ref = _cand_pv
                            warn_list_anchor = ("首帧取自**白膜首帧**，并同时附白膜参考视频 —— "
                                                "构图/走位/运镜都以白膜为准（不再用场景图当首帧）。")
                            req_note_extra = ((req_note_extra or "") +
                                "严格跟随参考视频的机位、走位与运镜节奏；不要自行设计新场景或新构图。")
                except Exception as _e_pv:  # noqa: BLE001
                    logger.warning("白膜首帧优先失败，退回原有首帧逻辑：%s", _e_pv)
            # 白膜首帧兜底（没有接龙首帧时用白膜第一帧，构图贴着预演走）
            if not first_ref:
                try:
                    # 定义在 agent_tools_production 里（唯一实现，不复制一份）
                    from core.agent_tools_production import _previz_first_frame as _pfx
                    _pf = _pfx(shot)
                    if _pf:
                        from core.adapters import to_image_ref as _tir
                        first_ref = _tir(_pf) or first_ref
                except Exception as _e:  # noqa: BLE001
                    logger.warning("白膜首帧兜底失败：%s", _e)
            # 这一镜有台词 → 让视频模型自己说（音画同源，不靠后期配音）
            _has_dlg = False
            try:
                _tl = shot.get("layer2_timeline")
                if isinstance(_tl, str):
                    import json as _jj
                    _tl = _jj.loads(_tl) if _tl.strip() else []
                _has_dlg = any(
                    (isinstance(it, dict) and (
                        (isinstance(it.get("dialogue"), dict) and str((it["dialogue"] or {}).get("text") or "").strip())
                        or (isinstance(it.get("dialogue"), str) and it["dialogue"].strip())))
                    for it in (_tl or []))
            except Exception:  # noqa: BLE001
                _has_dlg = False

            from core.previz_ref import require_reference_quality
            try:
                _quality_gate = require_reference_quality(shot, provider, model_name, settings)
                if _quality_gate is not None and not _previz_video_url:
                    raise ValueError("白膜未注入本次参考视频请求；请确认白膜状态与引用设置后重试。")
            except ValueError as quality_error:
                return {"ok": False, "error": str(quality_error),
                        "hint": "付费任务尚未提交。请重新渲染并核验当前分镜白膜；不会自动改为文字生成。"}
            if provider == "seedance_t6" and _previz_video_url:
                # Reference-video mode excludes first/last-frame mode.
                first_ref, _lf, _pv_use_lastframe = None, "", False
                req_note_extra = "以@视频1为场景、构图、主体动作与运镜参考；保留剧本明确的主体颜色和静音要求。"
            req = VideoGenRequest(
                # ★ 2026-10-24：把白膜的提示词补充真的拼进 prompt（原来这个变量没人读）
                prompt=((str(vp["prompt"] or "") + "\n" + str(req_note_extra)) if req_note_extra else vp["prompt"]),
                duration=dur,
                aspect_ratio=shot.get("aspect_ratio") or "16:9",
                resolution=shot.get("resolution") or "720p",
                reference_images=char_refs,
                first_frame=first_ref,
                last_frame=((_lf or None) if _pv_use_lastframe else None),
                character_reference=char_refs,
                negative_prompt=vp["negative_prompt"],
                video_url=(_previz_video_url or None),
                with_audio=_has_dlg,
                extra={"quality": "mini"} if provider == "seedance_t6" and model_name == "seedance-2-0-promo" else {},
            )
            from core.adapters import degrade_request as _degrade
            _dgrade_warn = ""
            # 降级逻辑在 core.adapters.degrade_request 里（可单测）——
            # 这里只负责用它，并把结果翻译成人话给用户看。
            verr, degraded = _degrade(adapter, req)
            if verr:
                return {"ok": False, "error": f"请求参数不合法：{verr}",
                        "hint": f"当前模型 {provider} 支持的时长 "
                                f"{adapter.min_duration}~{adapter.max_duration}s、"
                                f"比例 {adapter.supported_aspect_ratios}"}
            if degraded:
                logger.info("模型 %s 不支持 %s，已自动去掉这些可选输入后继续",
                            provider, degraded)
                # ★★ 2026-09-14：**降级必须让用户看见**。
                #   原来只 logger.info 一行 —— 用户在界面上什么都看不到，
                #   于是「我明明关联了角色图、出片却不像」变成无解之谜。
                #   现在按「人话 + 下一步动作」报出来。
                _human = {"reference_images": "人物参考图",
                          "character_reference": "人物参考图（主体参考）",
                          "first_frame": "首帧参考图（场景图 / 上一镜尾帧）",
                          "last_frame": "尾帧图",
                          "negative_prompt": "负面提示词"}
                _names, _seen = [], set()
                for _d in degraded:
                    _n = _human.get(_d, _d)
                    if _n not in _seen:
                        _seen.add(_n)
                        _names.append(_n)
                _roles = "、".join(c.get("name") or "" for c in char_cards
                                  if c.get("name")) or "本镜角色"
                _joined = "、".join(_names)
                if any("参考图" in n for n in _names):
                    _dgrade_warn = (
                        f"⚠ 当前模型（{adapter.display_name}）**用不了**{_joined} —— "
                        f"也就是说 {_roles} 的**形象图没有被采用**，"
                        f"人物长相只能靠提示词里的文字描述，"
                        f"**很可能跟你在「角色」页生成的形象不符**。"
                        f"想让人物对得上：换一个支持主体/角色参考的模型"
                        f"（例如 Seedance），或接受「以文字为准」。")
                else:
                    _dgrade_warn = (f"当前模型（{adapter.display_name}）不支持"
                                    f"{_joined}，已自动去掉后继续出片。")
            out_dir = os.path.join(_shots_out_dir(shot["project_id"]), "api")
            os.makedirs(out_dir, exist_ok=True)

            # ══════════════════════════════════════════════════════════
            # 长时长：按**模型真实能力**分段，每段只演自己那几秒的内容
            # ══════════════════════════════════════════════════════════
            # 旧实现有**三个真错**，正是用户"分层定 10s 却只能出 5s、
            # 而且画面跟描述不相干/只有一小部分"的直接原因：
            #   ① 用 `adapter.max_duration` 这个**标量**当上限，
            #      而真实约束是"某分辨率下只允许某些时长"
            #      （海螺 Hailuo-02：1080P 只给 6s，768P 给 6/10s）
            #   ② 段长是 `total//n` 算出来的**任意值**（7s、5s…），
            #      不在模型的合法时长集合里 → 被厂商拒绝或静默截断
            #   ③ **每段用同一段提示词**，只追加一句"这是第 N 段"
            #      → 每段都在演同样的内容，用户看到的就是"只有一小部分"
            # 现在改用 `core.shotplan` 规划：合法时长 + 每段独立切片提示词。
            from core.shotplan import plan_shot, plan_summary
            _ov = str(duration_override or "").strip()
            if _ov and _ov not in ("auto", "None"):
                try:
                    total_sec = max(1, int(float(_ov)))
                except Exception:
                    total_sec = int(shot.get("duration_seconds") or 5)
            else:
                total_sec = int(shot.get("duration_seconds") or 5)
            model_entry: Dict[str, Any] = {}
            try:
                # 跨厂商查：分镜表的 provider/model_name 可能对不上
                # （线上有 provider=hailuo 而 model_name=kling-1.6 的记录），
                # 只按 provider 查会退回"最保守 5 秒"，白白多花一次调用。
                from core.model_catalog import find_video_model
                model_entry = find_video_model(model_name, provider) or {}
            except Exception:
                model_entry = {}
            # 该型号是否实测过"有首帧时画幅由首帧决定"（决定能否为省钱降分辨率）
            _ff_governs = False
            try:
                from core import aspect as _aspf
                _ff_governs = _aspf.first_frame_governs(provider, model_name)
            except Exception:
                _ff_governs = False
            # ★★ 音频主轴：先量这个分镜的台词**念完到底几秒**，再让规划器
            #    按它来定总长。这样"分层定 10s 却只能出 5s"才不会发生
            #    —— 因为如果台词只有 6.8 秒，我们根本不会去要 10 秒。
            #    顺序很关键：用户手动指定时长时以用户为准，否则以配音实测为准。
            audio_sec = 0.0
            if not (_ov and _ov not in ("auto", "None")):
                audio_sec = await _measure_shot_audio(shot, settings, char_cards)
            splan = plan_shot(shot, model_entry=model_entry,
                              resolution=(shot.get("resolution") or req.resolution or "720p"),
                              want_duration=total_sec,
                              audio_seconds=audio_sec,
                              provider=provider,
                              # 有首帧、且该型号实测"首帧决定画幅"时，
                              # 降分辨率不会改变画幅 → 允许它省钱换长时长
                              first_frame_governs=bool(scene_ref) and _ff_governs)
            segments = splan.get("segments") or [{"duration": total_sec, "timeline": []}]
            plan = [int(s["duration"]) for s in segments]
            # 计划总长 = 各段之和（就是 splan["effective"]）。后面拿它跟**实测**比对，
            # 差多少要如实报给用户 —— 不能让"计划 10 秒、实出 5 秒"再悄悄发生一次。
            effective_planned = sum(plan) or int(total_sec)
            plan_note = plan_summary(splan)
            # 分辨率可能被规划器调过（降一档换更长时长）
            if splan.get("resolution"):
                try:
                    req.resolution = splan["resolution"]
                except Exception:
                    pass

            parts: List[str] = []
            seg_urls: List[str] = []
            seg_last_frame: str = ""
            aspect_seen: List[Dict[str, Any]] = []
            for si, seg in enumerate(segments):
                seg_dur = int(seg["duration"])
                # ★ 请求时长必须**落在该分辨率允许的集合里**。
                #   这里原来只按 `adapter.max_duration`（一个**标量**）做 min/max，
                #   而真实约束是"某分辨率下只允许某些时长"：
                #   海螺 Hailuo-2.3 是 {'768P': [6,10], '1080P': [6]}，
                #   于是 10s 段在 1080P 下会被这个 min 放过（标量是 10），
                #   发出去被厂商拒绝或**静默截成 6 秒** ——
                #   用户看到的就是"分镜的视频根本没生成"。
                #   现在按能力表把段长**吸附到合法值**（优先不小于计划值的那个），
                #   保证每个请求都是合法的。
                req.duration = _snap_legal_duration(
                    adapter, provider, model_name, req.resolution, seg_dur)
                # ★ 吸附改动了时长就必须说 —— 不然就成了"偷偷把 10 秒变 6 秒"，
                #   正是用户抱怨的那个现象。交给下面的时长对账去报缺口。
                if int(req.duration) != int(seg_dur):
                    splan.setdefault("warnings", []).append(
                        f"第 {si+1} 段：{req.resolution} 下这个模型只支持 "
                        f"{int(req.duration)} 秒（计划 {seg_dur} 秒），已按合法值请求；"
                        f"想完整覆盖请降到支持更长时长的分辨率，或拆成更多段。")
                # ★ 每段用**它自己那几秒的时间线**重新构造提示词 ——
                #   这是"画面与描述对得上"的关键：第 2 段演的是第 2 段该演的事，
                #   而不是把整段 12 秒的内容压缩进 6 秒。
                seg_timeline = seg.get("timeline") or []
                if seg_timeline:
                    seg_shot = dict(shot)
                    seg_shot["layer2_timeline"] = json.dumps(seg_timeline,
                                                             ensure_ascii=False)
                    seg_shot["duration_seconds"] = seg_dur
                    try:
                        seg_vp = build_video_prompt(
                            seg_shot, scene=scene, characters=char_cards, art=art,
                            style=(art.get("style_id") or settings.get("default_style")
                                   or "cinematic"),
                            provider=provider, duration=seg_dur,
                            aspect_ratio=shot.get("aspect_ratio") or "16:9",
                            resolution=req.resolution,
                            prev_shot=payload_ctx.get("_prev_shot") if si == 0 else None,
                            next_shot=payload_ctx.get("_next_shot")
                            if si == len(segments) - 1 else None,
                            layers=layers or None)
                        req.prompt = seg_vp["prompt"]
                        req.negative_prompt = seg_vp.get("negative_prompt") or ""
                    except Exception as e:
                        logger.warning("第 %d 段提示词构造失败，回退整段提示词：%s", si + 1, e)
                        req.prompt = vp["prompt"]
                elif len(segments) > 1:
                    # 没有分层内容可切（分镜本身没有 timeline）→ 只能靠承接语
                    req.prompt = (vp["prompt"][:1200]
                                  + f"。这是同一镜头的第 {si+1}/{len(segments)} 段，"
                                    f"画面须与上一段结尾自然衔接，人物外观与服装保持一致")
                # ★ 段间**尾帧串联**：把上一段的尾帧当这一段的首帧，
                #   画面才是"长出来"的，而不是另起一个不相干的镜头。
                if si > 0 and seg_last_frame:
                    try:
                        # 段间首帧同样要归一画幅（正常情况下上一段已经是目标画幅，
                        # 这一步是 no-op；万一上一段画幅偏了，这里也不会把偏差传下去）
                        from core import aspect as _asp2
                        _lf = _asp2.conform_image(
                            seg_last_frame, shot.get("aspect_ratio") or "16:9")
                        ref2 = to_image_ref(_lf)
                        if ref2:
                            req.first_frame = ref2
                    except Exception:
                        pass
                elif si > 0:
                    req.first_frame = first_ref
                if provider == "seedance_t6" and req.video_url:
                    req.first_frame = req.last_frame = None
                    if "@视频1" not in req.prompt:
                        req.prompt += "\n以@视频1为场景、主体动作、构图与运镜参考；不增加剧本未要求的内容。"
                result = await asyncio.wait_for(adapter.generate(req), timeout=1800)
                if not result.success:
                    return {"ok": False,
                            "error": f"第 {si+1}/{len(segments)} 段失败：{result.error}",
                            "hint": _api_error_hint(provider, result.error or "")}
                if result.task_id:
                    _paid_task_ids.append(str(result.task_id))
                if provider == "seedance_t6" and model_name == "seedance-2-0-promo" and _previz_video_url:
                    from core.budget import add_cost
                    from core.shot_segments import _estimate_segment_cost_yuan
                    add_cost(shot.get("project_id") or "", _estimate_segment_cost_yuan(
                        provider, float(req.duration), req.resolution), db=db)
                    _generation_cost_recorded = True
                local = os.path.join(out_dir, f"{shot['id']}_{int(_t.time())}_{si:02d}.mp4")
                await adapter.download_to_local(result.video_url, local)
                if not os.path.exists(local) or os.path.getsize(local) < 10240:
                    return {"ok": False, "error": f"第 {si+1} 段视频未能下载"}
                # ★★ 时长台账：**每次真实生成都记一笔**"要了多久 / 实际给了多久"。
                #   厂商会**接受请求却给更短的片子**（实测 `video-01` 要 10s 给 5.64s），
                #   而静态能力表追不上这种"静默忽略参数"（换型号、换版本就变）。
                #   记满两次且明显偏短，规划层下次就自动避开那一档
                #   （见 `core.duraledger` 与 `caps_for`）。
                try:
                    from core.compose import probe_video_duration as _pvd
                    from core import duraledger as _dl
                    _real = float(await _pvd(local) or 0.0)
                    if _real > 0.2:
                        _dl.record(provider, model_name, req.resolution,
                                   req.duration, _real)
                        if _real < float(req.duration) * 0.8:
                            splan.setdefault("warnings", []).append(
                                f"第 {si+1} 段：要了 {int(req.duration)}s，"
                                f"厂商实际只给 {_real:.2f}s（已记入时长台账，"
                                f"下次规划会自动避开这个时长档）。")
                except Exception as _e:
                    logger.warning("时长记账失败（不影响生成）：%s", _e)
                # ★★ 画幅守卫：厂商回"任务成功"**不等于**它按你要的画幅出片。
                #   真实事故（花钱买来的）：分镜 16:9，海螺 MiniMax-Hailuo-2.3 在
                #   768P 下返回 768×768（1:1），而适配器 payload 里根本没有画幅字段
                #   —— 画幅是厂商按分辨率隐含决定的。以前这一层是空的，于是
                #   成片归一化时**静默裁掉 44% 画面**，构图/站位/运镜全废，
                #   而用户在界面上只看到"生成成功"。
                #   现在：量尺寸 → 与分镜画幅比 → 结构性不符就明确失败（并留下证据）。
                _want_ar = shot.get("aspect_ratio") or "16:9"
                try:
                    from core import aspect as _asp
                    _ag = _asp.guard_clip(local, _want_ar, provider=provider,
                                          model=model_name, resolution=req.resolution)
                except Exception as _e:
                    logger.warning("画幅检查没跑起来：%s", _e)
                    _ag = {}
                _seg_asp = {"segment": si + 1, "want": _want_ar,
                            "actual": _ag.get("actual") or "",
                            "w": _ag.get("w") or 0, "h": _ag.get("h") or 0,
                            "crop_loss": _ag.get("crop_loss") or 0.0,
                            "ok": _ag.get("ok") if _ag else None,
                            "file": local}
                aspect_seen.append(_seg_asp)
                if _ag.get("severity") == "fatal":
                    # ★★ 2026-09-14：**付费生成出来的片段不再被丢掉了**。
                    #   真实事故（用户）：seedance 明明出了片，但因为"画幅不符"
                    #   被这一层判失败 → 上层退回**本地合成**（一张图的运镜）。
                    #   用户看到的是"我用了视频模型、关联了图，结果没按剧本演、
                    #   该出现的人物也没出现" —— 他看到的其实是兜底幻灯片。
                    #   现在：把这条真实片段**登记成候选**（不自动选中），
                    #   报错里说清"东西还在、可以怎么用"，并附失败码与责任层。
                    try:
                        _reg_meta = {
                            "path": local, "duration": _ag.get("actual") or req.duration,
                            "width": _ag.get("w"), "height": _ag.get("h"),
                            "aspect_actual": _ag.get("actual"),
                            "aspect_want": _want_ar,
                            "aspect_mismatch": True,
                            "provider": provider, "model": model_name,
                            "resolution": req.resolution,
                            "note": ("这条是视频模型的真实产物，但画幅与分镜不一致；"
                                     "已保留为候选（未自动选中）。"),
                        }
                        _register_local_candidate(shot, _reg_meta, source="api", select=False)
                    except Exception as _e:
                        logger.warning("登记画幅不符的候选失败：%s", _e)
                    from core import failcodes as _fc
                    _ann = _fc.annotate(_ag.get("message") or "画幅不符")
                    return {"ok": False,
                            "error": (f"第 {si+1}/{len(segments)} 段"
                                      f"（{req.resolution}）：{_ag['message']}"),
                            "hint": ("**这条片段没有丢** —— 已经登记成这一镜的候选"
                                     "（画幅 "
                                     f"{_ag.get('w')}x{_ag.get('h')}）。"
                                     "你可以到分镜页选中它（合成时按分镜画幅居中裁剪/"
                                     "补边），或者改分辨率/型号重新生成一次。"
                                     + (f"\n诊断：{_ann['hint']}" if _ann.get("hint") else "")),
                            "failcodes": _ann.get("codes") or [],
                            "responsibility_layer": _ann.get("layer") or "",
                            "aspect": aspect_seen}
                if _ag.get("severity") == "warn":
                    splan.setdefault("warnings", []).append(
                        f"第 {si+1} 段：{_ag['message']}")
                parts.append(local)
                if si < len(segments) - 1:
                    try:
                        from core.continuity import extract_last_frame
                        lf = os.path.join(out_dir, f"{shot['id']}_seg{si}_last.jpg")
                        seg_last_frame = extract_last_frame(local, lf) or ""
                    except Exception as e:
                        logger.warning("抽第 %d 段尾帧失败：%s", si + 1, e)

            # ★★ 成片时长必须**实测**，不能拿"我要了多久"当"实际有多久"。
            #    旧代码这里是 `final_dur = total_sec`（只有一段时直接用请求值），
            #    于是模型只出 5 秒、我们却往库里写 10 秒 ——
            #    用户抱怨的"分层定 10s 但最终只能生成 5s"有一半是这个假数字造成的：
            #    画面确实只有 5 秒，但库里、时间轴里、字幕里都写着 10 秒，
            #    成片就出现"后半段没画面"或"字幕配空画面"。
            #    MoneyPrinterTurbo 在这一点上分得很清：`max_clip_duration` 约束的是
            #    **成片里的最终播放时长**，而读源文件用的是换算过的
            #    `source_clip_duration`（`video.py:743-748`）。它每次都用实际
            #    `clip.duration` 累加，并且会把缺口**明确打日志**
            #    （"video duration (X) is shorter than required duration (Y)"）。
            #    我们照这个做：实测 + 如实报告差距。
            from core.compose import _concat, probe_duration
            if len(parts) == 1:
                final_path = parts[0]
            else:
                final_path = os.path.join(out_dir, f"{shot['id']}_{int(_t.time())}_full.mp4")
                await _concat(parts, final_path, transition="fade", transition_sec=0.4)
            probed = 0.0
            try:
                probed = float(await probe_duration(final_path) or 0.0)
            except Exception as e:
                logger.warning("实测成片时长失败：%s", e)

            # 对账：计划 vs 实测。文案统一由 core.assemble.reconcile_duration 产出
            # （纯函数、可单测），这里不再自己写一套判断逻辑。
            from core.assemble import reconcile_duration
            if probed > 0.2:
                _rec = reconcile_duration(effective_planned, probed)
                final_dur = _rec["actual"]
                warn_list: List[str] = list(_rec["warnings"])
                duration_measured = True
                if _rec["shortfall"] > 0:
                    logger.warning("分镜 %s 时长不足：计划 %.1fs，实出 %.1fs（差 %.1fs）",
                                   shot.get("id"), effective_planned, final_dur,
                                   _rec["shortfall"])
            else:
                # 连 ffprobe 都读不到 → 退回"各段实测之和"，再不行退回请求值。
                # ★ 这时**不能**走对账：拿一个估出来的数字去算"差了多少秒"
                #   等于编一个缺口出来。如实说是估计的就行。
                per_seg = []
                for _p in parts:
                    try:
                        per_seg.append(float(await probe_duration(_p) or 0.0))
                    except Exception:
                        per_seg.append(0.0)
                _sum = sum(x for x in per_seg if x > 0.2)
                final_dur = round(_sum or float(effective_planned), 3)
                duration_measured = False
                warn_list = [
                    "量不出成片时长（ffprobe 没读到），已按各段时长之和记录，"
                    "可能与实际有少量出入。"]
            # ══════════════════════════════════════════════════════════
            # ★★ 2026-09-14：**生成完就给这一镜配音**（用户反馈"生成的视频里
            #    人物没发出声音"）
            # ══════════════════════════════════════════════════════════
            # 实测根因：视频模型返回的 mp4 **完全没有音轨** ——
            #   `caa9904f` 的 5 个 API 候选，`Audio:` 一个都没有；而这个函数
            #   （530 行）里**没有任何一处**做配音（dub/voicecast/TTS 出现 0 次）。
            #   配音以前只在**出片（compose）阶段**才做，所以：
            #     成片里有声音，但**点开这一镜预览是死寂** —— 观感就是
            #     "视频模型生成的人物不发声"。
            #
            # 现在在这一步就配上，而且按用户当初的要求来：
            #   · **整片选角表**（同一角色全片同一把嗓子，不是每镜乱选）
            #   · **逐句韵律**（情绪 → 语速/音高/音量 + 气口/尾拍）
            #   · 没有台词就**不加旁白**（`narration_fallback` 默认关：
            #     自动念剧情概述属于方法论明列的 `F-AUDIO-POLLUTION`）
            #
            # 不会和 compose 阶段重复叠音：那边 `_mux_clip_audio` 只取本片段的
            # **视频流** + 它自己那份配音（同一套选角与韵律），本片段的音轨被替换掉。
            delivery_info: Dict[str, Any] = {}
            voice_info: Dict[str, Any] = {}
            if with_voice:
                try:
                    from core.dialogue import timeline_lines as _tll2
                    from core import scriptlines as _slg2
                    _srcs2 = _script_sources_for(shot["project_id"])
                    _ctx2 = _shot_script_ctx(shot)
                    # ★ 先从剧本回填（分镜漏写 dialogue 时），再判断有没有台词。
                    _tl_raw2 = shot.get("layer2_timeline")
                    _shot_lines = _tll2(_tl_raw2, final_dur)
                    _rec2 = None
                    if not _shot_lines and _srcs2:
                        _inj2 = _slg2.recover_and_inject(
                            shot, _srcs2, scene_name=_ctx2["scene_name"],
                            character_names=_ctx2["character_names"],
                            order_index=shot.get("order_index"))
                        if _inj2.get("added"):
                            _tmp2 = dict(shot)
                            _tmp2["layer2_timeline"] = _inj2["timeline"]
                            _shot_lines = _tll2(_inj2["timeline"], final_dur)
                            if _shot_lines:
                                shot = _tmp2
                                _rec2 = _inj2
                    if not _shot_lines:
                        warn_list.append(
                            "这一镜**没有台词**，所以预览里没有人声（按你的要求"
                            "不会自动加旁白念剧情）。要让角色说话，先给这一镜补台词。")
                    else:
                        from core import voicecast as _vc2
                        from core.compose import _mux_clip_audio as _mux2
                        _all_chars = list(db.list_characters(shot["project_id"]) or [])
                        _dv = str(settings.get("default_voice")
                                  or "edge:zh-CN-XiaoxiaoNeural")
                        # ★ 生成这一镜时就按**真实音色目录**选角（与"一键配音"
                        #   和出片阶段同一套），并把目录传下去，好让卡外角色
                        #   也能各自分到一把嗓子。
                        _cat2 = await _vc2.load_voice_catalog(settings)
                        _cast = _vc2.cast_plan(_all_chars, _dv, catalog=_cat2)
                        _vdir = os.path.join(out_dir, "voice")
                        _dub = await _vc2.dub_shot(
                            shot, characters=_all_chars, settings=settings,
                            out_dir=_vdir, default_voice=_dv, duration=final_dur,
                            cast=_cast.get("voices"), catalog=_cat2,
                            script_sources=_srcs2,
                            scene_name=_ctx2["scene_name"],
                            shot_order=shot.get("order_index"),
                            character_names=_ctx2["character_names"])
                        if _rec2:
                            warn_list.append(
                                f"📜 这一镜的分镜里**漏写了台词**，已从剧本"
                                f"「{_rec2.get('scene_title') or _rec2.get('source')}」"
                                f"回填 {_rec2.get('added')} 句"
                                f"（只填说话人确实在这一镜里的台词）。")
                        if _dub.get("ok") and _dub.get("placements"):
                            _voiced = os.path.join(
                                out_dir, f"{shot['id']}_{int(_t.time())}_voiced.mp4")
                            _muxed = await _mux2(final_path, _dub["path"], _voiced,
                                                 final_dur)
                            if _muxed:
                                final_path = _voiced
                                voice_info = {
                                    "voiced": True, "line_count": _dub.get("line_count"),
                                    "cast": _dub.get("cast") or {},
                                    "prosody": _dub.get("prosody_notes") or [],
                                    "track": _dub.get("path")}
                                _who = "、".join(
                                    f"{k}→{str(v).split(':')[-1]}"
                                    for k, v in list(voice_info["cast"].items())[:6])
                                warn_list.append(
                                    f"🔊 本镜已配音：{voice_info['line_count']} 句，"
                                    f"角色各自音色（{_who}）；"
                                    f"预览即可听到对白。")
                            else:
                                warn_list.append(
                                    "配音合成好了，但**挂进这一镜失败** —— "
                                    "预览仍是静音（成片阶段还会再配一次）。")
                        else:
                            warn_list.append(
                                f"这一镜配音没成功（预览会是静音）："
                                f"{str(_dub.get('error') or '未知原因')[:60]}")
                except Exception as _e:
                    logger.warning("本镜配音失败：%s", _e)
                    warn_list.append(f"本镜配音失败（预览会是静音）：{_e}")
            if provider == "seedance_t6" and model_name == "seedance-2-0-promo" and _previz_video_url:
                from core.reference_delivery import finalize_reference_candidate
                from core.previz_ref import white_model_video
                delivery_info = await finalize_reference_candidate(
                    final_path, white_model_video(shot), float(total_sec),
                    aspect_ratio=shot.get("aspect_ratio") or "16:9", fps=24,
                    silent=not _has_dlg and not any(isinstance(item, dict) and str(item.get("narration") or "").strip()
                        for item in (_tl if isinstance(_tl, list) else [])),
                    ffmpeg_path=settings.get("ffmpeg_path") or "",
                    source_metadata={"provider": provider, "model": model_name,
                                     "task_ids": _paid_task_ids, "source": "seedance"})
                if not delivery_info.get("ok"):
                    _register_local_candidate(shot, {"path": delivery_info.get("path") or final_path,
                        "duration": final_dur, "provider": provider, "model": model_name,
                        "delivery": delivery_info,
                        "note": "视频已生成，可播放和下载；但与给定白膜的逐帧一致性未通过，未选为正式成片，也不会进入最终合成。"},
                        source="api", select=False)
                    _retained = delivery_info.get("path") or final_path
                    _play_url = f"/api/media?path={quote(_retained)}"
                    return {"ok": False, "generated": True, "playable": True,
                            "status": delivery_info.get("status") or "rejected",
                            "error": delivery_info.get("error") or "480p与给定白膜的逐帧一致性未通过",
                            "hint": "视频已生成并保留，可播放和下载；但一致性未通过，不会自动选为正式成片或进入最终合成，也不会自动重试、切换模型或用本地合成冒充。已发生的生成费用仍计入预算。",
                            "no_fallback": True, "requires_user_review": True,
                            "path": _retained, "raw_path": final_path,
                            "url": _play_url, "download_url": _play_url + "&download=1",
                            "delivery": delivery_info,
                            "cost_recorded": _generation_cost_recorded}
                final_path, final_dur = delivery_info["path"], delivery_info["duration"]
                duration_measured = True
                warn_list.append("Seedance交付片已通过逐帧一致性验收；原片、参考和验收记录均已保留。")
            if chain_info.get("path"):
                warn_list.append(chain_info.get("why") or "已接上一镜尾帧")
            elif chain and chain_info.get("why") and chain_info["why"] != "未启用串联":
                warn_list.append(f"未做镜头串联：{chain_info['why']}")
            warn_list.extend(_ff_notes)
            if warn_list_anchor:
                warn_list.append(warn_list_anchor)
            if _dgrade_warn:
                warn_list.append(_dgrade_warn)
            for a in (splan.get("adjustments") or []):
                warn_list.append(str(a).replace("<b>", "").replace("</b>", ""))
            for w in (splan.get("warnings") or []):
                warn_list.append(str(w))
            return {"ok": True, "path": final_path, "duration": final_dur,
                    "url": f"/api/media?path={quote(final_path)}", "source": "api",
                    "provider": provider, "model": model_name,
                    "segments": len(parts), "requested_seconds": total_sec,
                    "cost_recorded": _generation_cost_recorded,
                    "task_ids": _paid_task_ids,
                    "planned_seconds": effective_planned,
                    "duration_measured": duration_measured,
                    "delivery": delivery_info,
                    "duration_shortfall": round(
                        max(0.0, effective_planned - final_dur), 2)
                    if duration_measured else 0.0,
                    "plan": {"summary": plan_note,
                             "durations": plan,
                             "resolution": splan.get("resolution") or "",
                             "effective": splan.get("effective")},
                    "aspect": aspect_seen,
                    # ★ 生成前的引用体检结果（有哪些角色没关联上/关联已失效）
                    "ref_check": ref_check,
                    "refs_sent": {"characters": len(char_refs),
                                  "scene": bool(scene_ref),
                                  "characters_named": [c.get("name") for c in char_cards]},
                    "chain_from": chain_info.get("from_shot_id") or "",
                    "anchor": {"kind": _anchor_kind, "path": _anchor_path,
                               "ref_priority": ref_priority},
                    "refs": {"characters": len(char_refs),
                             "scene": 1 if scene_ref else 0,
                             "characters_used": bool(
                                 adapter.supports_character_ref and char_refs)},
                    "voice": voice_info,
                    "warnings": warn_list}
        except Exception as e:
            logger.exception("api video generation failed")
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "hint": _api_error_hint(provider, str(e))}

    def _api_error_hint(provider: str, err: str) -> str:
        """把常见 API 报错翻译成用户能照着做的动作（按厂商给不同建议）"""
        e = (err or "").lower()
        if "duration" in e and ("only support" in e or "invalid params" in e):
            return "时长超出该模型限制。注意部分模型的上限跟分辨率挂钩（如 MiniMax 海螺 1080P 只支持 6 秒，768P 支持 6/10 秒）。到「换模型」把时长改小或降低分辨率。"
        if "model" in e and ("not found" in e or "does not exist" in e or "invalid" in e):
            if provider in ("seedance", "jimeng"):
                return ("模型名/接入点 ID 不对。火山方舟要在控制台创建「推理接入点」拿到形如 "
                        "ep-2024xxxxxx-xxxxx 的 ID，或用官方模型 ID；"
                        "推荐到「设置 → 默认视频模型」点「🔍 拉取可用模型清单」直接从账号里选。")
            return "模型名不对。建议到「设置 → 默认视频模型」拉取该厂商账号下的可用模型清单后再选。"
        if "2049" in e or "invalid api key" in e or "401" in e or "unauthorized" in e or "authentication" in e:
            return ("API Key 无效。注意**对话 Key 与视频 Key 可能不是同一个**；"
                    "另外部分厂商新旧域名不同（MiniMax 视频要用 api.minimaxi.com 的 Key）。")
        if "1008" in e or "insufficient" in e or "balance" in e or "余额" in e:
            where = {"hailuo": "MiniMax（platform.minimaxi.com → 账户充值）",
                     "seedance": "火山方舟（console.volcengine.com/ark → 充值）",
                     "jimeng": "火山方舟",
                     "kling": "可灵开放平台",
                     "wanx": "阿里云百炼（bailian.console.aliyun.com）"}.get(provider, "对应厂商控制台")
            return (f"⚠ **账户余额不足** —— 厂商明确返回 insufficient balance。"
                    f"请到 {where} 充值后重试。\n"
                    f"（在此之前可以先点「生成方式 → 只用本地合成」，本地合成不花钱、照样能出片）")
        if "1002" in e or "429" in e or "rate limit" in e:
            return "触发限流，稍后重试或检查账户配额。"
        if "403" in e or "forbidden" in e or "not activated" in e or "no permission" in e:
            return "该 Key 没有开通这个模型/服务，请到厂商控制台开通后再试。"
        if "sensitive" in e or "moderation" in e or "审核" in e:
            return "提示词触发内容审核，请把敏感词换成中性描述后重试。"
        if "timeout" in e or "超时" in e:
            return "模型侧超时，可减少时长/分辨率后重试。"
        return "可先用「本地合成」模式出片（不需要 Key），同时到「设置」核对模型名、分辨率与 Key。"

    async def _local_render_shot(shot: Dict[str, Any], settings: dict, payload: Dict[str, Any],
                                 state: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
        """本地合成路径（不需要任何付费 Key）"""
        from core.compose import render_shot_clip
        target = payload.get("duration_seconds")
        default_size = "720x1280" if shot.get("aspect_ratio") == "9:16" else "1280x720"
        out = os.path.join(_shots_out_dir(shot["project_id"]), f"{shot['id']}.mp4")
        return await render_shot_clip(
            shot, db=db, settings=settings,
            voice_id=payload.get("voice_id") or "edge:zh-CN-XiaoxiaoNeural",
            with_voice=bool(payload.get("with_voice", True)),
            target_seconds=float(target) if target else None,
            auto_images=bool(payload.get("auto_images", True)),
            image_provider=str(payload.get("image_provider") or "minimax"),
            image_size=str(payload.get("image_size") or default_size),
            out_path=out,
            progress=lambda stage, pct, msg: state.update(
                {"percent": round(pct * 100, 1), "message": msg}),
        )

    def _register_local_candidate(shot: Dict[str, Any], res: Dict[str, Any], source: str = "local", *, select: bool = True):
        cands = shot.get("candidates") or []
        if isinstance(cands, str):
            try:
                cands = json.loads(cands)
            except Exception:
                cands = []
        cands = [c for c in cands if isinstance(c, dict)]
        if select:
            for c in cands:
                c["is_selected"] = False
        cid = f"{source}_{int(time.time())}"
        _play_url = res.get("url") or f"/api/media?path={quote(res['path'])}"
        _download_url = f"/api/media?path={quote(res['path'])}&download=1"
        _delivery = res.get("delivery") or {}
        _validation = ("verified" if _delivery.get("ok") is True else
                       ("rejected" if _delivery else "not_requested"))
        cands.append({"candidate_id": cid, "path": res["path"],
                      "url": _play_url, "download_url": _download_url,
                      "duration_seconds": res.get("duration"), "is_selected": select,
                      "source": source, "width": res.get("width"), "height": res.get("height"),
                      "size_bytes": res.get("size_bytes") or 0, "created_at": time.time(),
                      "delivery": _delivery, "validation_status": _validation,
                      "note": res.get("note") or "",
                      "provider": res.get("provider") or "", "model": res.get("model") or ""})
        if select:
            db.update_shot(shot["id"], status="completed", progress=1.0,
                           candidates=cands, selected_candidate_id=cid, error_message="")
        else:
            db.update_shot(shot["id"], candidates=cands)
        return cid

    @app.post("/api/projects/{pid}/voice/dub")
    async def dub_project_voices(pid: str, payload: Dict[str, Any] = None):
        r"""**一键配音**：把整片每个镜头都按角色卡 + 剧本配好音，挂进这一镜。

        用户的要求：「能不能一键完成角色配音什么的，要完全契合剧本、角色还有
        情景、情绪等等，而不是你丢的那几个固定的 npc 发音」。

        所以这里做的是**整片一次性的选角 + 逐句演绎**：
          ① 拉**真实音色目录**（各厂商 list_voices；实测本机 52 个、中文 44 个），
             按角色卡的**性别/年龄/定位/personality**（很多卡把"说话语气…"
             直接写进去了）打分选音色，并给出**可读的理由**；
          ② 整片一次定死（同一角色全片同一把嗓子，且尽量不撞音）；
          ③ 逐句按情绪算韵律（语速/音高/音量 + 气口/尾拍）；
          ④ 音轨**挂进这一镜**（只替换音轨、视频流原样 copy），
             所以**不用重新调用视频模型就能听到对白**（对已有素材也有效，零成本）；
          ⑤ 没有台词的分镜**不加旁白**，只如实说明。

        body（都可选）：`{"select": true}` 把配好音的版本设为该镜选中候选。
        用法：POST /api/projects/{pid}/voice/dub
        """
        payload = payload or {}
        proj = db.get_project(pid)
        if not proj:
            raise error_response("项目不存在", status=404)
        shots = sorted(db.list_shots(pid) or [], key=lambda s: s.get("order_index") or 0)
        if not shots:
            raise error_response("这个项目还没有分镜", status=400)
        settings = db.get_all_settings()
        chars = list(db.list_characters(pid) or [])
        dv = str(settings.get("default_voice") or "edge:zh-CN-XiaoxiaoNeural")

        from core import voicecast as _vc
        from core.dialogue import timeline_lines as _tl
        catalog = await _vc.load_voice_catalog(settings)
        plan = _vc.cast_plan(chars, dv, catalog=catalog)
        res: Dict[str, Any] = {
            "ok": True, "cast": plan.get("voices") or {},
            "reasons": plan.get("reasons") or {},
            "collisions": plan.get("collisions") or [],
            "catalog": {"size": len(catalog), "used": bool(plan.get("catalog_used")),
                        "note": ("用各厂商真实音色目录按角色卡打分"
                                 if plan.get("catalog_used")
                                 else "拉不到音色目录，退回内置 8 音色池")},
            "shots": [], "warnings": [],
        }
        if not catalog:
            res["warnings"].append(
                "没拉到音色目录（可能是没网或各厂商都没配 Key）——"
                "本次用内置的 8 个音色兜底选角，角色区分度会差一些。")
        if plan.get("unmatched"):
            res["warnings"].append(
                "这些角色没配上音色：" + "、".join(plan["unmatched"]))

        do_select = bool(payload.get("select", True))
        # ★ 把**实际生效的配音模型**一起返回：用户要的是"真正的去选择配音模型"，
        #   那结果里就得看得见"这一次用的是哪个型号"，否则选了也不知道有没有生效。
        try:
            from core.voice import dispatcher as _vd0
            _provs = await _vd0.list_providers(settings)
            res["tts"] = {
                "models": {p["name"]: p.get("selected_model") or ""
                           for p in _provs
                           if p.get("has_api_key") and p.get("supports_model")
                           and p.get("name") != "silent"},
                "no_model_engines": {p["name"]: p.get("model_note") or ""
                                     for p in _provs
                                     if p.get("has_api_key") and not p.get("supports_model")
                                     and p.get("name") != "silent"},
            }
        except Exception as e:
            logger.warning("查配音模型失败：%s", e)
        voiced_dir = os.path.join(_shots_out_dir(pid), "voiced")
        n_done = n_skip = 0
        for i, sh in enumerate(shots):
            item: Dict[str, Any] = {"index": i + 1, "ok": False, "warnings": []}
            clip = ""
            try:
                from core.compose import _selected_candidate_path as _scp
                clip = _scp(sh) or ""
            except Exception:
                clip = ""
            if not clip or not os.path.exists(clip):
                item["warnings"].append("这一镜还没有选中的片段，跳过")
                res["shots"].append(item)
                n_skip += 1
                continue
            dur = float(sh.get("duration_seconds") or 0) or 0.0
            try:
                from core.compose import probe_duration as _pd
                dur = float(await _pd(clip) or dur)
            except Exception:
                pass
            lines = _tl(sh.get("layer2_timeline"), dur)
            # ★★ 2026-09-14：用户实测「第一个镜头没有声音（剧本是有对话的）」。
            #   `caa9904f` 实测：shot#1 的时间轴 3 段**全都没有 dialogue 字段**
            #   （AI 写分镜时漏了），而剧本那一场（废墟中的铁王座：布兰/无面者）
            #   明明写着对话。这里从剧本回填，但**只填说话人确实在这一镜里的** ——
            #   这个项目的剧本被改过一稿（当前细纲是琼恩/瑟曦线），
            #   按场号硬塞会把别人的台词配到画面上，比静音更糟。
            _srcs = _script_sources_for(pid)
            _ctx = _shot_script_ctx(sh)
            _rec = None
            if not lines and _srcs:
                try:
                    from core import scriptlines as _slg
                    _inj = _slg.recover_and_inject(
                        sh, _srcs, scene_name=_ctx["scene_name"],
                        character_names=_ctx["character_names"],
                        order_index=sh.get("order_index"))
                    if _inj.get("added"):
                        sh = dict(sh)
                        sh["layer2_timeline"] = _inj["timeline"]
                        lines = _tl(_inj["timeline"], dur)
                        if lines:
                            _rec = _inj
                except Exception as e:
                    item["warnings"].append(f"从剧本回填台词失败：{e}")
            item["lines"] = len(lines)
            if not lines:
                item["warnings"].append(
                    "这一镜没有台词 —— 按你的要求不会自动加旁白念剧情；"
                    "要让角色说话请先补台词"
                    + ("。（剧本里有对话，但说话人都不在这一镜的画面/角色表里，"
                       "不硬塞）" if _srcs else ""))
                res["shots"].append(item)
                n_skip += 1
                continue
            if _rec:
                item["recovered"] = {
                    "from": _rec.get("source"), "scene": _rec.get("scene_title"),
                    "added": _rec.get("added"), "placed": _rec.get("placed"),
                    "skipped": _rec.get("skipped"),
                }
                item["warnings"].append(
                    f"📜 分镜里漏写了台词，已从剧本"
                    f"「{_rec.get('scene_title') or _rec.get('source')}」回填 "
                    f"{_rec.get('added')} 句 —— 这一镜现在有声音了。")
            try:
                dub = await _vc.dub_shot(
                    sh, characters=chars, settings=settings,
                    out_dir=os.path.join(voiced_dir, sh["id"]),
                    default_voice=dv, duration=dur, cast=plan.get("voices"),
                    catalog=catalog, script_sources=_srcs,
                    scene_name=_ctx["scene_name"],
                    shot_order=sh.get("order_index"),
                    character_names=_ctx["character_names"])
            except Exception as e:
                item["warnings"].append(f"配音失败：{e}")
                res["shots"].append(item)
                continue
            if not dub.get("ok"):
                item["warnings"].append(f"配音失败：{dub.get('error')}")
                res["shots"].append(item)
                continue
            out = os.path.join(voiced_dir, f"{sh['id']}_voiced.mp4")
            try:
                from core.compose import _mux_clip_audio as _mux
                muxed = await _mux(clip, dub["path"], out, dur)
            except Exception as e:
                muxed = False
                item["warnings"].append(f"挂音轨异常：{e}")
            if not muxed:
                item["warnings"].append("音轨没能挂进这一镜（预览仍是静音）")
                res["shots"].append(item)
                continue
            item.update({"ok": True, "path": out,
                         "url": f"/api/media?path={quote(out)}",
                         "cast": dub.get("cast") or {},
                         "prosody": dub.get("prosody_notes") or []})
            item["warnings"].extend(str(w) for w in (dub.get("warnings") or [])[:3])
            if do_select:
                try:
                    _register_local_candidate(sh, {"path": out, "duration": dur},
                                              source="voiced")
                    item["selected"] = True
                except Exception as e:
                    item["warnings"].append(f"设为选中候选失败：{e}")
            res["shots"].append(item)
            n_done += 1
        res["done"] = n_done
        res["skipped"] = n_skip
        res["summary"] = (f"{n_done} 个镜头已配好并挂上音轨"
                          + (f"，{n_skip} 个跳过（无台词或没有片段）" if n_skip else ""))
        if not n_done:
            res["ok"] = False
            res["warnings"].append(
                "一个镜头都没配上 —— 检查这些镜头是否有台词、是否已选中片段")
        return success(res)

    @app.get("/api/projects/{pid}/framesheet")
    async def project_framesheet(pid: str, per: int = Query(3), cols: int = Query(6)):
        r"""把该项目**已选中的候选片段**逐镜抽帧拼成一张联络表。

        ★ 为什么产品里要有这个：
          2026-09-13 我就是靠"眼看"发现了两件自动检测抓不住的事 ——
          有一个镜头**末尾约 1.7 秒整体倒过来**，还有两个项目的"5 个镜头"
          其实是**同一张图**（只是换了压在上面的那段文字）。
          我试过三种自动判据想认倒置，**全都不合格**（真正倒置的漏报、
          正常镜头反而误报，见 `tests/probe_flip_transition.py`）。
          所以正确的做法不是"假装能自动判"，而是**给用户一个一眼看全的入口**。

        产物落在缓存目录，返回可直接给 `<img src>` 用的 URL。
        """
        proj = db.get_project(pid)
        if not proj:
            raise error_response("项目不存在", status=404)
        try:
            from core.compose import _selected_candidate_path as _scp
        except Exception:
            _scp = None
        shots = sorted(db.list_shots(pid) or [], key=lambda s: s.get("order_index") or 0)
        clips, missing = [], []
        for i, sh in enumerate(shots):
            p = (_scp(sh) if _scp else "") or ""
            if p and os.path.exists(p):
                clips.append((f"分镜{i+1}", p))
            else:
                missing.append(f"分镜{i+1}（没有选中片段）")
        if not clips:
            raise error_response("这个项目还没有已选中的候选片段，无法抽帧", status=400)
        from core.framesheet import build_sheet
        settings = db.get_all_settings()
        cache = settings.get("cache_dir") or os.path.join(
            _os.environ.get("VIDEOFORGE_DATA_DIR") or ".", "cache")
        work = os.path.join(cache, "framesheet")
        os.makedirs(work, exist_ok=True)
        out = os.path.join(work, f"{pid[:8]}_{int(time.time())}.png")
        res = build_sheet(clips, out, per_clip=max(1, min(6, int(per or 3))),
                          cols=max(2, min(10, int(cols or 6))), work_dir=work)
        if not res.get("ok"):
            raise error_response(f"抽帧失败：{res.get('error')}", status=500)
        return success({
            "path": res["path"],
            "url": f"/api/media?path={quote(res['path'])}",
            "cells": res["cells"], "cols": res["cols"], "rows": res["rows"],
            "legend": res["legend"], "skipped": res.get("skipped") or [],
            "missing": missing, "note": res.get("note") or "",
        })

    @app.get("/api/shots/{sid}/prompt-audit")
    async def audit_shot_prompt(sid: str):
        """在**花钱之前**给这一镜的提示词做体检。

        用户要能看见"这条提示词到底合不合格、哪里不合格、怎么修"，
        而不是等生成完了拿到一段"太扯"的画面才反推。
        判据与 `core/promptaudit.py` 完全一致（错一个都算不合格）。

        ★★ 2026-09-13 修好一个**很要命的不一致**：这个接口原来**没有传**
        `prev_shot` / `next_shot`，而真正提交给付费模型的那条提示词是**传了的**
        （见 `_generate_with_api` 里的 `payload_ctx`）。于是"承接上一镜""为下一镜"
        这两段**从来没被体检过** —— 闸门检查的是一条更短的提示词。
        而"承接句"恰恰是"荒唐画面"的高发区（把上一镜的**状态**读成**动作**，
        就会拍出"主角从冰雪里爬出来"这种东西）。现在两边用同一份上下文。
        """
        shot = db.get_shot(sid)
        if not shot:
            raise error_response("分镜不存在", status=404)
        from core.videoprompt import build_video_prompt as _bvp
        settings = db.get_all_settings()
        scene = db.get_scene(shot["scene_id"]) if shot.get("scene_id") else None
        cids = shot.get("character_ids") or []
        if isinstance(cids, str):
            try:
                cids = json.loads(cids)
            except Exception:
                cids = []
        cards = [c for c in (db.get_character(c) for c in (cids or [])) if c]
        # ★ 与生成路径同源：相邻分镜也一起取，保证"体检的就是要发出去的那条"
        _prev, _next = None, None
        try:
            _all = sorted(db.list_shots(shot.get("project_id")) or [],
                          key=lambda s: s.get("order_index") or 0)
            _i = next((k for k, s in enumerate(_all) if s.get("id") == shot.get("id")), -1)
            if _i > 0:
                _prev = _all[_i - 1]
            if 0 <= _i < len(_all) - 1:
                _next = _all[_i + 1]
        except Exception as e:
            logger.warning("体检取相邻分镜失败（按无衔接体检）：%s", e)
        vp = _bvp(shot, scene=scene, characters=cards,
                  style=(settings.get("default_style") or "cinematic"),
                  provider=shot.get("model_provider") or "",
                  duration=int(shot.get("duration_seconds") or 5),
                  aspect_ratio=shot.get("aspect_ratio") or "16:9",
                  resolution=shot.get("resolution") or "720p",
                  prev_shot=_prev, next_shot=_next)
        au = vp.get("audit") or {}
        return success({
            "prompt": vp["prompt"],
            "negative_prompt": vp.get("negative_prompt") or "",
            "audit": au,
            "camera_moves_seen": (vp.get("meta") or {}).get("camera_moves_seen"),
            "camera_conflict": (vp.get("meta") or {}).get("camera_conflict"),
            "has_action": (vp.get("meta") or {}).get("has_action"),
        })

    @app.post("/api/shots/{sid}/render")
    async def render_shot(sid: str, payload: Dict[str, Any] = None):
        """把一个分镜渲染成可播放 mp4 —— 不需要任何付费视频模型 Key。

        mode:
          local → 本地合成（图片/AI 生成 + TTS 配音 + 运镜 + 字幕）
          api   → 走真实视频模型适配器（需要有对应厂商 Key）
          auto  → 有 Key 走 api，没 Key 自动降级为 local（默认）
        """
        payload = payload or {}
        shot = db.get_shot(sid)
        if not shot:
            raise error_response("分镜不存在", status=404)
        if _shot_render_state.get(sid, {}).get("running"):
            return success({"started": False, "message": "该分镜正在渲染中"})

        settings = db.get_all_settings()
        mode = (payload.get("mode") or "auto").lower()
        if payload.get("prefer_local"):
            mode = "local"
        provider = payload.get("model_provider") or shot.get("model_provider") or "kling"
        model_name = payload.get("model_name") or shot.get("model_name") or ""
        # ★ 自动纠正"厂商与模型名不匹配"的历史脏数据
        #   （早期换模型弹窗不联动，库里留下了 hailuo + kling-1.6 这种组合）
        model_fix_note = ""
        if not model_name_matches_provider(provider, model_name):
            fixed = DEFAULT_MODEL_IDS.get(provider, "")
            if fixed:
                model_fix_note = (f"分镜记录的模型名「{model_name}」不属于厂商「{provider}」，"
                                  f"已自动改用该厂商默认模型「{fixed}」")
                logger.info("纠正模型名不匹配：%s", model_fix_note)
                model_name = fixed
                try:
                    db.update_shot(sid, model_name=fixed)
                except Exception:
                    pass
        api_keys = settings.get("api_keys") or {}
        has_key = bool(_lookup_key_ci(api_keys, provider))
        if mode == "api" and not has_key:
            raise error_response(
                f"「{provider}」没有配置 API Key，无法走真实视频模型。"
                f"可改用「本地合成」模式（不需要 Key），或到「设置」里填 Key。", status=400)

        # ══════════════════════════════════════════════════════════════
        # ★★ 生成前闸门：**先审提示词，再花钱**（2026-09-13）
        # ══════════════════════════════════════════════════════════════
        # 用户原话："视频分镜里面有的视频太扯了……有个主角还从冰雪里面爬出来"
        # 这类画面有一大半**不是模型的问题，是提示词本身不合格** ——
        # 没写结束构图、没写音频边界、同时给了两个运镜、没说画面里几个人。
        # 实测改造前 16 条真实提示词**全部**不合格（平均分 59.7）。
        # 而每次失败都要**真金白银**（一次生成几毛到几块），所以必须在提交之前拦。
        #
        # 判据来自参考方法论（`project-qa-gates.md`）：**平均分不能抵消 hard gate**，
        # 只要有 error 就不该进入生成。想硬上可以传 `force=true`（把选择权给用户，
        # 不是替他决定）。
        _will_spend = (mode == "api") or (mode == "auto" and has_key)

        # ★ 2026-10-20 修复：`target` 原来在下方 2477 行才赋值，而 2455 行的
        #   提示词审计就已经用它 —— 每次抛 NameError 被 except 以"放行"吞掉，
        #   于是「先审提示词、再花钱」这道闸**从来没有真正生效过**
        #   （实测日志：生成前提示词自检失败（放行）：cannot access local
        #    variable 'target' where it is not associated with a value）。
        target = payload.get("duration_seconds")

        # ══════════════════════════════════════════════════════════════
        # ★★ 预算闸：**先算钱，再花钱**（2026-10-20 新增）
        # ══════════════════════════════════════════════════════════════
        # 之前 dev 产品里 default_video_budget_yuan / allow_video_spend
        # 两个设置**没有任何代码读**，生成一次真视频可以无限花钱。
        # 现在：预算未设置 → 拒；本次估算 > 剩余 → 拒；用户关了开关 → 拒。
        if _will_spend and not payload.get("force"):
            from core.budget import (BudgetExceeded, assert_spend_allowed,
                                     check_budget)
            from core.shot_segments import _estimate_segment_cost_yuan as _estc
            try:
                assert_spend_allowed(db, provider=provider)
                _n_cand = max(1, int(payload.get("num_candidates") or 1))
                _dur_est = float(target or shot.get("duration_seconds") or 5)
                _one_est = _estc(provider, _dur_est,
                                 shot.get("resolution") or "720p")
                check_budget(shot.get("project_id") or "",
                             round(_one_est * _n_cand, 2), db=db,
                             provider=provider,
                             detail=f"（{_n_cand} 个候选 × {_dur_est:.0f} 秒）")
            except BudgetExceeded as _be:
                raise error_response(str(_be), status=402)

        if _will_spend and not payload.get("force"):
            try:
                from core.videoprompt import build_video_prompt as _bvp
                _scene = db.get_scene(shot["scene_id"]) if shot.get("scene_id") else None
                _cids = shot.get("character_ids") or []
                if isinstance(_cids, str):
                    try:
                        _cids = json.loads(_cids)
                    except Exception:
                        _cids = []
                _cards = [db.get_character(c) for c in (_cids or [])]
                _cards = [c for c in _cards if c]
                _dur = int(target or shot.get("duration_seconds") or 5)
                _vp = _bvp(shot, scene=_scene, characters=_cards,
                           style=(settings.get("default_style") or "cinematic"),
                           provider=provider, duration=_dur,
                           aspect_ratio=shot.get("aspect_ratio") or "16:9",
                           resolution=shot.get("resolution") or "720p")
                _au = _vp.get("audit") or {}
                if _au.get("error_count"):
                    _bad = _au.get("errors") or []
                    _lines = "；".join(
                        f"【{i['code']}】{i['message']}（修法：{i['fix']}）" for i in _bad[:3])
                    raise error_response(
                        f"提示词还不合格，**已拦下、没有花这次的钱**。"
                        f"审计分 {_au.get('score')}/100，{len(_bad)} 处硬伤：{_lines}"
                        f"　想照原样硬上就传 force=true。", status=400)
            except HTTPException:
                raise
            except Exception as _e:
                # 闸门自己出问题不许把正常生成挡住（宁可放过，不可误拦）
                logger.warning("生成前提示词自检失败（放行）：%s", _e)

        voice_id = payload.get("voice_id") or "edge:zh-CN-XiaoxiaoNeural"
        target = payload.get("duration_seconds")
        state = {"running": True, "percent": 0.0, "message": "准备中…",
                 "mode": mode, "used": "", "result": None, "error": ""}
        _shot_render_state[sid] = state

        async def _worker():
            warnings: List[str] = []
            if model_fix_note:
                warnings.append(model_fix_note)
            try:
                # ── 第一步：若配置了真实模型 Key，先试真实模型 ──
                if mode in ("auto", "api") and has_key:
                    # ★★ 2026-10-24：型号不支持参考视频（seedance 2.0 系列）时，
                    #   走**灰模首尾帧独立通道**（绕开场景图首帧决策链）。
                    #   证据：判定实验只给灰模首帧+末帧 → SSIM 0.913/0.905；
                    #   而旧链路连续 3 轮首帧都被场景图占用（0.44/0.45/0.47）。
                    _use_pvd = False
                    try:
                        from core import previz_drive as _pvd
                        _use_pvd = _pvd.should_use(shot, provider, model_name)
                    except Exception as _e_pvd:  # noqa: BLE001
                        logger.warning("灰模驱动判定异常（按普通方式继续）：%s", _e_pvd)
                    if _use_pvd:
                        state["message"] = "灰模首尾帧驱动出片中…"
                    state["message"] = f"调用真实模型 {provider}…"
                    state["percent"] = 5.0
                    if _use_pvd:
                        api_res = await _pvd.generate(shot, settings, provider, model_name,
                                                      duration_override=target)
                    else:
                        api_res = await _try_api_video(shot, settings, provider, model_name,
                                                   duration_override=target,
                                                   chain=bool(payload.get("chain", True)),
                                                   ref_priority=str(
                                                       payload.get("ref_priority")
                                                       or "auto"),
                                                   with_voice=bool(
                                                       payload.get("with_voice", True)))
                    if api_res.get("ok"):
                        _register_local_candidate(shot, api_res, source="api")
                        # ★ 2026-10-20：真实模型成功 → 记入项目成本（预算累计）
                        try:
                            from core.budget import add_cost
                            from core.shot_segments import (
                                _estimate_segment_cost_yuan as _estc2)
                            add_cost(
                                shot.get("project_id") or "",
                                _estc2(provider,
                                       float(api_res.get("duration")
                                             or target or 5),
                                       shot.get("resolution") or "720p") if not api_res.get("cost_recorded") else 0.0,
                                db=db)
                        except Exception as _ce:
                            logger.warning("成本累计失败（不影响出片）：%s", _ce)
                        _cw = list(api_res.get("warnings") or [])
                        if api_res.get("chain_from"):
                            _cw.append("本镜用了上一镜的尾帧作为首帧，画面是承接过来的")
                        state["result"] = {"ok": True, "path": api_res["path"],
                                           "url": api_res.get("url"),
                                           "duration": api_res.get("duration") or 0,
                                           "source": "api", "provider": provider,
                                           "delivery": api_res.get("delivery") or {},
                                           "model": model_name,
                                           "warnings": _cw, "errors": [],
                                           "narration": "", "image_source": "api",
                                           "chain_from": api_res.get("chain_from") or "",
                                           "subtitles": []}
                        state["used"] = "api"
                        state["percent"] = 100.0
                        state["message"] = f"真实模型完成（{api_res.get('duration') or 0:.1f}s）"
                        state["running"] = False
                        return
                    # 真实模型失败
                    err = api_res.get("error") or "未知错误"
                    hint = api_res.get("hint") or ""
                    logger.warning("真实模型 %s 失败：%s", provider, err)
                    if mode == "api" or api_res.get("no_fallback") or (
                            provider == "seedance_t6" and model_name == "seedance-2-0-promo"):
                        state["result"] = api_res
                        state["used"] = "api"
                        state["running"] = False
                        state["error"] = f"真实模型失败：{err}" + (f"\n建议：{hint}" if hint else "")
                        state["message"] = state["error"]
                        db.update_shot(sid, status="failed", error_message=state["error"][:800])
                        return
                    warnings.append(f"真实模型（{provider}）失败，已自动改用本地合成：{err}"
                                    + (f"（{hint}）" if hint else ""))
                    state["message"] = "真实模型失败，改用本地合成…"
                    state["percent"] = 8.0
                elif mode in ("auto", "api") and not has_key:
                    if provider == "seedance_t6" and model_name == "seedance-2-0-promo":
                        state.update(running=False, used="api", error="Seedance API Key 未配置，未生成、未交付。",
                                     message="Seedance API Key 未配置；不会自动改为本地合成。")
                        db.update_shot(sid, status="failed", error_message=state["error"])
                        return
                    # ⚠ 这里以前是**静默降级**：用户把分镜换成自己的模型后点生成，
                    #   因为那个模型没配 Key，程序一声不响地用了本地合成，
                    #   用户看到的现象就是"改了模型还是用本地的，用不上自己的模型"。
                    reason = (f"分镜当前模型「{provider} / {model_name or '默认'}」没有配置 API Key，"
                              f"已改用本地合成。要使用真实模型：① 到「设置 → 视频模型 API Keys」"
                              f"填好该厂商的 Key；② 或在「生成」页把生成方式设为「只用真实模型」看具体报错。")
                    warnings.append(reason)
                    state["message"] = f"{provider} 未配置 Key，改用本地合成"
                    logger.info("auto 模式降级：%s", reason)

                # ── 第二步：本地合成（不需要任何 Key）──
                res = await _local_render_shot(shot, settings, payload, state, warnings)
                res.setdefault("warnings", [])
                res["warnings"] = list(res.get("warnings") or []) + warnings
                state["used"] = "local"
                state["result"] = res
                state["running"] = False
                if res.get("ok"):
                    _register_local_candidate(shot, res, source="local")
                    state["percent"] = 100.0
                    state["message"] = f"本地合成完成（{res['duration']:.1f}s）"
                else:
                    state["error"] = "；".join(res.get("errors") or []) or "渲染失败"
                    state["message"] = state["error"]
                    db.update_shot(sid, status="failed", error_message=state["error"][:800])
            except Exception as e:
                logger.exception("render_shot worker crashed")
                state["running"] = False
                state["error"] = f"{type(e).__name__}: {e}"
                state["message"] = state["error"]
                try:
                    db.update_shot(sid, status="failed", error_message=state["error"][:800])
                except Exception:
                    pass

        asyncio.create_task(_worker())
        return success({"started": True, "mode": mode, "voice_id": voice_id,
                        "target_seconds": target})

    @app.post("/api/shots/{sid}/reference-audit")
    async def reference_audit(sid: str, payload: Dict[str, Any] = None):
        """Recheck a registered candidate against the actual whitebox, without generation or selection."""
        payload = payload or {}
        shot = db.get_shot(sid)
        if not shot:
            raise error_response("分镜不存在", status=404)
        candidates = shot.get("candidates") or []
        if isinstance(candidates, str):
            candidates = json.loads(candidates or "[]")
        requested = payload.get("candidate_id") or shot.get("selected_candidate_id")
        candidate = next((item for item in candidates if isinstance(item, dict) and (
            item.get("candidate_id") == requested if requested else item.get("is_selected"))), None)
        if not candidate:
            raise error_response("没有找到该分镜的已登记候选；请指定 candidate_id", status=404)
        from core.previz_ref import white_model_video
        reference = white_model_video(shot)
        if not reference:
            raise error_response("没有可用的给定白膜视频，无法确认一致性", status=400)
        from core.strict_reference import evaluate_reference
        folder = Path(data_root) / "outputs" / shot["project_id"] / "reference-audits"
        report = folder / f"{sid}-{time.time_ns()}.json"
        audit = await asyncio.to_thread(evaluate_reference, reference, candidate.get("path") or "",
            contract={"duration": float(shot.get("duration_seconds") or 0), "fps": 24,
                      "aspect_ratio": shot.get("aspect_ratio") or "16:9"}, output_report=str(report))
        return success({"ok": audit["ok"], "status": audit["status"], "candidate_id": candidate.get("candidate_id"),
                        "failures": audit["failures"], "frames_compared": audit["metrics"].get("frames_compared", 0),
                        "report_path": str(report), "report_url": f"/api/media?path={quote(str(report))}",
                        "selection_changed": False, "paid_calls": 0})

    @app.get("/api/shots/{sid}/render/status")
    async def render_shot_status(sid: str):
        st = _shot_render_state.get(sid)
        if not st:
            return success({"running": False, "percent": 0.0, "message": "尚未渲染",
                            "result": None, "error": ""})
        return success(dict(st))

    @app.post("/api/projects/{pid}/render-all")
    async def render_all_shots(pid: str, payload: Dict[str, Any] = None):
        """批量渲染该项目所有分镜（顺序执行，避免同时开一堆 FFmpeg）"""
        payload = payload or {}
        shots = db.list_shots(pid) or []
        if not shots:
            raise error_response("该项目还没有分镜，请先在「分镜」页生成", status=400)
        voice_id = payload.get("voice_id") or "edge:zh-CN-XiaoxiaoNeural"
        target = payload.get("duration_seconds")
        auto_images = bool(payload.get("auto_images", True))
        image_provider = str(payload.get("image_provider") or "minimax")
        st = {"running": True, "percent": 0.0, "total": len(shots), "done": 0,
              "message": f"准备渲染 {len(shots)} 个分镜…", "error": "", "failed": []}
        _shot_render_state[f"__all__{pid}"] = st

        async def _worker():
            from core.compose import render_shot_clip
            settings = db.get_all_settings()
            for i, shot in enumerate(shots):
                if not _shot_render_state[f"__all__{pid}"].get("running"):
                    # ★ 以前这里直接 break —— 剩下的分镜就**永远停在 pending、
                    #   没有任何错误信息**。线上实测：58 个分镜里 41 个是这种状态，
                    #   用户看到的就是"很多分镜的视频根本没生成"，
                    #   而且**一个错误提示都没有**，完全无从下手。
                    #   现在改成：明确记下"被中断、未渲染"，状态写成 failed 并带原因。
                    for rest in shots[i:]:
                        try:
                            db.update_shot(
                                rest["id"], status="failed",
                                error_message="批量渲染被中断（可能是上一次任务被取消、"
                                              "服务重启，或中途点了取消），这个分镜没有渲染。"
                                              "重新点一次「批量渲染」会从没有视频的分镜继续。")
                        except Exception:
                            pass
                        st["failed"].append({"shot_id": rest["id"],
                                             "error": "被中断，未渲染"})
                    st["message"] = (f"已中断：还剩 {len(shots) - i} 个分镜没渲染"
                                     f"（已标记出来，不会静默留下 pending）")
                    break
                st["message"] = f"渲染 {i + 1}/{len(shots)}…"
                try:
                    out = os.path.join(_shots_out_dir(pid), f"{shot['id']}.mp4")
                    res = await render_shot_clip(
                        shot, db=db, settings=settings, voice_id=voice_id,
                        with_voice=bool(payload.get("with_voice", True)),
                        target_seconds=float(target) if target else None,
                        auto_images=auto_images, image_provider=image_provider,
                        image_size="720x1280" if shot.get("aspect_ratio") == "9:16" else "1280x720",
                        out_path=out, idx=i)
                    if res.get("ok"):
                        cands = shot.get("candidates") or []
                        if isinstance(cands, str):
                            try:
                                cands = json.loads(cands)
                            except Exception:
                                cands = []
                        cands = [c for c in cands if isinstance(c, dict)]
                        for c in cands:
                            c["is_selected"] = False
                        cid = f"local_{int(time.time())}"
                        cands.append({"candidate_id": cid, "path": res["path"],
                                      "url": f"/api/media?path={res['path']}",
                                      "duration_seconds": res["duration"], "is_selected": True,
                                      "source": "local", "created_at": time.time()})
                        db.update_shot(shot["id"], status="completed", progress=1.0,
                                       candidates=cands, selected_candidate_id=cid,
                                       error_message="")
                    else:
                        _err = "；".join(res.get("errors") or []) or "渲染未成功（无具体原因）"
                        st["failed"].append({"shot_id": shot["id"], "error": _err})
                        db.update_shot(shot["id"], status="failed",
                                       error_message=_err[:500])
                except Exception as e:
                    logger.exception("render-all item failed")
                    _err = f"{type(e).__name__}: {e}"
                    st["failed"].append({"shot_id": shot["id"], "error": _err})
                    # ★ 抛异常时也要落库。原来只 append 到内存里的 st["failed"]，
                    #   分镜本身仍是 pending + 空错误信息 —— 用户看不到任何线索。
                    try:
                        db.update_shot(shot["id"], status="failed",
                                       error_message=f"渲染出错：{_err[:480]}")
                    except Exception:
                        pass
                st["done"] = i + 1
                st["percent"] = round((i + 1) / len(shots) * 100, 1)
            st["running"] = False
            st["message"] = (f"完成：成功 {st['done'] - len(st['failed'])} / {len(shots)}"
                             + (f"，失败 {len(st['failed'])}" if st["failed"] else ""))

        asyncio.create_task(_worker())
        return success({"started": True, "total": len(shots)})

    @app.post("/api/projects/{pid}/regen-chained")
    async def regen_chained(pid: str, payload: Dict[str, Any] = None):
        """**按镜头顺序**用真实模型重新生成，并启用尾帧串联 —— 把"一镜到底"的最后一步交到手上。

        ══════════════════════════════════════════════════════════════
        为什么需要这个批量动作
        ══════════════════════════════════════════════════════════════
        尾帧串联（上一镜的尾帧当这一镜的首帧）是让多镜头看起来像一镜到底
        **最直接**的一招。但实测全库 49 个相邻接缝里，只有 **71% 在同一场景内**
        （那些才该串），剩下 29% 是换场景、本来就该硬切。

        而这件事**只能靠重新生成**实现：已有的片段是各自独立生成的，
        后期再怎么调色/转场/裁帧都补不上"它们本该连续"这件事
        （实测：`d5815c1e` 1 模型 1 场景 5 镜，接缝仍是自身运动尺度的 12~13 倍）。

        现有的「批量渲染」走的是**本地合成**（`render_shot_clip`），
        根本不调模型、也没有串联；要串只能一个个手点「生成」，
        还得自己保证顺序（上一镜必须先有视频，尾帧才存在）。

        这个动作把三件事一次做完：
          ① **按 order_index 顺序**逐镜生成（顺序是前提，不能并发）
          ② 每镜都 **chain=True**（自动判断同场景才串）
          ③ 前后各量一次接缝指标，**把"变好了多少"直接给出来**

        ══════════════════════════════════════════════════════════════
        ★ 这**会花钱**：每镜一次真实模型调用。所以
          · `dry_run=True` 只报告"会调用几次、哪些镜会串、预期能覆盖多少接缝"
          · 真正的执行要显式传 `dry_run=false`
        """
        payload = payload or {}
        dry = payload.get("dry_run", True)
        if isinstance(dry, str):
            dry = dry.strip().lower() not in ("0", "false", "no", "")
        shots = sorted(db.list_shots(pid) or [],
                       key=lambda s: s.get("order_index") or 0)
        if not shots:
            raise error_response("该项目还没有分镜，请先在「分镜」页生成", status=400)
        if _shot_render_state.get(f"__chain__{pid}", {}).get("running"):
            return success({"started": False, "message": "该项目正在按序重生成中，请稍候",
                            "status": _shot_render_state[f"__chain__{pid}"]})

        settings = db.get_all_settings()
        only_missing = bool(payload.get("only_missing", False))

        # ── 先算"会调用几次、哪些会串" ──
        plan: List[Dict[str, Any]] = []
        for i, sh in enumerate(shots):
            provider = (payload.get("model_provider") or sh.get("model_provider")
                        or settings.get("default_model_provider") or "")
            model_name = (payload.get("model_name") or sh.get("model_name")
                          or settings.get("default_model_name") or "")
            prev = shots[i - 1] if i > 0 else None
            from core.continuity import chain_decision, supports_first_frame
            dec = chain_decision(
                prev, sh,
                prev_has_video=bool(_selected_video_of(prev)) if prev else False,
                model_supports_first_frame=supports_first_frame(provider, model_name))
            has_key = bool(_lookup_key_ci(settings.get("api_keys") or {}, provider))
            would_skip = only_missing and bool(_selected_video_of(sh))
            plan.append({
                "index": (sh.get("order_index") or 0) + 1, "shot_id": sh["id"],
                "provider": provider, "model_name": model_name,
                "has_key": has_key, "will_chain": bool(dec.get("chain")),
                "chain_why": dec.get("why") or "",
                "chain_skip": dec.get("skipped_reason") or "",
                "skip": would_skip,
            })
        n_calls = sum(1 for p in plan if not p["skip"] and p["has_key"])
        n_chain = sum(1 for p in plan if p["will_chain"] and not p["skip"])
        no_key = sorted({p["provider"] for p in plan if not p["has_key"] and not p["skip"]})

        if dry:
            return success({
                "dry_run": True, "total_shots": len(shots),
                "will_call_model_times": n_calls,
                "will_chain_times": n_chain,
                "providers_without_key": no_key,
                "plan": plan,
                "note": (f"预演：会调用真实模型 {n_calls} 次，其中 {n_chain} 次用上尾帧串联。"
                         f"**没有生成任何内容、没有花钱。**"
                         + (f" 注意：{ '、'.join(no_key) } 没配 API Key，这些镜头会被跳过。"
                            if no_key else "")),
                "cost_warning": f"真正执行会调用真实视频模型 {n_calls} 次（每次都可能计费）。",
            })

        st = {"running": True, "percent": 0.0, "total": len(shots), "done": 0,
              "message": f"准备按序重生成 {len(shots)} 个镜头…", "error": "",
              "plan": plan, "results": [], "seam_before": None, "seam_after": None,
              "will_call_model_times": n_calls, "will_chain_times": n_chain}
        _shot_render_state[f"__chain__{pid}"] = st

        def _clip_paths() -> List[str]:
            """按顺序取每个镜头当前选中的视频。

            ★ 必须**重新读库**，不能用请求进来时抓的那份 `shots`：
              那份的 `candidates` 是生成之前的旧值。曾经因为这个，
              before/after 两次量的是**同一批旧路径**，
              指标一模一样（14.123 → 14.123），看起来像"串联没用"——
              其实是量错了对象。
            """
            try:
                fresh = {s["id"]: s for s in (db.list_shots(pid) or [])}
            except Exception:
                fresh = {}
            return [_selected_video_of(fresh.get(s["id"]) or s) for s in shots]
        before_clips = _clip_paths()

        async def _worker():
            try:
                from core import seamfix as _sf
                st["message"] = "先量一次当前接缝（作为对比基准）…"
                st["percent"] = 2.0
                _bs = []
                for c in before_clips:
                    if c and os.path.exists(c):
                        try:
                            _bs.append(await _sf.analyze_clip(c))
                        except Exception:
                            pass
                if len(_bs) >= 2:
                    _rep = _sf.seam_report(_bs)
                    _tex = _sf.texture_report(_bs)
                    st["seam_before"] = {"mean_seam_ratio": _rep["mean_seam_ratio"],
                                         "seams": _rep["seams"],
                                         "texture_ratio": _tex["texture_ratio"]}

                for i, sh in enumerate(shots):
                    if not _shot_render_state[f"__chain__{pid}"].get("running"):
                        break
                    p = plan[i]
                    if p["skip"]:
                        st["results"].append({**p, "ok": False, "skipped": True,
                                              "error": "已有视频，按设置跳过"})
                        st["done"] += 1
                        continue
                    if not p["has_key"]:
                        st["results"].append({**p, "ok": False,
                                              "error": f"「{p['provider']}」没有配 API Key"})
                        st["done"] += 1
                        continue
                    st["message"] = (f"生成第 {p['index']}/{len(shots)} 个镜头"
                                     f"（{'接上一镜尾帧' if p['will_chain'] else '独立开场'}）…")
                    st["percent"] = round(5 + 90 * i / max(1, len(shots)), 1)
                    try:
                        # 每一镜生成前重读一次 shot：上一镜刚写好候选，
                        # `_chain_first_frame` 要靠它拿到"上一镜的尾帧"
                        fresh = db.get_shot(sh["id"]) or sh
                        res = await _try_api_video(fresh, settings, p["provider"],
                                                   p["model_name"], chain=True)
                        if res.get("ok"):
                            _register_local_candidate(fresh, res, source="api")
                            st["results"].append({
                                **p, "ok": True,
                                "chained": bool(res.get("chain_from")),
                                "chain_from": res.get("chain_from") or "",
                                "duration": res.get("duration"),
                                "segments": res.get("segments"),
                                "warnings": res.get("warnings") or [],
                            })
                        else:
                            st["results"].append({**p, "ok": False,
                                                  "error": res.get("error") or "未知错误"})
                    except Exception as e:
                        logger.exception("regen-chained 第 %d 镜失败", p["index"])
                        st["results"].append({**p, "ok": False,
                                              "error": f"{type(e).__name__}: {e}"})
                    st["done"] += 1
                    st["percent"] = round(5 + 90 * (i + 1) / max(1, len(shots)), 1)

                # ── 再量一次接缝，给出前后对比 ──
                st["message"] = "重新量接缝（和刚才的基准对比）…"
                st["percent"] = 97.0
                after_clips = _clip_paths()
                _as = []
                for c in after_clips:
                    if c and os.path.exists(c):
                        try:
                            _as.append(await _sf.analyze_clip(c))
                        except Exception:
                            pass
                if len(_as) >= 2:
                    _rep2 = _sf.seam_report(_as)
                    _tex2 = _sf.texture_report(_as)
                    st["seam_after"] = {"mean_seam_ratio": _rep2["mean_seam_ratio"],
                                        "seams": _rep2["seams"],
                                        "texture_ratio": _tex2["texture_ratio"]}
                _b = (st.get("seam_before") or {}).get("mean_seam_ratio")
                _a = (st.get("seam_after") or {}).get("mean_seam_ratio")
                if _b is not None and _a is not None:
                    st["message"] = (f"完成：接缝强度 {_b} → {_a}"
                                     f"（越接近 1 越像一镜到底）")
                else:
                    st["message"] = "完成（接缝指标不足，无法对比）"
                st["percent"] = 100.0
            except Exception as e:
                logger.exception("regen-chained 失败")
                st["error"] = f"{type(e).__name__}: {e}"
                st["message"] = st["error"]
            finally:
                st["running"] = False

        asyncio.create_task(_worker())
        return success({"started": True, "will_call_model_times": n_calls,
                        "will_chain_times": n_chain,
                        "cost_warning": f"开始按序重生成：会调用真实模型 {n_calls} 次。"})

    @app.get("/api/projects/{pid}/regen-chained/status")
    async def regen_chained_status(pid: str):
        stt = _shot_render_state.get(f"__chain__{pid}")
        if not stt:
            return success({"running": False, "percent": 0.0, "message": "尚未执行",
                            "results": [], "seam_before": None, "seam_after": None})
        return success(dict(stt))

    @app.get("/api/projects/{pid}/render-all/status")
    async def render_all_status(pid: str):
        st = _shot_render_state.get(f"__all__{pid}")
        return success(st or {"running": False, "percent": 0.0, "message": "尚未开始"})

    @app.get("/api/media")
    async def local_media(path: str, download: int = 0):
        """安全地返回本地**视频/音频/图片**文件（限制在数据/缓存/输出目录内）。

        WebView2 里 `file://` 会被安全策略拦掉，所以成片/分镜视频必须走 HTTP。

        `download=1` 时附带 `Content-Disposition: attachment` —— 否则浏览器/WebView2
        会把它当成"在线播放"而不是下载，用户点「⬇ 下载」看不到文件落地。
        """
        settings = db.get_all_settings()
        roots = [
            os.path.abspath(str(settings.get("cache_dir") or "")),
            os.path.abspath(str(settings.get("output_dir") or "")),
            os.path.abspath(os.environ.get("VIDEOFORGE_DATA_DIR", "")),
        ]
        roots = [r for r in roots if r and r != os.path.abspath("")]
        target = os.path.abspath(path or "")
        if not target or not any(target.startswith(r + os.sep) or target == r for r in roots):
            raise error_response("路径不在允许的目录范围内", status=403)
        if not os.path.exists(target):
            raise error_response("文件不存在（可能已被清理）", status=404)
        ext = os.path.splitext(target)[1].lower()
        mime = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
                ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
                ".ogg": "audio/ogg", ".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "application/octet-stream")
        if download:
            from urllib.parse import quote as _q
            fn = os.path.basename(target)
            return FileResponse(target, media_type=mime, headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{_q(fn)}",
            })
        return FileResponse(target, media_type=mime)

    # ──────────── 输出目录 / 原生目录选择 ────────────

    @app.post("/api/settings/output-dir")
    async def set_output_dir(payload: Dict[str, Any]):
        """自定义成片输出目录（会做可写性校验，避免选到只读位置）"""
        path = (payload.get("path") or "").strip().strip('"')
        if not path:
            raise error_response("请提供目录路径", status=400)
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".vf_write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
        except Exception as e:
            raise error_response(f"该目录不可写：{e}", status=400)
        db.set_setting("output_dir", path)
        return success({"output_dir": path})

    @app.post("/api/dialog/pick-folder")
    async def pick_folder(payload: Dict[str, Any] = None):
        """弹出 Windows 原生「选择文件夹」对话框（用 PowerShell，不依赖 tkinter）"""
        payload = payload or {}
        initial = payload.get("initial") or _outputs_root()
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            f"$d.SelectedPath = '{str(initial).replace(chr(39), chr(39) * 2)}';"
            "$d.Description = '选择成片输出目录';"
            "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ [Console]::Out.Write($d.SelectedPath) }"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-STA", "-Command", ps,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
            picked = (out or b"").decode("utf-8", "ignore").strip()
            return success({"path": picked, "cancelled": not picked})
        except asyncio.TimeoutError:
            raise error_response("选择目录超时（对话框可能被其他窗口遮挡）", status=408)
        except Exception as e:
            raise error_response(f"无法打开目录选择框：{e}", status=500)

    def _build_link_index(pid: str):
        """构建匹配索引 + 匹配器（供 from-script 与 relink 共用）"""
        char_list = db.list_characters(pid) or []
        scene_list = db.list_scenes(pid) or []
        details_map: Dict[int, dict] = {}
        try:
            for d in (db.list_scene_details(pid) or []):
                if isinstance(d, dict) and d.get("scene_number"):
                    details_map[d["scene_number"]] = d
        except Exception:
            pass

        def norm(s) -> str:
            s = str(s or "").strip()
            s = re.sub(r"[（(][^）)]*[）)]", "", s)
            s = re.sub(r"[，,、·\s\-—_]+", "", s)
            return s

        scene_by_norm = {}
        for sc in scene_list:
            for key in (norm(sc.get("name")), str(sc.get("name") or "").strip()):
                if key:
                    scene_by_norm.setdefault(key, sc)
        chars_sorted = sorted(char_list, key=lambda c: -len(str(c.get("name") or "")))

        def match_char_name(name) -> Optional[dict]:
            """剧本称呼 → 角色资产：全等 → 别名 → 双向包含（「雪诺」↔「琼恩·雪诺」）"""
            nm = str(name or "").strip()
            if not nm:
                return None
            for c in char_list:
                if str(c.get("name") or "").strip() == nm:
                    return c
            for c in char_list:
                try:
                    rf = c.get("reference_features")
                    rf = json.loads(rf) if isinstance(rf, str) else (rf or {})
                    for a in (rf.get("aliases") or []):
                        if str(a).strip() == nm:
                            return c
                except Exception:
                    pass
            n2 = norm(nm)
            if not n2:
                return None
            for c in chars_sorted:      # 先长后短，避免「丹妮莉丝」抢走「丹妮莉丝·坦格利安」
                cn = norm(c.get("name"))
                if cn and (cn == n2 or cn in n2 or n2 in cn):
                    return c
            return None

        def match_scene_names(*cands) -> Optional[str]:
            for cand in cands:
                if not cand:
                    continue
                key = norm(cand)
                if not key:
                    continue
                if key in scene_by_norm:
                    return scene_by_norm[key]["id"]
                for k, sc in scene_by_norm.items():
                    if k and len(k) >= 2 and (k in key or key in k):
                        return sc["id"]
            return None

        def match_chars_of(num, scene, tlp) -> List[str]:
            """优先级：**剧本细节的 characters** > 场次 characters > 文本命中
            （细节里的名字更完整：剧本写「雪诺」，细节写「琼恩·雪诺」）"""
            names: List[str] = []
            detail = details_map.get(num) or {}
            for src in (detail.get("characters"), (scene or {}).get("characters")):
                if isinstance(src, list) and src:
                    names = [(x.get("name") if isinstance(x, dict) else x) for x in src]
                    names = [str(x).strip() for x in names if x]
                    if names:
                        break
            if not names:
                for blob in (json.dumps(tlp, ensure_ascii=False) if isinstance(tlp, dict) else "",
                             json.dumps(scene or {}, ensure_ascii=False)):
                    if not blob:
                        continue
                    hit = [str(c["name"]) for c in chars_sorted if c.get("name") and str(c["name"]) in blob]
                    if hit:
                        names = hit
                        break
            ids: List[str] = []
            for n in names:
                c = match_char_name(n)
                if c and c["id"] not in ids:
                    ids.append(c["id"])
            if not ids:
                blob = json.dumps({"s": scene, "d": detail}, ensure_ascii=False)
                for c in chars_sorted:
                    nm = str(c.get("name") or "")
                    if nm and nm in blob and c["id"] not in ids:
                        ids.append(c["id"])
            return ids

        return {"details_map": details_map, "match_scene_names": match_scene_names,
                "match_chars_of": match_chars_of, "chars": char_list,
                "scenes": scene_list,
                "char_count": len(char_list), "scene_count": len(scene_list)}

    @app.get("/api/projects/{pid}/shots/ref-check")
    async def shots_ref_check(pid: str):
        """**分镜资产关联体检**：哪些镜头引用了不存在的角色 / 文字提到但没关联。

        ★ 为什么需要它（2026-09-14 用户实测"关联了角色人却没出现"）：
          角色卡被删除/重建后，分镜上会留下**悬空 ID**，我们静默丢掉 →
          提示词里少了主体 → 模型**自己编一个人**，而界面上一点提示都没有。
          参考方法论里这就是 `MISSING_REFERENCE`（引用不存在的资产 = error），
          属于**生成前门**该拦的东西。
        """
        from core import refcheck as _rc
        shots = db.list_shots(pid) or []
        if not shots:
            raise error_response("这个项目还没有分镜", status=400)
        chars = db.list_characters(pid) or []
        scenes = db.list_scenes(pid) or []
        return success(_rc.check_project_shots(shots, chars, scenes))

    @app.post("/api/projects/{pid}/shots/relink")
    async def relink_shots(pid: str):
        """**重新关联已有分镜**的角色与场景（不重建分镜、不丢已生成的视频）。

        为什么需要：早期版本生成的分镜根本没有 scene_id / character_ids
        （甚至把全部角色塞给每个分镜），界面上就显示「未关联角色/场景」；
        而「从剧本生成」会删掉旧分镜、**连带丢掉已渲染好的视频**。
        这个接口按 order_index 原地补关联。
        """
        script = db.get_script(pid)
        shots = db.list_shots(pid) or []
        if not shots:
            raise error_response("该项目还没有分镜", status=400)
        idx = _build_link_index(pid)
        scenes = (script or {}).get("scenes")
        if isinstance(scenes, str):
            try:
                scenes = json.loads(scenes)
            except Exception:
                scenes = []
        scenes = scenes if isinstance(scenes, list) else []
        tlp_all = (script or {}).get("three_layer_prompts") or {}

        updated, detail = 0, []
        for shot in shots:
            oi = shot.get("order_index") or 0
            scene = scenes[oi] if oi < len(scenes) else {}
            num = (scene or {}).get("scene_number") or (oi + 1)
            tlp = tlp_all.get(f"scene_{num}") or {}
            sid_m = idx["match_scene_names"](
                (scene or {}).get("location"), (scene or {}).get("name"),
                (idx["details_map"].get(num) or {}).get("location"),
                shot.get("layer1_overview"))
            cids = idx["match_chars_of"](num, scene or {}, tlp)
            # ★★ 2026-09-14：除了"按剧本场次匹配"，再按**分镜文字里提到的角色名**补齐，
            #   并把**悬空 ID 剔除**。
            #   为什么：用户实测「关联了场景+角色，人却没出现」的真实原因是
            #   **角色卡被删除/重建** → 分镜上留着指向旧卡的 ID（本例是旧「AI 神」），
            #   我们静默丢掉它 → 提示词里只剩 1 个主体 → 模型自己编了一个发光的壮汉。
            #   这类"文字提到了、卡也在、就是没关联上"的情况，按名字是能修好的。
            try:
                from core import refcheck as _rc
                _chk = _rc.check_shot_refs(shot, idx["chars"], idx["scenes"])
                for _m in (_chk.get("mentioned_not_linked") or []):
                    if _m.get("id") and _m["id"] not in cids:
                        cids.append(_m["id"])
                _valid = {str(c.get("id")) for c in idx["chars"]}
                cids = [c for c in cids if c in _valid]      # 剔除悬空 ID
            except Exception as _e:
                logger.warning("按文字补关联失败（沿用剧本匹配）：%s", _e)
            patch = {}
            if sid_m and sid_m != shot.get("scene_id"):
                patch["scene_id"] = sid_m
            if cids and list(cids) != list(shot.get("character_ids") or []):
                patch["character_ids"] = cids
            if patch:
                try:
                    db.update_shot(shot["id"], **patch)
                    updated += 1
                except Exception as e:
                    logger.warning("relink 更新失败 %s: %s", shot.get("id"), e)
            detail.append({"order_index": oi, "scene": bool(sid_m), "characters": len(cids)})
        return success({
            "updated": updated, "total": len(shots),
            "assets": {"characters": idx["char_count"], "scenes": idx["scene_count"]},
            "detail": detail,
            "hint": "" if (idx["char_count"] or idx["scene_count"]) else
                    "该项目还没有角色/场景资产，请先到「角色」/「场景」页点「📝 从剧本提取」",
        })

    @app.post("/api/projects/{pid}/shots/from-script")
    async def shots_from_script(pid: str, replace: bool = True):
        """把剧本的场次批量转成分镜，并**建立分镜 ↔ 角色/场景 的真实引用**。

        这里是把「剧本 → 角色/场景 → 分镜 → 生成」串成一条链的关键一步：

        旧实现把**全部角色塞给每一个分镜**，而且**完全不写 scene_id** ——
        结果是：分镜不知道自己在哪个场景，每个分镜都挂着所有角色，
        于是生成时拿不到正确的参考图（每个镜头都用同一张脸/同一张场景图），
        角色页看到的"设定"和最终视频没有任何关系。

        现在：按场次的地点匹配场景、按场次出场人物匹配角色，
        角色/场景才真正成为分镜与视频生成的**输入**。
        """
        script = db.get_script(pid)
        if not script or not script.get("scenes"):
            raise error_response("该项目还没有剧本，请先在「剧本」页生成剧本", status=400)
        scenes = script["scenes"]
        if not isinstance(scenes, list) or not scenes:
            raise error_response("剧本里没有可用场次", status=400)

        tlp_all = script.get("three_layer_prompts") or {}
        char_list = db.list_characters(pid) or []
        scene_list = db.list_scenes(pid) or []
        proj = db.get_project(pid) or {}
        ps = proj.get("settings") or {}
        if isinstance(ps, str):
            try:
                ps = json.loads(ps)
            except Exception:
                ps = {}
        settings = db.get_all_settings()
        # 没有显式配置时，优先用**用户已经配了 Key 的**视频模型，
        # 避免「配了 seedance 却一直用默认 kling」这种"配了用不上"的情况
        default_provider = settings.get("default_model_provider")
        default_model = settings.get("default_model_name")
        if not default_provider:
            keys = settings.get("api_keys") or {}
            for k, v in keys.items():
                if v:
                    default_provider = k
                    break
        if not default_provider:
            default_provider = "kling"
        if not default_model:
            from core.adapters import get_adapter
            try:
                a = get_adapter(default_provider)
                default_model = DEFAULT_MODEL_IDS.get(default_provider, "")
            except Exception:
                default_model = ""

        # ── 建立匹配索引 ──
        def norm(s: str) -> str:
            s = str(s or "").strip()
            # 去掉「（伏击圈内）」这类括注，便于与场景名对上
            s = re.sub(r"[（(][^）)]*[）)]", "", s)
            return re.sub(r"\s+", "", s)

        scene_by_name = {}
        for sc in scene_list:
            scene_by_name.setdefault(norm(sc.get("name")), sc)
            scene_by_name.setdefault(str(sc.get("name") or "").strip(), sc)
        char_by_name = {}
        for c in char_list:
            char_by_name[str(c.get("name") or "").strip()] = c

        def match_scene(scene: Dict[str, Any]) -> Optional[str]:
            """把剧本场次的地点匹配到场景资产"""
            cands = [scene.get("location"), scene.get("name"), scene.get("title"),
                     scene.get("place")]
            for cand in cands:
                if not cand:
                    continue
                key = norm(cand)
                if key in scene_by_name:
                    return scene_by_name[key]["id"]
                # 包含匹配（"山林小道（伏击圈内）" → "山林小道"）
                for k, sc in scene_by_name.items():
                    if k and (k in key or key in k) and len(k) >= 2:
                        return sc["id"]
            return None

        def match_chars(scene: Dict[str, Any], tlp: Dict[str, Any]) -> List[str]:
            """把该场次的出场人物匹配到角色资产；剧本没写就用文本里出现的名字兜底"""
            names: List[str] = []
            raw = scene.get("characters")
            if raw:
                names = [str(x).strip() for x in (raw if isinstance(raw, list) else [raw])]
            if not names and isinstance(tlp, dict):
                blob = json.dumps(tlp, ensure_ascii=False)
                names = [n for n in char_by_name if n and n in blob]
            if not names:
                # 最后兜底：整段文本里出现的角色名
                blob = json.dumps(scene, ensure_ascii=False)
                names = [n for n in char_by_name if n and n in blob]
            ids = []
            for n in names:
                c = char_by_name.get(n)
                if c and c["id"] not in ids:
                    ids.append(c["id"])
            return ids

        existing = db.list_shots(pid) or []
        # ★ 分镜必须**严格按剧本细节**：细节里有逐秒动作/表情/镜头，
        #   比剧本场次本身细一层，直接决定生成质量。
        details_map = {}
        try:
            for d in (db.list_scene_details(pid) or []):
                if isinstance(d, dict) and d.get("scene_number"):
                    details_map[d["scene_number"]] = d
        except Exception:
            details_map = {}
        # ⚠ 真的把旧分镜删掉：此前注释写着"先清掉"，代码却没实现，
        #   导致每点一次「自动从剧本生成」就多出一整套重复分镜。
        removed = 0
        if replace and existing:
            for old_shot in existing:
                try:
                    if db.delete_shot(old_shot["id"]):
                        removed += 1
                except Exception as e:
                    logger.warning("删除旧分镜失败 %s: %s", old_shot.get("id"), e)
        created, unmatched_scene, no_char = [], [], []
        used_detail = 0
        for i, scene in enumerate(scenes):
            num = scene.get("scene_number") or (i + 1)
            tlp = tlp_all.get(f"scene_{num}") or {}
            detail = details_map.get(num) or {}
            if detail:
                used_detail += 1
            sid_matched = match_scene(scene)
            cids = match_chars(scene, tlp)
            # 细节里明确写了出场人物时，以**细节为准**（比场次字段更准）
            dchars = detail.get("characters")
            if isinstance(dchars, list) and dchars:
                names = []
                for c in dchars:
                    nm = (c.get("name") if isinstance(c, dict) else c)
                    if nm:
                        cobj = char_by_name.get(str(nm).strip())
                        if cobj and cobj["id"] not in names:
                            names.append(cobj["id"])
                if names:
                    cids = names
            if not sid_matched:
                unmatched_scene.append(str(scene.get("location") or scene.get("name") or f"第{num}场"))
            if not cids:
                no_char.append(num)
            # ★ 优先级：剧本细节 > 三层提示词 > 场次字段
            #   细节是"逐秒动作 + 表情 + 镜头"，比场次概述细一层，直接决定成片质感
            d_timeline = detail.get("timeline") if isinstance(detail.get("timeline"), list) else []
            if d_timeline:
                layer2 = [{
                    "start": t.get("start"), "end": t.get("end"),
                    "action": t.get("action") or t.get("description") or "",
                    "expression": t.get("expression") or "",
                    "camera": t.get("camera") or t.get("camera_move") or "",
                    "shot_size": t.get("shot_size") or t.get("景别") or "",
                    "dialogue": t.get("dialogue") or t.get("line") or "",
                } for t in d_timeline if isinstance(t, dict)]
            else:
                layer2 = tlp.get("layer2_timeline") or []
            layer1 = (detail.get("summary") or detail.get("atmosphere")
                      or tlp.get("layer1_overview") or scene.get("summary")
                      or scene.get("description") or "")
            layer3 = tlp.get("layer3_constraints") or {}
            if not layer3 and detail.get("constraints"):
                layer3 = detail.get("constraints") if isinstance(detail.get("constraints"), dict) else {}
            dur = detail.get("duration_seconds") or scene.get("duration_seconds") or 10
            # ★★ 新分镜的生成条件必须跟**本项目已有镜头**保持一致，而不是跟全局设置。
            #    否则用户换了全局默认模型之后再生成分镜，新旧镜头立刻变成"两个世界"
            #    —— 不同模型的画风/色彩科学不同，拼起来一眼就是两个片子，
            #    后期调色和转场**补不回来**（实测线上 9 个项目里 5 个中招）。
            #    只有在项目里还没有任何镜头时，才回落到全局设置。
            try:
                from core.consistency import default_shot_conditions
                _cond = default_shot_conditions(settings, db.list_shots(pid) or [])
            except Exception:
                _cond = {}
            _prov = _cond.get("model_provider") or default_provider
            _model = _cond.get("model_name") or default_model or ""
            _ar = _cond.get("aspect_ratio") or ps.get("default_aspect_ratio") or "16:9"
            _res = _cond.get("resolution") or "1080p"
            # ★ 2026-10-24（用户：「视频倍速好慢」）：这里原来写死 max(5, dur)，
            #   把 2.5s 的规划硬抬成 5s —— 切点密度直接减半。现在按项目节奏档位下限。
            try:
                from core import scriptplan as _spj
                _pacing_key = str((ps or {}).get("pacing") or "") or _spj.pacing_for(
                    style=(ps or {}).get("style"), intent="")
                _pmin = int(_spj.pacing_spec(_pacing_key).get("min") or 2)
            except Exception:  # noqa: BLE001
                _pmin = 2
            shot = db.create_shot(
                project_id=pid,
                order_index=i,
                scene_id=sid_matched,
                duration_seconds=max(_pmin, int(round(float(dur or 0)))),
                layer1_overview=layer1,
                layer2_timeline=layer2,
                layer3_constraints=layer3,
                character_ids=cids,
                model_provider=_prov,
                model_name=_model,
                aspect_ratio=_ar,
                resolution=_res,
            )
            created.append(shot)

        linked_scene = sum(1 for s in created if s.get("scene_id"))
        linked_char = sum(1 for s in created if s.get("character_ids"))
        return success({
            "created": len(created),
            "existing_before": len(existing),
            "removed": removed,
            "shots": created,
            "linkage": {
                "scene_linked": linked_scene,
                "char_linked": linked_char,
                "total": len(created),
                "used_scene_detail": used_detail,
                "unmatched_scenes": unmatched_scene[:8],
                "shots_without_character": no_char[:8],
            },
            "default_model": {"provider": default_provider, "model": default_model},
        })

    @app.post("/api/projects/{pid}/characters/from-script")
    async def characters_from_script(pid: str, refresh: bool = True, force: bool = False):
        """从剧本提取角色。

        旧实现只抄了个名字，description 写死"从剧本自动提取"，
        年龄/性别/服装/外貌全空 → 生成形象时等于只给模型一个名字，
        模型只能凭空编人（用户反馈"完全不按剧本设定生成"）。

        现在改为**把剧本正文交给 LLM 做结构化提取**，产出可直接用于
        出图的人物卡；`refresh=True` 时会**更新**已存在的同名角色，
        这样此前建好的空壳角色也能被修好。LLM 失败则回落朴素提取。
        """
        script = db.get_script(pid)
        if not script:
            raise error_response("该项目的剧本尚未生成，请先到「剧本」页生成", status=400)

        settings = db.get_all_settings()
        profiles: List[Dict[str, Any]] = []
        extract_error = ""
        art: Dict[str, Any] = {}
        try:
            from core.assets import extract_characters, detect_art_direction
            llm = _make_llm(settings)
            details = []
            try:
                details = db.list_scene_details(pid) or []
            except Exception:
                details = []
            # 先定全剧美术基调，再据此设计角色（保证同剧风格统一）
            # ★ 把「剧本细节」一并喂进去：细节里含外貌/服装/环境质感，
            #   角色卡因此更细腻（用户明确要求）
            art = await detect_art_direction(llm, script, scene_details=details) or {}
            profiles = await extract_characters(llm, script, art, scene_details=details)
            if art:
                try:
                    db.set_setting("art_direction", json.dumps(art, ensure_ascii=False))
                except Exception:
                    pass
        except Exception as e:
            extract_error = f"{type(e).__name__}: {e}"
            logger.warning("LLM 角色提取失败，回落朴素提取：%s", e)

        # 回落：从场次的 characters 字段抄名字
        if not profiles:
            names: List[str] = []
            for s in (script.get("scenes") or []):
                cs = s.get("characters") or []
                cs = cs if isinstance(cs, list) else [cs]
                for c in cs:
                    n = str(c).strip()
                    if n and n not in names:
                        names.append(n)
            profiles = [{"name": n} for n in names]
            if not extract_error:
                extract_error = "LLM 未返回可用角色，已回落为仅提取名字"

        existing = {c.get("name"): c for c in (db.list_characters(pid) or [])}
        created, updated, skipped = [], [], []
        # ── 覆盖策略 ──
        # 占位/空内容（"从剧本自动提取" 之类）一律用新设计覆盖；
        # 用户自己写过的内容不覆盖。
        PLACEHOLDERS = ("从剧本自动提取", "符合角色身份", "从剧本提取", "")
        force = bool(force)
        for p in profiles:
            name = (p.get("name") or "").strip()
            if not name:
                continue
            fields = {
                "description": p.get("description") or "",
                "costume_main": p.get("costume_main") or "",
                "age": str(p.get("age") or ""),
                "props": p.get("props") or [],
            }
            # ★ 固定槽位（脸型/肤色/眼睛/发型/体型/上装/下装/鞋/配饰/主色/识别锚点）
            #   必须落盘：出图和出视频的提示词都从这里读，保证跨镜头逐字一致。
            extra = {k: p.get(k) for k in ("personality", "relationships", "aliases", "role", "gender")
                     if p.get(k)}
            if p.get("slots"):
                extra["slots"] = p["slots"]
            old = existing.get(name)
            if old:
                patch = {}
                for k, v in fields.items():
                    if not v:
                        continue
                    cur = old.get(k)
                    if force or cur in PLACEHOLDERS or (isinstance(cur, list) and not cur):
                        patch[k] = v
                if extra:
                    # 已有槽位时合并而不是丢弃（用户手改过的字段优先保留）
                    prev = old.get("reference_features")
                    if isinstance(prev, str):
                        try:
                            prev = json.loads(prev)
                        except Exception:
                            prev = {}
                    if isinstance(prev, dict):
                        merged = dict(prev)
                        for sk, sv in (extra.get("slots") or {}).items():
                            merged.setdefault("slots", {})
                            if force or not merged["slots"].get(sk):
                                merged["slots"][sk] = sv
                        extra = {**prev, **extra, "slots": merged.get("slots", {})}
                    patch["reference_features"] = json.dumps(extra, ensure_ascii=False)
                if patch:
                    db.update_character(old["id"], **patch)
                    updated.append(name)
                else:
                    skipped.append(name)
            else:
                rec = dict(fields)
                rec["project_id"] = pid
                rec["name"] = name
                rec["status"] = "draft"
                if extra:
                    rec["reference_features"] = json.dumps(extra, ensure_ascii=False)
                created.append(db.create_character(**rec))

        return success({
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "count": len(created),
            "updated_count": len(updated),
            "total_in_script": len(profiles),
            "llm_used": bool(profiles and not extract_error),
            "warning": extract_error,
        })

    @app.post("/api/projects/{pid}/scenes/from-script")
    async def scenes_from_script(pid: str, force: bool = False):
        """从剧本提取场景（LLM 结构化 + 按美术基调设计环境细节）。

        旧实现只抄 `location` 字段并把 description 写死"从剧本自动提取"，
        灯光/天气/时间全是硬编码 → 生成场景图时同样等于没给信息。
        """
        script = db.get_script(pid)
        if not script:
            raise error_response("该项目的剧本尚未生成，请先到「剧本」页生成", status=400)

        settings = db.get_all_settings()
        profiles: List[Dict[str, Any]] = []
        warn = ""
        art: Dict[str, Any] = {}
        try:
            art_raw = settings.get("art_direction")
            if isinstance(art_raw, str):
                try:
                    art = json.loads(art_raw)
                except Exception:
                    art = {}
            elif isinstance(art_raw, dict):
                art = art_raw
            from core.assets import extract_scenes
            llm = _make_llm(settings)
            try:
                details = db.list_scene_details(pid) or []
            except Exception:
                details = []
            profiles = await extract_scenes(llm, script, art, scene_details=details)
        except Exception as e:
            warn = f"{type(e).__name__}: {e}"
            logger.warning("LLM 场景提取失败，回落朴素提取：%s", e)

        if not profiles:
            locs: List[str] = []
            for s in (script.get("scenes") or []):
                loc = str(s.get("location") or "").strip()
                if loc and loc not in locs:
                    locs.append(loc)
            profiles = [{"name": x} for x in locs]
            if not warn:
                warn = "LLM 未返回可用场景，已回落为仅提取地点名"

        valid_lt = {"natural", "golden_hour", "blue_hour", "night", "neon", "studio", "candle", "overcast"}
        valid_tod = {"day", "night", "dawn", "dusk", "noon"}
        PLACEHOLDERS = ("从剧本自动提取", "从剧本提取", "")
        existing = {s.get("name"): s for s in (db.list_scenes(pid) or [])}
        created, updated, skipped = [], [], []
        for p in profiles:
            name = (p.get("name") or "").strip()
            if not name:
                continue
            lt = p.get("lighting") if p.get("lighting") in valid_lt else "natural"
            tod = p.get("time_of_day") if p.get("time_of_day") in valid_tod else "day"
            fields = {
                "description": p.get("description") or "",
                "location_type": p.get("location_type") or "outdoor",
                "time_of_day": tod,
                "weather": p.get("weather") or "clear",
                "lighting": lt,
            }
            # ★ 固定槽位（地形/建筑/陈设/氛围/色调）存进 reference_features，
            #   同一地点在不同镜头里拿到的环境描述才会逐字相同。
            old = existing.get(name)
            if old:
                patch = {}
                for k, v in fields.items():
                    if not v:
                        continue
                    cur = old.get(k)
                    if force or cur in PLACEHOLDERS or cur in ("outdoor", "natural", "day", "clear"):
                        patch[k] = v
                if p.get("slots"):
                    prev = old.get("reference_features")
                    if isinstance(prev, str):
                        try:
                            prev = json.loads(prev)
                        except Exception:
                            prev = {}
                    merged = dict(prev) if isinstance(prev, dict) else {}
                    slots_prev = merged.get("slots") if isinstance(merged.get("slots"), dict) else {}
                    slots_new = dict(slots_prev)
                    for sk, sv in p["slots"].items():
                        if force or not slots_new.get(sk):
                            slots_new[sk] = sv
                    merged["slots"] = slots_new
                    patch["reference_features"] = merged
                if patch:
                    db.update_scene(old["id"], **patch)
                    updated.append(name)
                else:
                    skipped.append(name)
            else:
                rec = dict(fields)
                rec["project_id"] = pid
                rec["name"] = name
                rec["space_scale"] = p.get("space_scale") or "medium"
                if p.get("slots"):
                    rec["reference_features"] = {"slots": p["slots"]}
                created.append(db.create_scene(**rec))

        return success({"created": created, "updated": updated, "skipped": skipped,
                        "count": len(created), "updated_count": len(updated),
                        "total_in_script": len(profiles),
                        "llm_used": bool(profiles and not warn),
                        "art_direction": art, "warning": warn})

    @app.get("/api/characters/{cid}")
    async def get_character(cid: str):
        char = db.get_character(cid)
        if not char:
            raise error_response("Character not found", status=404)
        return success(char)

    @app.patch("/api/characters/{cid}")
    async def update_character(cid: str, req: CharacterUpdate):
        kwargs = req.model_dump(exclude_unset=True)
        char = db.update_character(cid, **kwargs)
        if not char:
            raise error_response("Character not found", status=404)
        return success(char)

    @app.delete("/api/characters/{cid}")
    async def delete_character(cid: str):
        if not db.delete_character(cid):
            raise error_response("Character not found", status=404)
        return success({"deleted": True})

    @app.get("/api/projects/{pid}/cast")
    async def get_project_cast(pid: str):
        """整片选角表：每个角色最终用哪把嗓子、来自哪（手选/自动）、引擎是否可用。

        ★ 2026-10-23 新增。在此之前用户**看不到**角色实际用的音色，
        也无法在 API 层指定 —— 反馈「音色太烂/人物感情不对」时无从下手。
        """
        from core.voicecast import voice_of_character, gender_of_character

        project = db.get_project(pid)
        if not project:
            raise error_response("项目不存在", status=404)
        settings = db.get_all_settings()
        chars = db.list_characters(pid) or []
        try:
            from core.voicecast import load_voice_catalog
            catalog = await load_voice_catalog(settings)
        except Exception:
            catalog = []
        known = {str(v.get("voice_id") or "") for v in catalog if v.get("voice_id")}
        rows = []
        for ch in chars:
            picked = voice_of_character(ch)          # 手选（characters.voice_id）优先
            provider = picked.split(":")[0] if ":" in picked else ""
            rows.append({
                "id": ch.get("id"), "name": ch.get("name"),
                "gender": gender_of_character(ch),
                "voice_id": picked,                  # 生效值（空 = 还没定，出片时自动选）
                "provider": provider,
                "source": "角色卡指定" if picked else "自动选角",
                "available": (picked in known) if picked else None,
            })
        return success({"project_id": pid, "characters": rows,
                        "catalog_size": len(catalog)})

    @app.post("/api/projects/{pid}/cast")
    async def set_project_cast(pid: str, payload: Dict[str, Any] = None):
        """给角色指定音色 / 性别。`assignments` 与 `genders` 的键都可以是**角色名或角色 id**。

        - `assignments`: {角色: voice_id}；值为空串 = 清除手选、回到自动选角。
          voice_id 形如 `agentai:speech-2.8:presenter_male` / `edge:zh-CN-YunxiNeural`。
        - `genders`: {角色: 性别}；接受 男/女/male/female，**空串 = 清除、回到自动推断**
          （人名用字 → 描述关键词 → 保守男声）。
          ★ 2026-10-23：`characters.gender` 列已加（core/db.py `_ensure_column`）；
            写进这一列后，`voicecast.gender_of_character()` 会**优先**读它 ——
            角色卡 gender 优先于文本猜。
        """
        payload = payload or {}
        assignments = payload.get("assignments") or {}
        genders = payload.get("genders") or {}
        if not isinstance(assignments, dict):
            raise error_response("assignments 必须是对象：{角色名或id: voice_id}", status=400)
        if not isinstance(genders, dict):
            raise error_response("genders 必须是对象：{角色名或id: 性别}", status=400)
        if not assignments and not genders:
            raise error_response("至少要给一项：assignments（音色）或 genders（性别）", status=400)
        project = db.get_project(pid)
        if not project:
            raise error_response("项目不存在", status=404)
        chars = db.list_characters(pid) or []
        by_name = {str(c.get("name") or "").strip(): c for c in chars}
        by_id = {str(c.get("id") or ""): c for c in chars}

        def _norm_gender(raw: Any) -> str:
            """性别归一：男/male/m → 'male'；女/female/f → 'female'；空 → ''。"""
            g = str(raw or "").strip().lower()
            if not g:
                return ""
            if g.startswith(("m", "男")):
                return "male"
            if g.startswith(("f", "女")):
                return "female"
            raise error_response(f"性别只接受 男/女/male/female（收到 {raw!r}）", status=400)

        updated = []
        for key, voice in assignments.items():
            ch = by_id.get(str(key)) or by_name.get(str(key).strip())
            if not ch:
                raise error_response(f"角色不存在：{key}", status=404)
            value = str(voice or "").strip()
            if value and ":" not in value:
                raise error_response(
                    f"voice_id 必须带 provider 前缀，例如 agentai:speech-2.8:presenter_male（收到 {value!r}）",
                    status=400)
            db.update_character(str(ch.get("id")), voice_id=value)
            updated.append({"id": ch.get("id"), "name": ch.get("name"), "voice_id": value})
        for key, raw in genders.items():
            ch = by_id.get(str(key)) or by_name.get(str(key).strip())
            if not ch:
                raise error_response(f"角色不存在：{key}", status=404)
            value = _norm_gender(raw)
            db.update_character(str(ch.get("id")), gender=value)
            # ★ 回读校验：本项目吃过"写了没生效却报成功"的亏，当场回读。
            back = db.get_character(str(ch.get("id"))) or {}
            if str(back.get("gender") or "") != value:
                raise error_response(
                    f"性别写入没生效：期望 {value!r}，库里是 {back.get('gender')!r}"
                    f"（characters.gender 列可能缺失，检查启动迁移）", status=500)
            updated.append({"id": ch.get("id"), "name": ch.get("name"), "gender": value})
        return success({"project_id": pid, "updated": updated})

    @app.post("/api/projects/{pid}/script/import")
    async def import_script(pid: str,
                            file: Optional[UploadFile] = File(None),
                            raw_text: str = Form(""),
                            structurize: bool = Form(True),
                            title: str = Form("")):
        """导入用户自己的剧本：文档（txt/md/json/docx/pdf/rtf）或直接粘贴的文字。

        - `structurize=True` 时会把文本交给 LLM 结构化成分场剧本（场次 + 三层提示词），
          这样下游的「角色/场景提取 → 分镜 → 视频生成」全都能直接用；
        - 失败或 `structurize=False` 时，至少把原文存成剧本，剧本页可见可编辑。
        """
        if not db.get_project(pid):
            raise error_response("项目不存在", status=404)

        text = (raw_text or "").strip()
        note = ""
        filename = ""
        if file is not None and getattr(file, "filename", ""):
            filename = file.filename
            settings0 = db.get_all_settings()
            cache = settings0.get("cache_dir") or os.path.join(
                os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
            updir = os.path.join(cache, "imports", pid)
            os.makedirs(updir, exist_ok=True)
            safe = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", os.path.basename(filename))[:120]
            saved = os.path.join(updir, f"{int(time.time())}_{safe}")
            with open(saved, "wb") as f:
                shutil.copyfileobj(file.file, f)
            try:
                from core.docimport import parse_document
                parsed, note = parse_document(saved)
            except ValueError as e:
                raise error_response(str(e), status=400)
            except Exception as e:
                raise error_response(f"解析失败：{type(e).__name__}: {e}", status=400)
            if parsed:
                text = (text + "\n\n" + parsed).strip() if text else parsed

        if not text or len(text) < 10:
            raise error_response(
                "没有拿到可用文本。" + (note or "请上传含文字的文档，或直接在文本框粘贴剧本内容。"),
                status=400)
        if len(text) > 60000:
            text = text[:60000]
            note = (note + " " if note else "") + "文本过长，已截取前 60000 字。"

        settings = db.get_all_settings()
        scenes_created = 0
        used_llm = False
        warn = ""
        if structurize:
            try:
                llm = _make_llm(settings)
                svc = ScriptService(llm)
                result = await svc.generate_script(
                    user_prompt=text, total_duration=60,
                    style="cinematic", auto_split=True,
                    settings=settings, intent=text)
                scenes = result.get("scenes") or []
                if scenes:
                    three_layer = {}
                    for scene in scenes[:24]:
                        try:
                            layer = await svc.generate_three_layer_prompt(scene)
                            three_layer[f"scene_{scene.get('scene_number')}"] = layer
                        except Exception as e:
                            logger.warning("三层提示词生成失败：%s", e)
                    db.upsert_script(
                        project_id=pid,
                        user_prompt=(title or filename or "导入的剧本"),
                        outline=result.get("outline") or text,
                        scenes=scenes,
                        three_layer_prompts=three_layer,
                    )
                    scenes_created = len(scenes)
                    used_llm = True
            except Exception as e:
                warn = f"{type(e).__name__}: {e}"
                logger.warning("LLM 结构化导入剧本失败，改为原文入库：%s", e)

        if not used_llm:
            # 兜底：把原文按"第N场/第N集/空行"粗切成分场，保证下游仍可推进
            chunks = re.split(r"\n(?=\s*(?:第\s*[0-9一二三四五六七八九十百]+\s*[场集幕]|场景\s*[0-9]+|INT\.|EXT\.))",
                              text)
            chunks = [c.strip() for c in chunks if c and len(c.strip()) > 5]
            if len(chunks) <= 1:
                paras = [p.strip() for p in text.split("\n\n") if p.strip()]
                chunks = paragraphs_to_scenes(paras)
            scenes = []
            for i, c in enumerate(chunks[:40], 1):
                scenes.append({
                    "scene_number": i,
                    "location": "",
                    "summary": c[:400],
                    "description": c[:1500],
                    "characters": [],
                    "duration_seconds": 10,
                })
            db.upsert_script(project_id=pid,
                           user_prompt=(title or filename or "导入的剧本"),
                           outline=text[:8000], scenes=scenes,
                           three_layer_prompts={})
            scenes_created = len(scenes)
            if not warn:
                warn = "已按文本切分为场次（未经 LLM 结构化），建议核对后使用"

        return success({
            "imported": True, "filename": filename, "chars": len(text),
            "scenes": scenes_created, "llm_structurized": used_llm,
            "preview": text[:300], "note": note, "warning": warn,
        })

    @app.post("/api/projects/{pid}/script/paste")
    async def paste_script(pid: str, payload: Dict[str, Any]):
        """直接粘贴剧本文字（等同导入 raw_text）"""
        text = (payload.get("text") or "").strip()
        if not text:
            raise error_response("请提供剧本文字", status=400)
        return await import_script(pid, file=None, raw_text=text,
                                   structurize=bool(payload.get("structurize", True)),
                                   title=str(payload.get("title") or ""))

    @app.post("/api/projects/{pid}/{kind}/{aid}/video-reference")
    async def upload_video_reference(pid: str, kind: str, aid: str,
                                     file: UploadFile = File(...)):
        """用户提供一个**视频**做参考 → 抽出有代表性的画面供挑选。

        为什么"视频参考"要落到抽帧：
          主流图像/视频模型能接受的是**图片**（首帧/身份/风格参考），
          几乎没有能直接吃视频的。所以正确落地是
          **视频 → 挑画面 → 当参考图 → 再让 AI 按剧情改**。
        """
        kind = (kind or "").lower()
        is_char = kind in ("characters", "character")
        item = db.get_character(aid) if is_char else db.get_scene(aid)
        if not item:
            raise error_response("资产不存在", status=404)
        if item.get("project_id") != pid:
            raise error_response("该资产不属于这个项目", status=400)

        ext = os.path.splitext(os.path.basename(file.filename or ""))[1].lower()
        from core.videoref import VIDEO_EXT, IMAGE_EXT
        if ext not in VIDEO_EXT:
            raise error_response(
                f"请上传视频文件（支持 {'/'.join(e.lstrip('.') for e in VIDEO_EXT)}）。"
                f"如果只有一张图，请用「🖼 参考图改造」。", status=400)

        settings = db.get_all_settings()
        cache = settings.get("cache_dir") or os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
        d = os.path.join(cache, "refs", pid, "video", aid)
        os.makedirs(d, exist_ok=True)
        video = os.path.join(d, f"{int(time.time())}{ext}")
        with open(video, "wb") as f:
            shutil.copyfileobj(file.file, f)
        if os.path.getsize(video) < 4096:
            raise error_response("上传的视频过小，可能不是有效文件", status=400)

        from core.videoref import extract_keyframes
        r = extract_keyframes(video, os.path.join(d, "frames"), max_frames=6)
        if not r.get("ok"):
            return success({"ok": False, "error": r.get("error"),
                            "video": video, "frames": []})
        # 记住这个视频，之后可以换时间点再取帧
        try:
            rf = item.get("reference_features")
            if isinstance(rf, str):
                try:
                    rf = json.loads(rf)
                except Exception:
                    rf = {}
            rf = dict(rf) if isinstance(rf, dict) else {}
            rf["video_reference"] = {"path": video,
                                     "filename": os.path.basename(file.filename or ""),
                                     "duration": r.get("duration")}
            if is_char:
                db.update_character(aid, reference_features=rf)
            else:
                db.update_scene(aid, reference_features=rf)
        except Exception as e:
            logger.warning("记录视频参考失败：%s", e)

        return success({
            "ok": True, "video": video, "duration": r.get("duration"),
            "scene_points": r.get("scene_points"),
            "frames": [{"path": f["path"], "time": f["time"], "source": f["source"],
                        "url": f"/api/local-image?path={quote(f['path'])}"}
                       for f in r["frames"]],
            "warnings": r.get("warnings") or [],
            "note": (f"从 {r.get('duration')}s 的视频里挑出 {len(r['frames'])} 张有代表性的画面。"
                     f"挑一张作为参考图（或直接让 AI 按剧情改），就能把它变成你的场景/角色设定。"),
        })

    @app.post("/api/projects/{pid}/{kind}/{aid}/use-frame")
    async def use_video_frame(pid: str, kind: str, aid: str,
                              payload: Dict[str, Any] = None):
        """把挑中的画面（或按时间点现取的一帧）设为该资产的形象基准。

        ★ 这一步修掉一个真 bug：以前上传视频会把 **.mp4 直接写进
          `reference_image_path`**，而那个字段下游是当**图片**用的
          （前端 `<img>` 显示、抽首帧、转 data URI）—— 塞视频进去必然坏。
          现在视频留在 `reference_features.video_reference`，
          `reference_image_path` **只放真的图片**。
        """
        payload = payload or {}
        kind = (kind or "").lower()
        is_char = kind in ("characters", "character")
        item = db.get_character(aid) if is_char else db.get_scene(aid)
        if not item:
            raise error_response("资产不存在", status=404)

        frame = str(payload.get("frame_path") or "").strip()
        if not frame and payload.get("time") is not None:
            # 用户自己指定时间点 → 现取一帧
            rf = item.get("reference_features")
            if isinstance(rf, str):
                try:
                    rf = json.loads(rf)
                except Exception:
                    rf = {}
            video = ((rf or {}).get("video_reference") or {}).get("path") or ""
            if not video or not os.path.exists(video):
                raise error_response("还没有上传参考视频，请先上传", status=400)
            from core.videoref import extract_single_frame
            d = os.path.join(os.path.dirname(video), "frames")
            os.makedirs(d, exist_ok=True)
            frame = extract_single_frame(
                video, float(payload["time"]),
                os.path.join(d, f"pick_{float(payload['time']):.2f}s.jpg"))
            if not frame:
                raise error_response("按该时间点取帧失败，换个时间试试", status=400)

        if not frame or not os.path.exists(frame):
            raise error_response("请先选一张画面（或给一个时间点）", status=400)

        # 只接受真正的图片
        from core.videoref import is_image
        if not is_image(frame):
            raise error_response(
                f"「{os.path.basename(frame)}」不是图片文件。"
                f"这个字段必须是图片（视频请走「视频参考」抽帧）。", status=400)

        if is_char:
            db.update_character(aid, reference_image_path=frame, status="reference_uploaded")
        else:
            db.update_scene(aid, reference_image_path=frame)
        return success({"ok": True, "path": frame,
                        "url": f"/api/local-image?path={quote(frame)}",
                        "note": "已设为该资产的形象基准。想再改可以直接用「🖼 在原图上修改」。"})

    @app.post("/api/scenes/{sid}/upload-reference")
    async def upload_scene_reference(sid: str, file: UploadFile = File(...)):
        """上传场景参考素材（**图片或视频**都支持）"""
        sc = db.get_scene(sid)
        if not sc:
            raise error_response("场景不存在", status=404)
        ext = os.path.splitext(os.path.basename(file.filename or ""))[1].lower()
        is_img = ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
        is_vid = ext in (".mp4", ".mov", ".webm", ".mkv", ".avi")
        if not (is_img or is_vid):
            raise error_response(f"不支持的素材格式：{ext}（支持图片 png/jpg/webp 或视频 mp4/mov/webm）", status=400)
        from core.videoref import probe_duration as _vref_probe
        settings = db.get_all_settings()
        cache = settings.get("cache_dir") or os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
        d = os.path.join(cache, "refs", sc["project_id"], "scenes", sid)
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, f"{int(time.time())}{ext}")
        with open(dst, "wb") as f:
            shutil.copyfileobj(file.file, f)
        if os.path.getsize(dst) < 512:
            raise error_response("上传的文件过小，可能不是有效素材", status=400)
        # ★ 视频不能写进 reference_image_path —— 那个字段下游当**图片**用
        #   （前端 <img> 显示、抽首帧、转 data URI），塞 mp4 进去必然坏。
        #   视频存进 reference_features.video_reference，并**自动抽一帧**
        #   作为形象基准，这样"传了视频立刻能用"，也不会把下游搞崩。
        if is_vid:
            first_frame = ""
            try:
                from core.videoref import extract_keyframes
                r = extract_keyframes(dst, os.path.join(d, "frames"), max_frames=4)
                if r.get("ok") and r["frames"]:
                    first_frame = r["frames"][0]["path"]
            except Exception as e:
                logger.warning("上传视频后抽帧失败：%s", e)
            rf = sc.get("reference_features")
            if isinstance(rf, str):
                try:
                    rf = json.loads(rf)
                except Exception:
                    rf = {}
            rf = dict(rf) if isinstance(rf, dict) else {}
            rf["video_reference"] = {"path": dst,
                                     "filename": os.path.basename(file.filename or ""),
                                     "duration": _vref_probe(dst)}
            patch = {"reference_features": rf}
            if first_frame:
                patch["reference_image_path"] = first_frame
            updated = db.update_scene(sid, **patch)
            return success({"scene": updated, "path": dst, "kind": "video",
                            "auto_frame": first_frame,
                            "url": f"/api/media?path={quote(dst)}",
                            "note": ("已上传视频，并自动抽了一帧作为形象基准。"
                                     "想挑更好的画面，用「🎞 视频参考」可以看到全部候选。"
                                     if first_frame else
                                     "已上传视频，但没能抽出可用画面，请换一个视频。")})
        updated = db.update_scene(sid, reference_image_path=dst)
        return success({"scene": updated, "path": dst, "kind": "image",
                        "url": f"/api/media?path={quote(dst)}"})

    # ────────────────────────────────────────────────────────────
    # ★★ 2026-10-20 补：把**外链图片**下载到本机并设为资产参考图。
    #
    #   为什么必须有：对话里点「📌 存为资产」时，前端拿到的常常是
    #   **厂商返回的外链 URL**。老做法是直接把这个 URL 写进
    #   `reference_image_path` —— 而后端把它当**文件路径**用：
    #     ① `os.path.exists(URL)` 永远 False ⇒ 生成时才炸；
    #     ② 厂商外链 24 小时过期 ⇒ 资产悄悄失效。
    #   前端其实想调 `attachImage`（注释里写了"attach-image 会把字节落盘"），
    #   但**这个方法在 api.js 里压根不存在**，后端也没有对应路由 ——
    #   于是每次都走"老接口"退路，存进去的是个坏引用。
    #
    #   现在补齐：服务端下载 → 校验真的是图片 → 落盘 → 写库。
    #   （走服务端下载而不是前端 fetch，是为了绕开 CDN 的 CORS 限制。）
    @app.post("/api/{kind}/{aid}/attach-image")
    async def api_attach_image(kind: str, aid: str, payload: Dict[str, Any] = None):
        payload = payload or {}
        url = str(payload.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise error_response("只支持 http(s) 外链图片", status=400)
        if kind not in ("characters", "scenes"):
            raise error_response("资产类型只能是 characters / scenes", status=400)
        table = "character" if kind == "characters" else "scene"
        obj = (db.get_character(aid) if table == "character" else db.get_scene(aid))
        if not obj:
            raise error_response("资产不存在", status=404)

        import time as _t_attach
        import httpx as _httpx_attach
        try:
            # ★ 必须带 User-Agent：不少 CDN（含 Wikimedia）会对**没有 UA 的
            #   请求直接回 403**（把脚本当爬虫挡掉）。带上就正常。
            with _httpx_attach.Client(
                    follow_redirects=True, timeout=60,
                    headers={"User-Agent": "VideoForge/2.1 (local desktop app)"}) as _cli:
                _resp = _cli.get(url)
                if _resp.status_code != 200:
                    raise error_response(
                        f"下载图片失败（HTTP {_resp.status_code}）", status=400)
                _blob = _resp.content
        except HTTPException:
            raise
        except Exception as e:
            raise error_response(f"下载图片失败：{type(e).__name__}: {e}", status=400)
        if not _blob or len(_blob) < 1024:
            raise error_response("下载到的内容太小，不像是图片", status=400)
        if len(_blob) > 25 * 1024 * 1024:
            raise error_response("图片太大（超过 25MB）", status=400)

        # 校验并统一转成 PNG（顺带把 EXIF 方向摆正）
        try:
            from PIL import Image as _PILImage
            import io as _io_attach
            _im = _PILImage.open(_io_attach.BytesIO(_blob))
            _im.load()
            if (_im.width < 16 or _im.height < 16):
                raise error_response("图片尺寸太小", status=400)
            _im = _im.convert("RGB")
        except HTTPException:
            raise
        except Exception as e:
            raise error_response(f"这不是一张能用的图片：{type(e).__name__}", status=400)

        _settings = db.get_all_settings() or {}
        _base = Path(_settings.get("output_dir", "./data/outputs"))
        _dir = _base / str(obj.get("project_id") or "") / ("characters" if table == "character" else "scenes") / aid
        _dir.mkdir(parents=True, exist_ok=True)
        _dst = _dir / f"ref_{int(_t_attach.time())}.png"
        try:
            _im.save(str(_dst), "PNG")
        except Exception as e:
            raise error_response(f"保存图片失败：{type(e).__name__}", status=500)
        if table == "character":
            db.update_character(aid, reference_image_path=str(_dst),
                                status="reference_uploaded")
        else:
            db.update_scene(aid, reference_image_path=str(_dst))
        return success({"path": str(_dst), "origin": "downloaded",
                        "url": f"/api/media?path={quote(str(_dst))}",
                        "width": int(_im.width), "height": int(_im.height)})

    @app.post("/api/characters/{cid}/upload-reference")
    async def upload_character_reference(cid: str, file: UploadFile = File(...)):
        char = db.get_character(cid)
        if not char:
            raise error_response("Character not found", status=404)
        settings = db.get_all_settings()
        ref_dir = Path(settings.get("output_dir", "./data/outputs")) / char["project_id"] / "characters" / cid
        ref_dir.mkdir(parents=True, exist_ok=True)
        file_path = ref_dir / file.filename

        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)

        db.update_character(cid, reference_image_path=str(file_path), status="reference_uploaded")
        return success({"path": str(file_path)})

    # ──────────── 场景 ────────────

    @app.post("/api/scenes")
    async def create_scene(req: SceneCreate):
        scene = db.create_scene(**req.model_dump())
        return success(scene)

    @app.get("/api/projects/{pid}/scenes")
    async def list_scenes(pid: str):
        return success({"scenes": db.list_scenes(pid)})

    @app.get("/api/scenes/{sid}")
    async def get_scene(sid: str):
        scene = db.get_scene(sid)
        if not scene:
            raise error_response("Scene not found", status=404)
        return success(scene)

    @app.patch("/api/scenes/{sid}")
    async def update_scene(sid: str, req: SceneUpdate):
        kwargs = req.model_dump(exclude_unset=True)
        scene = db.update_scene(sid, **kwargs)
        if not scene:
            raise error_response("Scene not found", status=404)
        return success(scene)

    @app.delete("/api/scenes/{sid}")
    async def delete_scene(sid: str):
        if not db.delete_scene(sid):
            raise error_response("Scene not found", status=404)
        return success({"deleted": True})

    # ──────────── 道具 ────────────

    @app.post("/api/props")
    async def create_prop(req: PropCreate):
        return success(db.create_prop(**req.model_dump()))

    @app.get("/api/projects/{pid}/props")
    async def list_props(pid: str):
        return success({"props": db.list_props(pid)})

    @app.patch("/api/props/{ppid}")
    async def update_prop(ppid: str, req: PropUpdate):
        kwargs = req.model_dump(exclude_unset=True)
        return success(db.update_prop(ppid, **kwargs))

    @app.delete("/api/props/{ppid}")
    async def delete_prop(ppid: str):
        if not db.delete_prop(ppid):
            raise error_response("Prop not found", status=404)
        return success({"deleted": True})

    # ──────────── 分镜（核心）────────────

    @app.post("/api/shots")
    async def create_shot(req: ShotCreate):
        kwargs = req.model_dump()
        return success(db.create_shot(**kwargs))

    @app.get("/api/projects/{pid}/shots")
    async def list_shots(pid: str):
        """分镜列表（附带 API Key 状态，前端可据此提示"该模型能不能用"）"""
        settings = db.get_all_settings()
        keys = settings.get("api_keys") or {}
        shots = db.list_shots(pid) or []
        for s in shots:
            prov = s.get("model_provider") or ""
            s["has_api_key"] = bool(_lookup_key_ci(keys, prov)) if prov else False
            # ★ 2026-10-22：同时给出**白膜视频**路径。库里的 previz_path 是首帧 PNG
            #   （界面拿它显示静帧），白膜视频是同目录的 镜NN.mp4。
            #   现算、不写库 —— 白膜重渲一次就自动跟上，不用改表结构。
            try:
                from core import previz_ref as _pr
                _v = _pr.white_model_video(s)
                if _v:
                    s["previz_video"] = _v
            except Exception:  # noqa: BLE001
                pass
        return success({"shots": shots,
                        "configured_providers": [k for k, v in keys.items() if v],
                        "default_mode": settings.get("default_render_mode") or "auto"})

    # ── 分镜提示词（三段式：video / negative / audio + 角色·场景锚定）──────
    # 设计稿来自 claude-opus-5 的协议（2026-10-22）；实现在 core/shot_prompts.py。
    def _shot_keyframe_path(pid: str, shot: Dict[str, Any]) -> str:
        """某一镜的写实关键帧路径（没有就空串）。"""
        try:
            from core import keyframe as _kf
            _root = str((db.get_all_settings() or {}).get("output_dir")
                        or os.path.join(os.getcwd(), "data", "outputs"))
            return _kf.existing_keyframe(_root, shot)
        except Exception:  # noqa: BLE001
            return ""

    @app.get("/api/projects/{pid}/prompts")
    async def list_shot_prompts(pid: str):
        """每镜三段提示词；没编译过的返回空，前端/工具可据此提示"先编译"。"""
        from core import shot_prompts as _sp
        shots = db.list_shots(pid) or []
        out = []
        for s in shots:
            prompts = _sp.stored_prompts(s)
            out.append({
                "shot_id": s.get("id"),
                "index": s.get("order_index"),
                "overview": s.get("layer1_overview") or "",
                "compiled": bool(prompts.get("video_prompt")),
                "video_prompt": prompts.get("video_prompt") or "",
                "negative_prompt": prompts.get("negative_prompt") or "",
                "audio_prompt": prompts.get("audio_prompt") or "",
                "character_anchor": prompts.get("character_anchor") or "",
                "scene_anchor": prompts.get("scene_anchor") or "",
                # ★ 跨镜接龙信息：这一镜是不是接着上一镜的尾帧出的
                "chain_mode": prompts.get("chain_mode") or "independent",
                "relay_first_frame": prompts.get("relay_first_frame") or "",
                "continuity_brief": prompts.get("continuity_brief") or "",
                # ★ 写实关键帧（= 用户确认的那张图，出片时当首帧）——
                #   有它就说明这一镜已经能"所见即所得"地出片。
                "keyframe_path": _shot_keyframe_path(pid, s),
            })
        return success({"shots": out, "count": len(out)})

    @app.post("/api/projects/{pid}/prompts/compile")
    async def compile_shot_prompts_route(pid: str):
        """重新编译整个项目的分镜提示词（改了剧本/人设/场景之后调用）。"""
        from core.shot_prompts import compile_project_prompts
        n = compile_project_prompts(db, pid)
        return success({"compiled": n})

    @app.put("/api/projects/{pid}/prompts/{sid}")
    async def update_shot_prompt(pid: str, sid: str, payload: dict = None):
        """改某一镜的提示词：只接受 video_prompt / negative_prompt / audio_prompt 三个键。

        ★ 允许用户直接改，是因为这几句直接决定出片效果；改完不用重编译，
          下一次生成就会用新的（`_compose_full_prompt` 优先读这里）。
        """
        import json as _json
        from core import shot_prompts as _sp
        shot = db.get_shot(sid)
        if not shot or str(shot.get("project_id")) != str(pid):
            return error_response("找不到这个镜头", 404)
        payload = payload or {}
        allowed = ("video_prompt", "negative_prompt", "audio_prompt")
        changed = {k: str(payload[k]).strip() for k in allowed if isinstance(payload.get(k), str) and str(payload[k]).strip()}
        if not changed:
            return error_response("没有可改的字段（只接受 video_prompt / negative_prompt / audio_prompt）", 400)
        layer3 = _sp.attach_prompts(shot, changed)
        db.update_shot(sid, layer3_constraints=_json.dumps(layer3, ensure_ascii=False))
        return success({"shot_id": sid, "updated": sorted(changed.keys()), "next": "下一次出片就会用这段提示词"})

    # ── Skill 库（只读）：给 vf_skill 工具与前端看 ─────────────────────
    @app.get("/api/skills")
    async def list_skills_route():
        """列出已装载的 skill（名字 + 一句话 + 来源）。"""
        from core.skills import get_registry, extra_dirs_from_settings
        try:
            _dd = os.environ.get("VIDEOFORGE_DATA_DIR") or os.path.join(os.getcwd(), "data")
            reg = get_registry(_dd, extra_dirs_from_settings(db.get_all_settings()))
            skills = getattr(reg, "skills", None) or getattr(reg, "_skills", {}) or {}
            items = []
            for name, sk in (skills.items() if hasattr(skills, "items") else []):
                items.append({
                    "name": getattr(sk, "name", name),
                    "title": getattr(sk, "title", "") or getattr(sk, "name", name),
                    "description": getattr(sk, "description", ""),
                    "source": getattr(sk, "source", ""),
                })
            return success({"skills": items, "count": len(items)})
        except Exception as e:  # noqa: BLE001
            return error_response(f"读 skill 库失败：{type(e).__name__}: {e}", 500)

    # ══════════════════════════════════════════════════════════════
    # 视频复刻（2026-10-24 新增）
    #   参考视频 → 镜头表/景别/运镜/色调/构图/字幕版式/台词 → remake_spec.json
    #   台词走硅基流动 ASR（core/asr_siliconflow.py），Key 复用 tts_api_keys.siliconflow。
    #   复刻的是**手法**（节奏/景别/运镜/色调/构图/台词密度），不是内容。
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/remake/status")
    async def remake_status():
        """复刻功能点自检：依赖、Key、风格档。"""
        try:
            from core import video_remake as VR
            from core import asr_siliconflow as ASR
            import shutil
            return success({
                "ok": True,
                "ffmpeg": shutil.which("ffmpeg") or "",
                "ffprobe": shutil.which("ffprobe") or "",
                "asr": ASR.key_status(db),
                "style_profiles": {k: {"label": v.get("label"),
                                       "aspect": v.get("aspect"),
                                       "shot_mean_gate": v.get("shot_mean_gate"),
                                       "shot_median_gate": v.get("shot_median_gate"),
                                       "shot_max_gate": v.get("shot_max_gate")}
                                   for k, v in VR.style_profiles().items()
                                   if not k.startswith("_")},
                "style_rules_source": VR.style_profiles().get("_source"),
                "compliance": VR.COMPLIANCE,
            })
        except Exception as e:  # noqa: BLE001
            return error_response(f"复刻自检失败：{type(e).__name__}: {e}", 500)

    @app.post("/api/remake/analyze")
    async def remake_analyze(payload: dict = Body(default_factory=dict)):
        """参考视频 → 复刻规格。dry_run=true（默认）只报成本与镜头数估算。"""
        try:
            from core import video_remake as VR
        except Exception as e:  # noqa: BLE001
            return error_response(f"复刻模块没加载上：{type(e).__name__}: {e}", 500)
        path = str((payload or {}).get("video_path") or "").strip()
        if not path or not os.path.isfile(path):
            return error_response("参考视频不存在：%s" % (path or "(空)"), 400)
        dry = (payload or {}).get("dry_run")
        dry_run = True if dry is None else bool(dry)
        pid = str((payload or {}).get("project_id") or "").strip()
        try:
            if dry_run:
                sd = VR.detect_shots(path)
                pr = VR.probe(path)
                from core import asr_siliconflow as ASR
                est = ASR.estimate(float(pr.get("duration") or 0))
                return success({"dry_run": True, "source": pr,
                                "estimated_shots": len(sd.get("shots") or []),
                                "est": est})
            spec = VR.analyze(
                path,
                out_dir=str((payload or {}).get("out_dir") or ""),
                project_id=pid, db=db,
                use_siliconflow=bool((payload or {}).get("use_siliconflow", True)),
                use_whisper=bool((payload or {}).get("use_whisper", True)),
                use_ocr=bool((payload or {}).get("use_ocr", True)),
                vision=bool((payload or {}).get("vision", False)),
                max_shots=int((payload or {}).get("max_shots") or 0))
            if not spec.get("ok"):
                return error_response(str(spec.get("error") or "复刻分析失败"), 500)
            return success({"dry_run": False, "spec_path": spec.get("spec_path"),
                            "shots": len(spec.get("shots") or []),
                            "style_profile": spec.get("style_profile"),
                            "pacing": spec.get("pacing"),
                            "dialogue_stats": spec.get("dialogue_stats"),
                            "asr": spec.get("asr"),
                            "warnings": spec.get("warnings"),
                            "compliance": spec.get("compliance")})
        except Exception as e:  # noqa: BLE001
            logger.exception("remake analyze failed")
            return error_response(f"复刻分析失败：{type(e).__name__}: {e}", 500)

    @app.post("/api/remake/transcribe")
    async def remake_transcribe(payload: dict = Body(default_factory=dict)):
        """视频/音频 → 台词（本地 whisper 出时间戳 + 硅基流动出整段校对文本）。"""
        try:
            from core import asr_siliconflow as ASR
        except Exception as e:  # noqa: BLE001
            return error_response(f"ASR 模块没加载上：{type(e).__name__}: {e}", 500)
        path = str((payload or {}).get("video_path") or "").strip()
        if not path or not os.path.isfile(path):
            return error_response("文件不存在：%s" % (path or "(空)"), 400)
        try:
            r = ASR.transcribe_timed(
                path,
                whisper_size=str((payload or {}).get("whisper_size") or "small"),
                use_siliconflow=bool((payload or {}).get("use_siliconflow", True)),
                project_id=str((payload or {}).get("project_id") or ""), db=db)
            return success({"ok": r.get("ok"),
                            "lines": [{"start": s.get("start"), "end": s.get("end"),
                                       "text": s.get("text")}
                                      for s in (r.get("segments") or [])],
                            "full_text": r.get("full_text"),
                            "siliconflow_text": r.get("siliconflow_text"),
                            "cost_yuan": r.get("cost_yuan"),
                            "elapsed": r.get("elapsed"),
                            "error": r.get("error"),
                            "note": r.get("note")})
        except Exception as e:  # noqa: BLE001
            logger.exception("remake transcribe failed")
            return error_response(f"台词提取失败：{type(e).__name__}: {e}", 500)

    # ══════════════════════════════════════════════════════════════
    # 电商商品视频（2026-10-24 新增）
    #   平台规格 / 品类节拍 / 合规词表 / AI 标识义务，数据全在
    #   skills_builtin/ecommerce-product-video/rules.json（单一真相源）。
    #   纯本地零付费，不调任何生成模型。
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/ecommerce/status")
    async def ecommerce_status():
        """电商功能自检：规则覆盖了哪些平台/品类/合规组。"""
        try:
            from core import ecommerce_video as EV
            st = EV.rules_status()
            return success({
                "ok": bool(st.get("loaded")),
                "platforms": st.get("platforms"),
                "categories": st.get("categories"),
                "compliance_lists": st.get("compliance_lists"),
                "skill_version": st.get("skill_version"),
                "placements": list((EV.rules().get("placements") or {}).keys()),
                "ai_labeling": EV.ai_labeling_checklist(),
                "batch_risk": EV.batch_upload_risk(),
            })
        except Exception as e:  # noqa: BLE001
            return error_response(f"电商自检失败：{type(e).__name__}: {e}", 500)

    @app.get("/api/ecommerce/platforms")
    async def ecommerce_platforms():
        """列出已收录的平台规格。"""
        try:
            from core import ecommerce_video as EV
            return success({"platforms": EV.list_platforms(),
                            "categories": EV.list_categories()})
        except Exception as e:  # noqa: BLE001
            return error_response(f"读平台规格失败：{type(e).__name__}: {e}", 500)

    @app.post("/api/ecommerce/plan")
    async def ecommerce_plan(payload: dict = Body(default_factory=dict)):
        """商品 → 视频方案（scenes 用 VideoForge 剧本 schema，可直接进流水线）。"""
        try:
            from core import ecommerce_video as EV
        except Exception as e:  # noqa: BLE001
            return error_response(f"电商模块没加载上：{type(e).__name__}: {e}", 500)
        p = payload or {}
        prod = str(p.get("product") or "").strip()
        if not prod:
            return error_response("必须给 product（商品名）", 400)
        sps = p.get("selling_points")
        if isinstance(sps, str):
            sps = [x.strip() for x in sps.replace("\n", "、").split("、") if x.strip()]
        try:
            plan = EV.plan_video(
                product=prod,
                platform=str(p.get("platform") or ""),
                category=str(p.get("category") or ""),
                selling_points=[str(x) for x in (sps or [])],
                audience=str(p.get("audience") or ""),
                duration_sec=float(p.get("duration_sec") or 0),
                price_line=str(p.get("price_line") or ""),
                brand=str(p.get("brand") or ""),
                ai_label=str(p.get("ai_label") or "plan"))
            if not plan.get("ok"):
                return error_response(str(plan.get("error") or "生成失败"), 500)
            return success(plan)
        except Exception as e:  # noqa: BLE001
            logger.exception("ecommerce plan failed")
            return error_response(f"生成商品视频方案失败：{type(e).__name__}: {e}", 500)

    @app.post("/api/ecommerce/audit")
    async def ecommerce_audit(payload: dict = Body(default_factory=dict)):
        """文案合规体检（BLOCK + WARN 双层）。"""
        try:
            from core import ecommerce_video as EV
        except Exception as e:  # noqa: BLE001
            return error_response(f"电商模块没加载上：{type(e).__name__}: {e}", 500)
        text = str((payload or {}).get("text") or "")
        if not text.strip():
            return error_response("必须给 text（要检查的文案）", 400)
        try:
            return success(EV.audit_copy(text))
        except Exception as e:  # noqa: BLE001
            logger.exception("ecommerce audit failed")
            return error_response(f"合规体检失败：{type(e).__name__}: {e}", 500)

    @app.post("/api/ecommerce/ai-label")
    async def ecommerce_ai_label(payload: dict = Body(default_factory=dict)):
        """AI 内容标识：apply=false（默认）只出清单+方案；apply=true 真烧进画面+元数据。"""
        try:
            from core import ecommerce_video as EV
        except Exception as e:  # noqa: BLE001
            return error_response(f"电商模块没加载上：{type(e).__name__}: {e}", 500)
        p = payload or {}
        vp = str(p.get("video_path") or "").strip()
        mode = str(p.get("mode") or "start_and_badge")
        if not p.get("apply"):
            return success({"checklist": EV.ai_labeling_checklist(),
                            "plan": EV.ai_label_plan(
                                video_path=vp, mode=mode,
                                start_seconds=float(p.get("start_seconds") or 3),
                                start_text=str(p.get("start_text") or EV.DEFAULT_START_TEXT),
                                badge_text=str(p.get("badge_text") or EV.DEFAULT_BADGE_TEXT)),
                            "batch_risk": EV.batch_upload_risk()})
        if not vp or not os.path.isfile(vp):
            return error_response("apply=true 时必须给 video_path（且文件存在）", 400)
        try:
            r = EV.apply_ai_label(vp, out_path=str(p.get("out_path") or ""), mode=mode,
                                  start_seconds=float(p.get("start_seconds") or 3),
                                  start_text=str(p.get("start_text") or EV.DEFAULT_START_TEXT),
                                  badge_text=str(p.get("badge_text") or EV.DEFAULT_BADGE_TEXT),
                                  font_path=str(p.get("font_path") or ""))
            if not r.get("ok"):
                return error_response(str(r.get("error") or "打标识失败"), 500)
            return success(r)
        except Exception as e:  # noqa: BLE001
            logger.exception("ecommerce ai-label failed")
            return error_response(f"打 AI 标识失败：{type(e).__name__}: {e}", 500)

    @app.post("/api/ecommerce/selling-points")
    async def ecommerce_selling_points(payload: dict = Body(default_factory=dict)):
        """卖点 → 可拍摄画面（形容词改写成动作/数字）。"""
        try:
            from core import ecommerce_video as EV
        except Exception as e:  # noqa: BLE001
            return error_response(f"电商模块没加载上：{type(e).__name__}: {e}", 500)
        sps = (payload or {}).get("selling_points")
        if isinstance(sps, str):
            sps = [x.strip() for x in sps.replace("\n", "、").split("、") if x.strip()]
        if not isinstance(sps, list) or not sps:
            return error_response("必须给 selling_points（数组或顿号分隔字符串）", 400)
        try:
            return success(EV.rewrite_selling_points([str(x) for x in sps]))
        except Exception as e:  # noqa: BLE001
            logger.exception("ecommerce selling-points failed")
            return error_response(f"卖点改写失败：{type(e).__name__}: {e}", 500)

    @app.get("/api/skills/{name}")
    async def get_skill_body(name: str):
        """读某个 skill 的完整正文（模型需要时才拉，避免吃 token）。"""
        from core.skills import get_registry, extra_dirs_from_settings
        try:
            _dd = os.environ.get("VIDEOFORGE_DATA_DIR") or os.path.join(os.getcwd(), "data")
            reg = get_registry(_dd, extra_dirs_from_settings(db.get_all_settings()))
            sk = reg.get(name)
            if sk is None:
                return error_response(f"没有这个 skill：{name}", 404)
            return success({
                "name": getattr(sk, "name", name),
                "description": getattr(sk, "description", ""),
                "body": getattr(sk, "body", "") or getattr(sk, "content", ""),
            })
        except Exception as e:  # noqa: BLE001
            return error_response(f"读 skill 失败：{type(e).__name__}: {e}", 500)

    @app.post("/api/projects/{pid}/prompts/check")
    async def check_prompts_route(pid: str):
        """检查分镜提示词一致性（C01~C08），返回问题清单。"""
        from core.shot_prompts import check_project_prompts
        return success(check_project_prompts(db, pid))

    @app.get("/api/shots/{sid}")
    async def get_shot(sid: str):
        shot = db.get_shot(sid)
        if not shot:
            raise error_response("Shot not found", status=404)
        return success(shot)

    @app.patch("/api/shots/{sid}")
    async def update_shot(sid: str, req: ShotUpdate):
        """中途修改分镜：换视频模型 / 改时长 / 改提示词 / 换角色等"""
        kwargs = req.model_dump(exclude_unset=True, exclude_none=True)
        if not kwargs:
            shot = db.get_shot(sid)
            if not shot:
                raise error_response("Shot not found", status=404)
            return success(shot)
        # 时长下限 5 秒（前端可传更小值，这里统一兜底）
        if "duration_seconds" in kwargs:
            try:
                kwargs["duration_seconds"] = max(5, int(kwargs["duration_seconds"]))
            except (TypeError, ValueError):
                kwargs.pop("duration_seconds", None)
        shot = db.update_shot(sid, **kwargs)
        if not shot:
            raise error_response("Shot not found", status=404)
        return success(shot)

    @app.delete("/api/shots/{sid}")
    async def delete_shot(sid: str):
        if not db.delete_shot(sid):
            raise error_response("Shot not found", status=404)
        return success({"deleted": True})

    @app.post("/api/projects/{pid}/shots/reorder")
    async def reorder_shots(pid: str, ordered_ids: List[str]):
        db.reorder_shots(pid, ordered_ids)
        return success({"reordered": True})

    @app.post("/api/shots/{sid}/generate")
    async def generate_shot(sid: str, req: GenerateVideoRequest):
        """兼容旧入口：统一转到 /render 的逻辑（真实模型优先 + 失败回退本地合成）。

        之前这里是"丢进任务队列就不管了"，用户只能看到一个 task_id，
        失败了界面也拿不到原因。现在直接把请求转发给 render_shot，
        让「生成」按钮的行为与「生成」页完全一致。
        """
        shot = db.get_shot(sid)
        if not shot:
            raise error_response("Shot not found", status=404)
        body = req.model_dump(exclude_none=True)
        body.pop("shot_id", None)
        # 旧调用（只传 num_candidates）默认走 auto：配了 Key 用真实模型，否则本地合成
        body.setdefault("mode", "auto")
        return await render_shot(sid, body)

    @app.post("/api/shots/{sid}/select-candidate")
    async def select_candidate(sid: str, candidate_id: str):
        shot = db.get_shot(sid)
        if not shot:
            raise error_response("Shot not found", status=404)

        candidates = shot.get("candidates", [])
        new_candidates = []
        for c in candidates:
            c["is_selected"] = (c.get("candidate_id") == candidate_id)
            new_candidates.append(c)

        db.update_shot(sid, candidates=new_candidates, selected_candidate_id=candidate_id)
        return success({"selected": candidate_id})

    # ──────────── 任务 ────────────

    @app.get("/api/tasks/{tid}")
    async def get_task(tid: str):
        task = db.get_task(tid)
        if not task:
            raise error_response("Task not found", status=404)
        return success(task)

    @app.get("/api/tasks")
    async def list_tasks(status: Optional[str] = None, limit: int = Query(50)):
        return success({"tasks": db.list_tasks(status, limit)})

    @app.get("/api/tasks/{tid}/stream")
    async def stream_task(tid: str):
        """SSE 流式进度推送"""
        async def event_generator():
            last_progress = -1
            last_status = None
            while True:
                task = db.get_task(tid)
                if not task:
                    yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                    break

                progress = task.get("progress", 0)
                status = task.get("status", "")
                if progress != last_progress or status != last_status:
                    yield f"data: {json.dumps({'progress': progress, 'status': status})}\n\n"
                    last_progress = progress
                    last_status = status

                if status in ("completed", "failed"):
                    break
                await asyncio.sleep(0.5)
        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # ──────────── 版权抽象化 ────────────

    @app.post("/api/projects/{pid}/abstract")
    async def abstract_copyright(pid: str, req: AbstractionRequest):
        settings = db.get_all_settings()
        llm = _make_llm(settings)
        svc = AbstractionService(llm)
        try:
            result = await svc.abstract(req.source_description)
        except Exception as e:
            raise error_response(str(e), status=500)

        # 自动创建资产
        for char in result.get("characters", []):
            db.create_character(
                project_id=pid,
                name=char.get("abstracted_name", "未命名角色"),
                description=char.get("description", ""),
                costume_main=char.get("costume", ""),
                reference_features={"preserved": char.get("preserved_traits", [])},
            )

        for scene in result.get("scenes", []):
            db.create_scene(
                project_id=pid,
                name=scene.get("abstracted_name", "未命名场景"),
                description=scene.get("description", ""),
            )

        for prop in result.get("props", []):
            db.create_prop(
                project_id=pid,
                name=prop.get("abstracted_name", "未命名道具"),
                description=prop.get("description", ""),
            )

        db.log_abstraction(pid, req.source_description, result,
                            result.get("removed_features", []),
                            result.get("preserved_features", []))
        return success(result)

    # ──────────── 设置 ────────────

    @app.get("/api/settings")
    async def get_settings():
        return success(db.get_all_settings())

    @app.put("/api/settings")
    async def update_settings(req: SettingsUpdate):
        current = db.get_all_settings()
        kwargs = req.model_dump(exclude_unset=True)
        for k, v in kwargs.items():
            if v is None:
                continue
            if k in ("api_keys", "llm_api_keys", "tts_api_keys", "tts_models"):
                # Each provider is edited independently. Blank secret fields leave stored keys intact.
                merged = dict(current.get(k) or {})
                merged.update({provider: value for provider, value in v.items()
                               if isinstance(value, str) and value.strip()})
                v = merged
            db.set_setting(k, v)
        return success(db.get_all_settings())

    @app.get("/api/tts/models")
    async def list_tts_models():
        """**配音模型**清单（用户要求："真正的去选择配音模型"）。

        每家一把：能不能选型号（`supports_model`）、有哪些真实型号
        （`models`：[{id,label,note}]）、当前选中的是哪个（`selected_model`）、
        以及**为什么不能选**（`model_note`，例如 Edge 免费引擎没有模型参数、
        硅基流动的模型跟着音色走）。

        ★ 型号清单是各适配器**自己声明**的（`core/voice/*_tts.py`），
          这里只是把真值透出来 —— 不在接口层再抄一份，免得两边不一致。
        ★ 顺便说清一个常见误解：MiniMax 的 **M3 是语言模型**，不是配音模型；
          M3 在「LLM 模型」列表里（`core/model_catalog.LLM_CATALOG`），
          配音型号是 `speech-*` 系列。
        """
        from core.voice import dispatcher as _vd
        settings = db.get_all_settings()
        provs = await _vd.list_providers(settings)
        # silent 是"不配音"的哨兵，不是引擎
        provs = [p for p in provs if p.get("name") != "silent"]
        return success({
            "providers": provs,
            "selected": {p["name"]: p.get("selected_model") or "" for p in provs},
            "note": ("MiniMax 的 M3 是**语言模型**（写剧本/分镜用），不是配音模型；"
                     "配音型号是 speech-* 系列（如 speech-2.8-hd）。"
                     "M3 已加到「LLM 模型」列表里。"),
        })

    # ──────────── 后期处理 ────────────

    # ──────────── 输出文件管理（后期页）────────────

    def _resolve_output(pid: str, source_path: str = "", source_filename: str = "") -> str:
        """把前端给的「文件名或路径」解析成真实存在的成片文件。

        兼容三种历史布局：<out>/<pid>/x.mp4、<out>/<pid>/final/x.mp4、<out>/<pid>/shots/x.mp4
        """
        roots = [os.path.join(_outputs_root(), pid),
                 os.path.join(_outputs_root(), pid, "final"),
                 os.path.join(_outputs_root(), pid, "shots")]
        if source_path:
            if not os.path.isabs(source_path):
                source_path = os.path.join(_outputs_root(), pid, source_path)
            cand = os.path.abspath(source_path)
            # 只允许输出目录内的文件，避免被当作任意文件读取器
            allowed = [os.path.abspath(_outputs_root())]
            if any(cand.startswith(a + os.sep) for a in allowed) and os.path.exists(cand):
                return cand
        if source_filename:
            for r in roots:
                p = os.path.join(r, source_filename)
                if os.path.exists(p):
                    return p
        raise error_response("找不到指定的输出文件（可能已被移动或删除）", status=404)

    @app.get("/api/projects/{pid}/outputs")
    async def list_outputs(pid: str):
        """列出该项目的所有产出文件（成片 / 分镜片段 / 导出比例 / 字幕），供后期页展示。

        流畅度注意：时长探测要 spawn 一次 ffmpeg，逐个串行会让列表加载明显卡顿，
        因此先收集文件、再**并发探测**（信号量限制并发数，避免一次开太多进程）。
        """
        from core.compose import probe_duration as _pd
        root = os.path.join(_outputs_root(), pid)
        # Preserve paid-but-rejected results for inspection.  They remain
        # playable/downloadable, but carry an explicit truth label and are
        # never presented as an accepted final output.
        candidate_meta: Dict[str, Dict[str, Any]] = {}
        for _shot in (db.list_shots(pid) or []):
            _candidates = _shot.get("candidates") or []
            if isinstance(_candidates, str):
                try:
                    _candidates = json.loads(_candidates or "[]")
                except Exception:
                    _candidates = []
            for _candidate in (_candidates if isinstance(_candidates, list) else []):
                if not isinstance(_candidate, dict) or not _candidate.get("path"):
                    continue
                _candidate_path = os.path.normcase(os.path.abspath(str(_candidate["path"])))
                _delivery = _candidate.get("delivery") if isinstance(_candidate.get("delivery"), dict) else {}
                _status = str(_candidate.get("validation_status") or _delivery.get("status") or "").lower()
                _rejected = (_status in ("rejected", "failed", "unverified")
                             or (_delivery and _delivery.get("ok") is False))
                candidate_meta[_candidate_path] = {
                    "validation_status": "rejected" if _rejected else (_status or "not_requested"),
                    "validation_note": (_candidate.get("note") or
                        ("已生成，可播放和下载；与给定白膜的一致性未通过，未作为正式成片。"
                         if _rejected else "")),
                    "accepted": not _rejected,
                }
        items = []
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith((".mp4", ".srt", ".mp3", ".m4a", ".wav")):
                    continue
                fp = os.path.join(dirpath, fn)
                rel = os.path.relpath(fp, root).replace("\\", "/")
                try:
                    stt = os.stat(fp)
                except OSError:
                    continue
                items.append({
                    "name": fn, "rel": rel, "path": fp,
                    "size_bytes": stt.st_size, "mtime": stt.st_mtime,
                    "url": f"/api/media?path={quote(fp)}",
                    "download_url": f"/api/media?path={quote(fp)}&download=1",
                    "kind": ("video" if fn.lower().endswith(".mp4")
                             else "subtitle" if fn.lower().endswith(".srt") else "audio"),
                    "is_final": rel == "final.mp4",
                    "duration": 0,
                    **candidate_meta.get(os.path.normcase(os.path.abspath(fp)), {}),
                })
        items.sort(key=lambda x: (not x["is_final"], -x["mtime"]))

        sem = asyncio.Semaphore(4)

        async def _fill(it):
            if it["kind"] != "video" or it["size_bytes"] <= 10240:
                return
            async with sem:
                try:
                    it["duration"] = await asyncio.wait_for(_pd(it["path"]), timeout=25)
                except Exception:
                    it["duration"] = 0

        if items:
            await asyncio.gather(*[_fill(it) for it in items])
        return success({"outputs": items, "output_dir": os.path.join(_outputs_root(), pid),
                        "root": _outputs_root()})

    @app.post("/api/projects/{pid}/outputs/add-voice")
    async def outputs_add_voice(pid: str, payload: Dict[str, Any]):
        """给指定成片重新配音（先 TTS，再替换原音轨），产出 *_voice.mp4"""
        src = _resolve_output(pid, payload.get("source_path", ""), payload.get("source_filename", ""))
        text = (payload.get("text") or "").strip()
        voice_id = payload.get("voice_id") or "edge:zh-CN-XiaoxiaoNeural"
        rate = float(payload.get("rate") or 1.0)
        if not text:
            raise error_response("请提供配音文本", status=400)

        settings = db.get_all_settings()
        cache = settings.get("cache_dir") or os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
        work = os.path.join(cache, "voice", pid)
        os.makedirs(work, exist_ok=True)
        audio = os.path.join(work, f"redo_{int(time.time())}.mp3")
        try:
            from core.voice.base import TTSRequest
            r = await voice_synthesize(TTSRequest(text=text, voice_id=voice_id,
                                                 rate=rate, output_path=audio), settings)
        except Exception as e:
            raise error_response(f"配音异常：{e}", status=500)
        if not r.success or not os.path.exists(audio):
            raise error_response(f"配音失败：{r.error}", status=500)

        dst = os.path.join(os.path.dirname(src),
                           f"{os.path.splitext(os.path.basename(src))[0]}_voice.mp4")
        ff = get_ffmpeg_for_postprocess()
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
               "-i", src, "-i", audio,
               "-map", "0:v", "-map", "1:a",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-shortest", "-movflags", "+faststart", dst]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(dst):
            raise error_response(f"配音混流失败：{(err or b'').decode('utf-8', 'ignore')[-300:]}", status=500)
        return success({"output_path": dst, "url": f"/api/media?path={quote(dst)}",
                        "duration": r.duration_seconds})

    @app.post("/api/projects/{pid}/outputs/export-aspect")
    async def outputs_export_aspect(pid: str, payload: Dict[str, Any]):
        """把指定成片导出成另一个比例（16:9 / 9:16 / 1:1）"""
        src = _resolve_output(pid, payload.get("source_path", ""), payload.get("source_filename", ""))
        aspect = (payload.get("target_aspect") or "9:16").strip()
        if aspect not in ("16:9", "9:16", "1:1"):
            raise error_response("target_aspect 只支持 16:9 / 9:16 / 1:1", status=400)
        dst = os.path.join(os.path.dirname(src),
                           f"{os.path.splitext(os.path.basename(src))[0]}_{aspect.replace(':', 'x')}.mp4")
        try:
            from core.postprocess import export_aspect_ratio
            out = await export_aspect_ratio(src, aspect, dst, get_ffmpeg_for_postprocess())
        except Exception as e:
            raise error_response(f"比例导出失败：{e}", status=500)
        return success({"output_path": out, "url": f"/api/media?path={quote(out)}"})

    @app.post("/api/projects/{pid}/outputs/mix-bgm")
    async def outputs_mix_bgm(pid: str, source_path: str = Form(""),
                              source_filename: str = Form(""),
                              bgm_volume: float = Form(0.25),
                              fade_in: float = Form(1.5),
                              fade_out: float = Form(2.0),
                              file: UploadFile = File(...)):
        """给成片混入 BGM（支持淡入淡出）—— 上传的音频直接落盘后混流"""
        src = _resolve_output(pid, source_path, source_filename)
        settings = db.get_all_settings()
        cache = settings.get("cache_dir") or os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
        bdir = os.path.join(cache, "bgm")
        os.makedirs(bdir, exist_ok=True)
        bgm = os.path.join(bdir, f"{pid[:8]}-{int(time.time())}-{file.filename or 'bgm.mp3'}")
        with open(bgm, "wb") as f:
            shutil.copyfileobj(file.file, f)

        dst = os.path.join(os.path.dirname(src),
                           f"{os.path.splitext(os.path.basename(src))[0]}_bgm.mp4")
        ff = get_ffmpeg_for_postprocess()
        from core.compose import probe_duration as _pd
        dur = await _pd(src) or 0
        fo_start = max(0.0, dur - float(fade_out)) if dur > 0 else 0.0
        chain = (f"[1:a]volume={float(bgm_volume):.3f},"
                 f"afade=t=in:st=0:d={float(fade_in):.2f}"
                 + (f",afade=t=out:st={fo_start:.2f}:d={float(fade_out):.2f}" if dur > 0 else "")
                 # ★ normalize=0：amix 默认 normalize=1 会把每路乘 1/N，
                 #   实测让人声掉 5.6dB（"一加 BGM 台词就听不清"的根因）
                 + "[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0:"
                   "normalize=0[a]")
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-i", bgm,
               "-filter_complex", chain, "-map", "0:v", "-map", "[a]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", dst]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(dst):
            raise error_response(f"BGM 混流失败：{(err or b'').decode('utf-8', 'ignore')[-300:]}", status=500)
        return success({"output_path": dst, "url": f"/api/media?path={quote(dst)}"})

    @app.post("/api/projects/{pid}/outputs/delete")
    async def outputs_delete(pid: str, payload: Dict[str, Any]):
        """删除某个产出文件（仅限输出目录内）"""
        p = _resolve_output(pid, payload.get("source_path", ""), payload.get("source_filename", ""))
        try:
            os.remove(p)
        except OSError as e:
            raise error_response(f"删除失败：{e}", status=500)
        return success({"deleted": p})

    @app.post("/api/projects/{pid}/stitch")
    async def stitch_project(pid: str, shot_ids: Optional[List[str]] = None,
                              output_filename: str = "final.mp4",
                              add_subtitles: bool = False):
        """拼接项目的所有 shot 视频"""
        shots = db.list_shots(pid)
        if shot_ids:
            shots = [s for s in shots if s["id"] in shot_ids]

        if not shots:
            raise error_response("No shots to stitch", status=400)

        # 收集所有已选的视频路径
        video_paths = []
        srt_segments = []
        current_time = 0.0

        for shot in shots:
            candidates = shot.get("candidates", [])
            selected_id = shot.get("selected_candidate_id")
            selected = next((c for c in candidates if c.get("candidate_id") == selected_id), None)
            if not selected and candidates:
                selected = candidates[0]
            if not selected:
                continue

            # ⚠ 本地渲染存的是 "path"，适配器存的是 "video_path"，两种都要兼容
            vp = selected.get("video_path") or selected.get("path")
            if not vp or not os.path.exists(vp):
                continue
            video_paths.append(vp)

            # 收集字幕
            timeline = shot.get("layer2_timeline", [])
            duration = shot.get("duration_seconds", 10)
            for item in timeline:
                srt_segments.append({
                    "start": current_time + item.get("start", 0),
                    "end": current_time + item.get("end", duration),
                    "action": item.get("action", ""),
                    "expression": item.get("expression", ""),
                })
            current_time += duration

        if not video_paths:
            raise error_response("No completed shots to stitch", status=400)

        settings = db.get_all_settings()
        output_dir = Path(settings.get("output_dir", "./data/outputs")) / pid
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / output_filename
        ffmpeg_path = get_ffmpeg_for_postprocess()

        # 拼接
        final_path = await stitch_videos(video_paths, str(output_path), ffmpeg_path)

        # 字幕（可选）
        if add_subtitles and srt_segments:
            srt_path = output_dir / (Path(output_filename).stem + ".srt")
            generate_srt_from_timeline(srt_segments, str(srt_path))
            with_subs_path = output_dir / (Path(output_filename).stem + ".sub.mp4")
            await burn_subtitles(final_path, str(srt_path), str(with_subs_path), ffmpeg_path)
            final_path = str(with_subs_path)

        return success({"output_path": final_path})

    @app.post("/api/projects/{pid}/export-aspect")
    async def export_aspect(pid: str, source_filename: str,
                              target_aspect: str = "9:16"):
        """导出指定比例"""
        settings = db.get_all_settings()
        ffmpeg_path = settings.get("ffmpeg_path", "ffmpeg")
        output_dir = Path(settings.get("output_dir", "./data/outputs")) / pid / "final"
        source = output_dir / source_filename
        if not source.exists():
            raise error_response(f"Source not found: {source}", status=404)
        target = output_dir / f"{source.stem}_{target_aspect.replace(':', 'x')}.mp4"
        result_path = await export_aspect_ratio(str(source), target_aspect, str(target), ffmpeg_path)
        return success({"output_path": result_path})

    @app.post("/api/projects/{pid}/add-bgm")
    async def add_bgm(pid: str, source_filename: str,
                        bgm_path: str = Form(...),
                        bgm_volume: float = Form(0.3)):
        settings = db.get_all_settings()
        ffmpeg_path = settings.get("ffmpeg_path", "ffmpeg")
        output_dir = Path(settings.get("output_dir", "./data/outputs")) / pid / "final"
        source = output_dir / source_filename
        target = output_dir / f"{source.stem}_bgm.mp4"
        result_path = await mix_bgm(str(source), bgm_path, str(target), ffmpeg_path, bgm_volume)
        return success({"output_path": result_path})

    @app.post("/api/projects/{pid}/add-voice")
    async def add_voice(pid: str, source_filename: str = Form(...),
                          voice_id: str = Form(...),
                          text: Optional[str] = Form(None),
                          voice_rate: float = Form(1.0)):
        """为视频配音：先合成语音，再混入视频音轨（替换原音轨，避免 BGM 叠加问题）

        如果 text 为空则只混音（用于：用户已经单独合成过 audio_path）。
        """
        settings = db.get_all_settings()
        ffmpeg_path = settings.get("ffmpeg_path", "ffmpeg")
        output_dir = Path(settings.get("output_dir", "./data/outputs")) / pid / "final"
        output_dir.mkdir(parents=True, exist_ok=True)
        source = output_dir / source_filename
        if not source.exists():
            raise error_response(f"Source not found: {source}", status=404)
        target = output_dir / f"{source.stem}_voice.mp4"

        # 1. 合成语音
        if not text:
            # 仅混音场景：要求用户先单独调 /api/voice/synthesize
            raise error_response("text 不能为空（配音需要文本）。如需只混音，请先调 /api/voice/synthesize 生成 audio_path。", status=400)

        audio_path = output_dir / f"{source.stem}_voice.mp3"
        req = TTSRequest(
            text=text,
            voice_id=voice_id,
            rate=voice_rate,
            output_path=str(audio_path),
            language=settings.get("voice_language", "zh-CN"),
        )
        result: TTSResult = await voice_synthesize(req, settings)
        if not result.success:
            raise error_response(f"配音失败：{result.error}", status=500)

        # 2. 混入视频（替换原音轨，避免叠加到 BGM 之上）
        voice_duration = result.duration_seconds or 0
        # 若语音时长已知，让视频循环 / 截断到语音时长（更接近 MPT 的做法）
        cmd = [
            ffmpeg_path, "-y",
            "-i", str(source),
            "-i", str(audio_path),
            "-map", "0:v",
            "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",                  # 截断到最短流
            str(target),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise error_response(
                f"配音混流失败：{(stderr.decode() or '')[:300]}",
                status=500,
            )
        return success({
            "output_path": str(target),
            "audio_path": str(audio_path),
            "voice_duration_seconds": voice_duration,
            "subtitles": result.subtitles,  # Edge TTS 提供；其他 provider 后续用 whisper 对齐
        })

    @app.post("/api/projects/{pid}/generate-subtitles")
    async def gen_subs(pid: str, output_filename: str = "subtitles.srt"):
        """生成分镜字幕 SRT"""
        shots = db.list_shots(pid)
        srt_segments = []
        current_time = 0.0
        for shot in shots:
            timeline = shot.get("layer2_timeline", [])
            duration = shot.get("duration_seconds", 10)
            for item in timeline:
                srt_segments.append({
                    "start": current_time + item.get("start", 0),
                    "end": current_time + item.get("end", duration),
                    "action": item.get("action", ""),
                    "expression": item.get("expression", ""),
                })
            current_time += duration

        settings = db.get_all_settings()
        output_dir = Path(settings.get("output_dir", "./data/outputs")) / pid / "final"
        srt_path = output_dir / output_filename
        generate_srt_from_timeline(srt_segments, str(srt_path))
        return success({"srt_path": str(srt_path), "segments": len(srt_segments)})

    # ──────────── 🎬 一键成片（compose）────────────
    #
    # 这是"不依赖任何付费视频模型 Key 也能出片"的主路径：
    # 分镜文本 → Edge TTS 配音 + 句级时间轴 → 场景图/角色图/自动文字卡
    #          → FFmpeg 运镜 + 拼接 + 烧字幕 + BGM → final.mp4
    _compose_state: Dict[str, Any] = {}

    def _outputs_root() -> str:
        s = db.get_all_settings()
        return str(s.get("output_dir") or os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "outputs"))

    @app.post("/api/projects/{pid}/reveal-output")
    async def reveal_output(pid: str):
        """在文件资源管理器中**打开并置顶**该项目的输出目录。

        用户反馈"点了没反馈"——后端其实打开了，但资源管理器窗口开在应用后面，
        用户什么都看不到。这里改成：打开后**主动把窗口带到前台**。
        """
        out_dir = os.path.join(_outputs_root(), pid)
        os.makedirs(out_dir, exist_ok=True)
        final_path = os.path.join(out_dir, "final.mp4")
        target = final_path if os.path.exists(final_path) else out_dir
        try:
            if sys.platform == "win32":
                args = ["explorer", "/select,", target] if target.endswith(".mp4") else ["explorer", target]
                subprocess.Popen(args)
                # 把资源管理器带到前台（否则它会开在应用窗口后面，用户以为没反应）
                await asyncio.sleep(0.8)
                try:
                    ps = (
                        "$w = New-Object -ComObject WScript.Shell; "
                        "$null = $w.AppActivate('文件资源管理器'); "
                        "if (-not $?) { $null = $w.AppActivate('File Explorer') }"
                    )
                    await asyncio.create_subprocess_exec(
                        "powershell", "-NoProfile", "-Command", ps,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                except Exception:
                    pass
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", target])
            else:
                subprocess.Popen(["xdg-open", out_dir])
        except Exception as e:
            raise error_response(f"打开目录失败：{e}", status=500)
        return success({"opened": out_dir, "target": target,
                        "has_final": os.path.exists(final_path)})

    @app.get("/api/video-models/discover")
    async def discover_video_models(provider: str = "seedance"):
        """**拉取该厂商账号下真实可用的视频模型清单**。

        为什么必须这么做：各家的模型 ID 带日期后缀且经常变（如火山方舟
        同时存在 doubao-seedance-1-0-pro-250528 / 2-0-260128 / 2-5-260628…），
        靠"默认写死一个名字"必然出现"我明明开通了却用不了"。
        这里直接问厂商要清单，让用户从**真实存在的**里面选。
        """
        settings = db.get_all_settings()
        api_key = _lookup_key_ci(settings.get("api_keys") or {}, provider)
        if not api_key:
            return success({"ok": False, "provider": provider,
                            "models": [], "error": f"未配置「{provider}」的 API Key"})
        VIDEO_KW = ("seedance", "video", "wan", "seaweed", "kling", "sora",
                    "veo", "pika", "hailuo", "vidu", "i2v", "t2v")
        try:
            import httpx as _httpx
            from core.adapters import get_adapter
            ad = get_adapter(provider, api_key=api_key)
            base = getattr(ad, "BASE_URL", "")
            if not base:
                return success({"ok": False, "provider": provider, "models": [],
                                "error": "该厂商不支持模型清单查询，请直接填写模型名"})
            async with _httpx.AsyncClient(timeout=45) as c:
                r = await c.get(base.rstrip("/") + "/models",
                                headers={"Authorization": f"Bearer {api_key}"})
            if r.status_code != 200:
                return success({"ok": False, "provider": provider, "models": [],
                                "error": f"厂商返回 HTTP {r.status_code}",
                                "raw": (r.text or "")[:300]})
            data = (r.json() or {}).get("data") or []
            models = []
            for m in data:
                mid = str(m.get("id") or "")
                if not mid:
                    continue
                status = str(m.get("status") or "")
                is_video = any(k in mid.lower() for k in VIDEO_KW)
                models.append({"id": mid, "name": m.get("name") or mid,
                               "status": status or "available",
                               "is_video": is_video,
                               "usable": status not in ("Shutdown", "Retiring")})
            models.sort(key=lambda x: (not x["is_video"], not x["usable"], x["id"]))
            return success({"ok": True, "provider": provider, "total": len(models),
                            "models": models,
                            "video_models": [m for m in models if m["is_video"]]})
        except Exception as e:
            logger.exception("discover video models failed")
            return success({"ok": False, "provider": provider, "models": [],
                            "error": f"{type(e).__name__}: {e}"})

    # ──────────── 🧱 可逐步确认的后期流水线（checkpoint 版）────────────

    _pipe_state: Dict[str, Any] = {}

    @app.get("/api/projects/{pid}/pipeline")
    async def get_pipeline(pid: str):
        """流水线状态：每步做了什么、跑没跑过、产物是什么"""
        from core.pipeline import status as pipe_status
        settings = db.get_all_settings()
        return success(pipe_status(settings, pid))

    @app.post("/api/projects/{pid}/pipeline/run")
    async def run_pipeline_step(pid: str, payload: Dict[str, Any] = None):
        """执行**单个**步骤（前端逐步调用 → 实现"每步停下来确认"）。

        body: {step: "base|voice|subtitle|bgm|aspect|trim", params: {...}}
        """
        payload = payload or {}
        step = str(payload.get("step") or "").strip()
        if not step:
            raise error_response("请指定要执行的步骤", status=400)
        if _pipe_state.get(pid, {}).get("running"):
            return success({"started": False, "message": "流水线正在执行中"})

        settings = db.get_all_settings()
        state = {"running": True, "step": step, "percent": 0.0, "message": "准备中…",
                 "result": None, "error": ""}
        _pipe_state[pid] = state

        async def _worker():
            try:
                from core.pipeline import run_step
                res = await run_step(
                    pid, step, payload.get("params") or {}, db=db, settings=settings,
                    progress=lambda k, pct, msg: state.update(
                        {"percent": round(pct * 100, 1), "message": msg}))
                state["result"] = res
                state["running"] = False
                state["percent"] = 100.0
                state["message"] = res.get("note") or ("完成" if res.get("ok") else res.get("error") or "失败")
                if not res.get("ok"):
                    state["error"] = res.get("error") or "执行失败"
            except Exception as e:
                logger.exception("pipeline worker crashed")
                state["running"] = False
                state["error"] = f"{type(e).__name__}: {e}"
                state["message"] = state["error"]

        asyncio.create_task(_worker())
        return success({"started": True, "step": step})

    @app.get("/api/projects/{pid}/pipeline/status")
    async def pipeline_step_status(pid: str):
        st = _pipe_state.get(pid)
        if not st:
            return success({"running": False, "percent": 0.0, "message": "尚未开始",
                            "result": None, "error": ""})
        return success(dict(st))

    @app.get("/api/projects/{pid}/pipeline/artifact/{key}")
    async def pipeline_artifact(pid: str, key: str, download: int = 0):
        """查看/下载某一步的中间产物（存在 cache 里，不在输出目录，所以要单独开个口子）。

        这样「看这一步的结果」按钮才有真实反馈 —— 每一步都能单独预览。
        """
        from core.pipeline import load_state, step_output
        settings = db.get_all_settings()
        p = step_output(settings, pid, key)
        if not p or not os.path.exists(p):
            raise error_response("这一步还没有产物，先跑它", status=404)
        from fastapi.responses import FileResponse
        return FileResponse(
            p, media_type="video/mp4", filename=os.path.basename(p),
            content_disposition_type="attachment" if download else "inline")

    @app.get("/api/projects/{pid}/shots/{sid}/plan")
    async def get_shot_plan(pid: str, sid: str, resolution: str = "",
                            duration: int = 0, measure: int = 1):
        """给一个分镜算出**可执行的拍摄计划**（分段 / 分辨率 / 转场 / 串帧）。

        这是"分镜写 10 秒但模型只能出 5 秒"这个问题的正面回答：
        在**花钱之前**就告诉用户会怎么拆、要调用几次模型、画面怎么接上。

        `measure=1`（默认）会把台词真的合成一遍量出**真实时长**，
        让这份预览和真正生成时的规划**完全一致** —— 预览要是跟实际不一致，
        那这预览就是在骗人，比没有还糟。TTS 免费且有缓存，`measure=0` 可关掉。
        """
        shot = db.get_shot(sid)
        if not shot:
            raise error_response("分镜不存在", status=404)
        from core.shotplan import plan_shot, plan_summary
        from core.model_catalog import find_video_model
        provider = shot.get("model_provider") or ""
        model = shot.get("model_name") or ""
        # 跨厂商查（理由同 _try_api_video：provider 和 model_name 可能不一致）
        entry = find_video_model(model, provider) or None
        settings = db.get_all_settings()
        audio_sec = 0.0
        if measure and not duration:
            audio_sec = await _measure_shot_audio(
                shot, settings, db.list_characters(pid) or [])
        plan = plan_shot(shot, model_entry=entry,
                         resolution=resolution or shot.get("resolution") or "1080P",
                         want_duration=duration or None,
                         audio_seconds=audio_sec,
                         provider=provider)
        plan["summary"] = plan_summary(plan)
        plan["provider"] = provider
        plan["measured"] = bool(measure and not duration)
        return success(plan)

    @app.get("/api/projects/{pid}/consistency")
    async def get_consistency(pid: str):
        """这个项目的分镜"生成条件"一致吗？不一致的话怎么统一？

        ★ 这是"多镜头拼起来一眼AI"的**第一现场**。
          线上实测：项目 `19636f68` 的 6 个分镜用了 3 种不同模型
          （video-01 / MiniMax-Hailuo-02 / kling-1.6），出来的画面
          一个是 3D 卡通、一个是写实真人 —— **不是同一个世界**。
          调色、转场、归一化能让接缝不那么刺眼，但补不回画风的差异。
          所以必须在**生成之前**就把条件锁死。
        """
        shots = db.list_shots(pid) or []
        if not shots:
            return success({"consistent": True, "total_shots": 0,
                            "warnings": ["该项目还没有分镜"], "deviations": []})
        from core.consistency import diff_shots, plan_unify, consistency_brief
        shots_sorted = sorted(shots, key=lambda s: s.get("order_index") or 0)
        rep = diff_shots(shots_sorted)
        plan = plan_unify(shots_sorted, rep.get("canonical"))
        out = dict(plan)
        # `plan_unify` 把体检汇总放在 report 里；前端要的是**平铺**的字段，
        # 顺手把它摊上来，省得前端为了显示一个"是否一致"去翻两层。
        out["consistent"] = rep.get("consistent")
        out["worst_level"] = rep.get("worst_level")
        out["counts"] = rep.get("counts")
        out["deviations"] = rep.get("deviations")
        out["total_shots"] = rep.get("total_shots")
        out.pop("report", None)
        out["brief"] = consistency_brief(shots_sorted)
        # ★★ 还要量**实际的片**，不能只看设置（真实事故的教训）：
        #   项目设置统一成 16:9 之后，一致性体检会报"一致"——
        #   但库里第 5 镜选中的素材其实是 **720x720 方形**（修复之前生成的）。
        #   只比设置等于"用配置证明配置没错"，而观众看到的是文件。
        #   所以这里逐个**实测**已选中素材的画幅，不符就明确列出来。
        try:
            from core import aspect as _asp
            measured, bad = [], []
            for sh in shots_sorted:
                path = ""
                try:
                    from core.compose import _selected_candidate_path as _scp
                    path = _scp(sh) or ""
                except Exception:
                    path = ""
                want = str(sh.get("aspect_ratio") or "16:9")
                if not path or not os.path.exists(path):
                    measured.append({"shot_id": sh.get("id"),
                                     "index": (sh.get("order_index") or 0) + 1,
                                     "want": want, "actual": "", "ok": None})
                    continue
                g = _asp.guard_clip(path, want, provider=str(sh.get("model_provider") or ""),
                                    model=str(sh.get("model_name") or ""),
                                    resolution=str(sh.get("resolution") or ""),
                                    record=False)
                measured.append({"shot_id": sh.get("id"),
                                 "index": (sh.get("order_index") or 0) + 1,
                                 "want": want, "actual": g.get("actual") or "",
                                 "size": f"{g.get('w')}x{g.get('h')}",
                                 "crop_loss": g.get("crop_loss") or 0.0,
                                 "ok": g.get("ok"), "file": path})
                if g.get("ok") is False:
                    bad.append(((sh.get("order_index") or 0) + 1, g))
            out["measured_aspect"] = measured
            if bad:
                _txt = "、".join(
                    f"第 {i} 镜 {g.get('w')}x{g.get('h')}（{g.get('actual')}，"
                    f"要 {g.get('want')}，会裁掉 {g.get('crop_loss', 0) * 100:.0f}%）"
                    for i, g in bad)
                out.setdefault("warnings", []).insert(
                    0, f"⚠ 实测画幅：{len(bad)} 个分镜的**已生成素材**与分镜画幅不符 —— "
                       f"{_txt}。这几镜需要重新生成（改设置不会改变已有的文件）。")
                out["aspect_mismatch"] = len(bad)
        except Exception as _e:
            logger.warning("一致性体检的画幅实测没跑起来：%s", _e)
        return success(out)

    @app.post("/api/projects/{pid}/consistency/unify")
    async def unify_consistency(pid: str, payload: Dict[str, Any] = None):
        """把偏离的分镜拉回项目的统一生成条件。

        ★ `dry_run` **默认 True** —— 只预演不落库。
          因为改模型/画幅会改变出片效果、还可能要求重新生成（花钱），
          这种事必须用户明确点头，不能后端替他决定。
        """
        payload = payload or {}
        dry = payload.get("dry_run", True)
        if isinstance(dry, str):
            dry = dry.strip().lower() not in ("0", "false", "no", "")
        shots = sorted(db.list_shots(pid) or [], key=lambda s: s.get("order_index") or 0)
        if not shots:
            raise error_response("该项目还没有分镜", status=400)
        from core.consistency import plan_unify, diff_shots
        # ★ 允许**指定**统一到哪一组条件，而不是只能按"多数票"。
        #   多数票在很多真实项目里是错的：线上一个 5 镜项目里 2 个 video-01、
        #   2 个 Hailuo-02、1 个 kling-1.6 —— 按多数票会统一到画质最差的那批，
        #   而用户真正想统一到的是"我打算用的那个模型"。
        _canon_in = payload.get("canonical")
        if not isinstance(_canon_in, dict):
            _canon_in = {k: payload.get(k) for k in
                         ("model_provider", "model_name", "resolution", "aspect_ratio")
                         if payload.get(k)}
        plan = plan_unify(shots, _canon_in or None)
        # ★★ 画幅自检：统一后的分辨率会不会让这个项目的镜头变成别的画幅？
        #   实测教训：海螺 2.3 的 768P 出 1:1（方形），而项目是 16:9 ——
        #   统一条件如果踩到这个坑，等于把整片都裁掉 44% 画面。
        try:
            from core import aspect as _asp
            _c = plan.get("canonical") or {}
            _prov, _model = _c.get("model_provider") or "", _c.get("model_name") or ""
            _res, _ar = _c.get("resolution") or "", _c.get("aspect_ratio") or "16:9"
            _bad = _asp.resolution_conflicts_aspect(_prov, _model, _res, _ar)
            if _bad:
                from core.model_catalog import caps_for
                _alts = _asp.resolutions_matching_aspect(
                    _prov, _model, _ar, caps_for(_prov, _model) or {})
                plan["warnings"].insert(0,
                    f"⚠ 画幅：统一到「{_model} + {_res}」时实测出的是 {_bad}，"
                    f"与项目画幅 {_ar} 不符 —— 每个镜头都会被裁掉画面。"
                    + (f"画幅相符的分辨率：{'、'.join(_alts)}。" if _alts else
                       "该型号的画幅映射没有实测数据，无法自动给出替代分辨率。"))
                plan["aspect_conflict"] = {"resolution": _res, "actual": _bad,
                                           "want": _ar, "alternatives": _alts}
            elif _asp.expected_aspect(_prov, _model, _res):
                plan["aspect_ok"] = _asp.expected_aspect(_prov, _model, _res)
        except Exception as _e:
            logger.warning("统一条件的画幅自检没跑起来：%s", _e)
        done: List[Dict[str, Any]] = []
        if not dry:
            for a in plan["actions"]:
                try:
                    db.update_shot(a["shot_id"], **a["patch"])
                    done.append({"shot_id": a["shot_id"], "index": a["index"],
                                 "applied": a["patch"]})
                except Exception as e:
                    logger.warning("统一分镜 %s 失败：%s", a["shot_id"], e)
        after = diff_shots(db.list_shots(pid) or []) if not dry else plan["report"]
        return success({
            "dry_run": dry,
            "canonical": plan["canonical"],
            "planned": plan["actions"],
            "applied": done,
            "need_regenerate": plan["need_regenerate"],
            "warnings": plan["warnings"],
            "consistent_after": after.get("consistent"),
            "counts_after": after.get("counts"),
            "aspect_ok": plan.get("aspect_ok") or "",
            "aspect_conflict": plan.get("aspect_conflict") or {},
            "note": ("预演完成，没有改动任何分镜（想真正执行请传 dry_run=false）"
                     if dry else
                     "已统一生成条件。标了『必须重新生成』的分镜需要重新出片，"
                     "否则保留的还是旧画风那一段。"),
        })


    @app.get("/api/projects/{pid}/continuity")
    async def get_continuity(pid: str):
        """整片的**衔接计划**：每个镜头用硬切还是溶解、要不要接上一镜的尾帧。

        直接回答用户最在意的那条："不同视频之间的衔接问题"。
        """
        from core.continuity import chain_plan, chain_summary, final_timeline
        shots = sorted(db.list_shots(pid) or [], key=lambda s: s.get("order_index") or 0)
        scenes = {s["id"]: s for s in (db.list_scenes(pid) or [])}

        def selected_video(sh):
            cands = sh.get("candidates") or []
            if isinstance(cands, str):
                try:
                    cands = json.loads(cands)
                except Exception:
                    cands = []
            cands = [c for c in cands if isinstance(c, dict)]
            sel = next((c for c in cands if c.get("is_selected")), None) \
                or (cands[-1] if cands else None)
            return (sel or {}).get("path") or ""

        plan = chain_plan(shots, scenes=scenes, selected_video_of=selected_video)
        # ★ 时间轴要用**真实片段时长**：分镜标称合计 60s 而成片可能只有 28s
        #   （模型实际只出了那么长）。用标称会让用户看到一个根本不存在的时长。
        real_durs: List[float] = []
        for sh in shots:
            d = 0.0
            cands = sh.get("candidates") or []
            if isinstance(cands, str):
                try:
                    cands = json.loads(cands)
                except Exception:
                    cands = []
            cands = [c for c in cands if isinstance(c, dict)]
            sel = next((c for c in cands if c.get("is_selected")), None) \
                or (cands[-1] if cands else None)
            if sel:
                try:
                    d = float(sel.get("duration_seconds") or 0)
                except (TypeError, ValueError):
                    d = 0.0
            real_durs.append(d if d > 0.2 else float(sh.get("duration_seconds") or 5))
        tl = final_timeline(shots, plan, real_durs)
        nominal = sum(float(s.get("duration_seconds") or 5) for s in shots)
        return success({
            "plan": plan,
            "summary": chain_summary(plan),
            "timeline": [{k: v for k, v in s.items()} for s in tl["shots"]],
            "total": tl["total"],
            "timeline_shrink": tl["timeline_shrink"],
            "nominal_total": round(nominal, 2),
            "uses_real_durations": True,
            "note": ("成片总长按**已生成片段的真实时长**计算；"
                     "标称总长 %.1fs 与它不同，是因为部分分镜的实际片段比剧本写的短。"
                     % nominal) if abs(nominal - tl["total"]) > 0.5 else "",
        })

    @app.get("/api/projects/{pid}/dialogue-audit")
    async def dialogue_audit(pid: str):
        """全片台词体检：哪些分镜**没有可念的台词**、哪些台词放不下。

        用户说"语音+字幕要么不出现要么特别不协调" ——
        最上游的原因就是分镜里根本没有台词。这个接口把这件事变成看得见的数字。
        """
        from core.dialogue import timeline_dialogue_stats
        out = []
        total_lines = total_chars = 0
        for sh in sorted(db.list_shots(pid) or [], key=lambda s: s.get("order_index") or 0):
            st = timeline_dialogue_stats(sh.get("layer2_timeline"),
                                         float(sh.get("duration_seconds") or 0))
            total_lines += st["line_count"]
            total_chars += st["total_chars"]
            out.append({
                "shot_id": sh.get("id"), "order_index": sh.get("order_index"),
                "duration": sh.get("duration_seconds"),
                **st,
                "needs_dialogue": not st["has_dialogue"],
            })
        missing = [x for x in out if x["needs_dialogue"]]
        return success({
            "shots": out,
            "total_lines": total_lines,
            "total_chars": total_chars,
            "shots_without_dialogue": len(missing),
            "shot_count": len(out),
            "advice": ("有 %d/%d 个分镜没有可念的台词 —— 配音只能退化成朗读剧情概述。"
                       "到「分镜」页点「🎬 补全台词」可以让 AI 按剧情写出真正的对白。"
                       % (len(missing), len(out))) if missing else "全部分镜都有台词。",
        })

    @app.post("/api/projects/{pid}/shots/{sid}/fill-dialogue")
    async def fill_shot_dialogue(pid: str, sid: str, payload: Dict[str, Any] = None):
        """给一个分镜**补写台词**（复用三层提示词生成，但强调台词）。

        为什么单独做这个动作：存量分镜是旧提示词生成的，`layer2_timeline`
        里只有动作描述、没有台词。要修好语音字幕，必须先让源头有可念的句子。
        """
        payload = payload or {}
        shot = db.get_shot(sid)
        if not shot:
            raise error_response("分镜不存在", status=404)
        settings = db.get_all_settings()
        try:
            from core.llm import LLMClient, ScriptService
            llm = _make_llm(settings)
            svc = ScriptService(llm)
            scene = db.get_scene(shot["scene_id"]) if shot.get("scene_id") else None
            chars = db.list_characters(pid) or []
            desc = {
                "title": (scene or {}).get("name") or "",
                "location": (scene or {}).get("description") or "",
                "duration_seconds": shot.get("duration_seconds") or 10,
                "actions": [shot.get("layer1_overview") or ""],
                "dialogues": [],
                "mood": "",
            }
            data = await svc.generate_three_layer_prompt(desc, chars, scene)
        except Exception as e:
            raise error_response(f"台词生成失败：{type(e).__name__}: {e}", status=500)

        tl = data.get("layer2_timeline") or []
        stats = data.get("dialogue_stats") or {}
        if not tl:
            raise error_response("AI 没有返回可用的时间线，请稍后重试", status=500)
        patch = {"layer2_timeline": json.dumps(tl, ensure_ascii=False)}
        if data.get("layer1_overview"):
            patch["layer1_overview"] = data["layer1_overview"]
        db.update_shot(sid, **patch)
        return success({
            "shot_id": sid, "timeline": tl, "stats": stats,
            "speakers": data.get("speakers") or [],
            "note": (f"已补写 {stats.get('line_count', 0)} 句台词"
                     f"（{stats.get('total_chars', 0)} 字）"),
        })

    @app.post("/api/projects/{pid}/pipeline/skip")
    async def pipeline_skip_step(pid: str, payload: Dict[str, Any] = None):
        """跳过某一步（跳过后产物沿用上一步，下游照常继续）。

        解决"视频模型自己就带对白和字幕，配音/字幕区显得多余"这个矛盾：
        不想做的那一步直接跳过，不用硬跑一遍。
        """
        payload = payload or {}
        step = str(payload.get("step") or "").strip()
        if not step:
            raise error_response("请指定要跳过的步骤", status=400)
        settings = db.get_all_settings()
        if payload.get("undo"):
            from core.pipeline import unskip_step
            r = unskip_step(settings, pid, step)
        else:
            from core.pipeline import skip_step
            r = skip_step(settings, pid, step, str(payload.get("reason") or ""))
        if not r.get("ok"):
            raise error_response(r.get("error") or "操作失败", status=400)
        return success(r)

    @app.post("/api/projects/{pid}/pipeline/bgm")
    async def pipeline_set_bgm(pid: str, file: UploadFile = File(...)):
        """为流水线的「④ 背景音乐」步骤上传 BGM"""
        settings = db.get_all_settings()
        out_dir = os.path.join(
            settings.get("cache_dir") or os.path.join(
                os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache"),
            "pipeline", pid)
        os.makedirs(out_dir, exist_ok=True)
        ext = os.path.splitext(os.path.basename(file.filename or "bgm.mp3"))[1].lower() or ".mp3"
        dst = os.path.join(out_dir, f"bgm{ext}")
        with open(dst, "wb") as f:
            shutil.copyfileobj(file.file, f)
        from core.pipeline import load_state, save_state
        st = load_state(settings, pid)
        st["bgm_path"] = dst
        # 换了 BGM → ① 旧的 BGM 步骤产物作废 ② 它下游的步骤也作废
        steps_state = st.setdefault("steps", {})
        order = ["base", "voice", "subtitle", "bgm", "aspect", "trim"]
        for later in order[order.index("bgm"):]:
            old = steps_state.get(later)
            if old:
                old.update({"ok": False, "stale": True, "output": "",
                            "note": "BGM 已更换，需要重跑"})
        save_state(settings, pid, st)
        return success({"bgm_path": dst, "filename": os.path.basename(file.filename or "")})

    @app.post("/api/projects/{pid}/pipeline/finalize")
    async def pipeline_finalize(pid: str):
        """把最后一个已完成步骤的产物落成 final.mp4"""
        from core.pipeline import finalize
        settings = db.get_all_settings()
        r = finalize(settings, pid)
        if not r.get("ok"):
            raise error_response(r.get("error") or "生成成片失败", status=400)
        return success({"output_path": r["output"], "source": r["source"],
                        "url": f"/api/projects/{pid}/final?download=1"})

    @app.post("/api/projects/{pid}/pipeline/reset")
    async def pipeline_reset(pid: str):
        """清空流水线记录（用于从头再来；不会删除已生成的分镜视频）"""
        from core.pipeline import pipeline_dir, save_state
        settings = db.get_all_settings()
        save_state(settings, pid, {"steps": {}})
        _pipe_state.pop(pid, None)
        return success({"reset": True, "dir": pipeline_dir(settings, pid)})

    @app.get("/api/model-catalog")
    async def get_model_catalog(kind: str = "video", provider: str = ""):
        """模型能力目录（用户看得懂的选项：清晰度 / 时长 / 适合什么场景）。

        `kind`: video（视频模型）| llm（文本模型）
        前端渲染成带说明的选项，而不是一串看不懂的模型 ID。
        """
        from core import model_catalog as MC
        if kind == "llm":
            return success({"kind": "llm", "catalog": MC.get_llm_catalog(provider)})
        return success({"kind": "video", "catalog": MC.get_video_catalog(provider)})

    # ─────────────── VideoForge 模型：四类平台统一目录 / 选择 / 连通性测试 ───────────────
    def _models_selected(settings: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
        tts = settings.get("tts_models") or {}
        tts = tts if isinstance(tts, dict) else {}
        audio_provider = next((str(k) for k, v in tts.items() if v), "")
        return {
            "text": {"provider": str(settings.get("llm_provider") or ""),
                     "model": str(settings.get("llm_model") or "")},
            "image": {"provider": str(settings.get("image_provider") or ""),
                      "model": str(settings.get("image_model") or "")},
            "video": {"provider": str(settings.get("default_model_provider") or ""),
                      "model": str(settings.get("default_model_name") or "")},
            "audio": {"provider": audio_provider,
                      "model": str(tts.get(audio_provider) or "") if audio_provider else ""},
        }

    @app.get("/api/models/registry")
    async def models_registry():
        """四类平台（文本/图像/视频/音频）的统一目录：用户能看清「支持谁、还差哪个 Key」。"""
        from core import provider_registry as PR
        settings = db.get_all_settings()
        payload = PR.registry(settings, _models_selected(settings))
        # 自定义 base_url（实测这台机器直连 api.openai.com 会 20s 超时；
        # 走中转/代理时必须能改地址，否则"能选却连不上"）
        overrides = settings.get("model_base_urls") or {}
        if isinstance(overrides, dict):
            for category in payload["categories"]:
                for provider in category["providers"]:
                    key = "%s:%s" % (category["id"], provider["id"])
                    custom = str(overrides.get(key) or "").strip()
                    provider["base_url_override"] = custom
                    provider["base_url_effective"] = custom or provider["base_url"]
        return success(payload)

    @app.post("/api/models/select")
    async def models_select(payload: Dict[str, Any] = None):
        """把「某类用哪个平台/哪个模型」写进设置（会校验平台确实在目录里）。"""
        from core import provider_registry as PR
        data = payload or {}
        category = str(data.get("category") or "")
        provider_id = str(data.get("provider") or "")
        model = str(data.get("model") or "")
        if category not in ("text", "image", "video", "audio"):
            return fail("category 必须是 text/image/video/audio")
        provider = PR.find(category, provider_id)
        if not provider:
            return fail("目录里没有这个平台：%s/%s" % (category, provider_id))
        model = model or str(provider.get("default_model") or "")
        base_url = str(data.get("base_url") or "").strip()
        settings = db.get_all_settings()
        if base_url or data.get("clear_base_url"):
            overrides = dict(settings.get("model_base_urls") or {}) if isinstance(
                settings.get("model_base_urls"), dict) else {}
            if base_url:
                overrides["%s:%s" % (category, provider_id)] = base_url
            else:
                overrides.pop("%s:%s" % (category, provider_id), None)
            settings["model_base_urls"] = overrides
        if category == "text":
            settings["llm_provider"] = provider_id
            settings["llm_model"] = model
            settings["enable_llm_local"] = provider_id == "ollama"
        elif category == "image":
            settings["image_provider"] = provider_id
            settings["image_model"] = model
        elif category == "video":
            settings["default_model_provider"] = provider_id
            settings["default_model_name"] = model
        else:
            tts = dict(settings.get("tts_models") or {}) if isinstance(settings.get("tts_models"), dict) else {}
            for key in list(tts):
                tts[key] = ""
            tts[provider_id] = model
            settings["tts_models"] = tts
        db.set_settings(settings)
        return success({"category": category, "provider": provider_id, "model": model,
                        "selected": _models_selected(settings)})

    @app.post("/api/models/test")
    async def models_test(payload: Dict[str, Any] = None):
        """连通性测试：只做**便宜**的真实探测，绝不为测试花钱出片。

        文本：发一个 max_tokens=1 的最小请求（成本可忽略）
        图像/音频：优先列模型（GET /models）；不支持就如实说「无法便宜验证」
        视频：按次计费，不做探测，返回 unverified 并说明原因
        """
        import time
        from core import provider_registry as PR
        data = payload or {}
        category = str(data.get("category") or "")
        provider_id = str(data.get("provider") or "")
        provider = PR.find(category, provider_id)
        if not provider:
            return fail("目录里没有这个平台：%s/%s" % (category, provider_id))
        settings = db.get_all_settings()
        bucket = provider.get("key_field") or "llm_api_keys"
        keys = settings.get(bucket) or {}
        keys = keys if isinstance(keys, dict) else {}
        key = next((str(v) for k, v in keys.items()
                    if str(k).lower() == provider_id and v), "")
        if not key:
            for env_name in (provider.get("env") or []):
                key = os.environ.get(env_name) or ""
                if key:
                    break
        if not key and provider_id not in ("ollama", "edge", "local"):
            return success({"category": category, "provider": provider_id, "ok": False,
                            "status": "no_key",
                            "message": "还没填这个平台的 API Key（设置里填完再点测试）"})
        model = str(data.get("model") or provider.get("default_model") or "")
        protocol = str(provider.get("protocol") or "native")
        overrides = db.get_all_settings().get("model_base_urls") or {}
        override = ""
        if isinstance(overrides, dict):
            override = str(overrides.get("%s:%s" % (category, provider_id)) or "").strip()
        base = (override or provider.get("base_url") or "").rstrip("/")
        path = str(provider.get("path") or "")
        url = base + path
        timeout = 20.0
        started = time.time()

        import httpx
        async def _probe() -> Dict[str, Any]:
            headers: Dict[str, str] = {}
            if protocol == "openai":
                headers["Authorization"] = "Bearer " + key
                if category == "text":
                    body = {"model": model, "messages": [{"role": "user", "content": "ping"}],
                            "max_tokens": 1, "stream": False}
                    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                        r = await client.post(url, headers=headers, json=body)
                    return {"status_code": r.status_code, "detail": r.text[:300]}
                # 图像/音频：能列模型就列，列不了就如实说
                list_url = base + "/v1/models"
                async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                    r = await client.get(list_url, headers=headers)
                if r.status_code == 200:
                    return {"status_code": 200, "detail": "模型列表可读", "evidence": "GET " + list_url}
                return {"status_code": r.status_code, "detail": r.text[:300],
                        "unverifiable": True,
                        "message": "这家没有便宜的连通性探测接口，只能真实跑一次才知道"}
            if protocol == "anthropic":
                headers.update({"x-api-key": key, "anthropic-version": "2023-06-01",
                                "content-type": "application/json"})
                body = {"model": model, "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}]}
                async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                    r = await client.post(url, headers=headers, json=body)
                return {"status_code": r.status_code, "detail": r.text[:300]}
            if protocol == "gemini":
                list_url = base + "/v1beta/models"
                async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                    r = await client.get(list_url, headers={"x-goog-api-key": key})
                return {"status_code": r.status_code, "detail": r.text[:300], "evidence": "GET " + list_url}
            if protocol == "ollama":
                get_url = base + "/api/tags"
                async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                    r = await client.get(get_url)
                return {"status_code": r.status_code, "detail": r.text[:300], "evidence": "GET " + get_url}
            if category == "video":
                return {"status_code": 0, "unverifiable": True,
                        "message": "视频模型按次计费，这里不做连通性探测；请用「白膜预演 + 480p 小样」验证"}
            return {"status_code": 0, "unverifiable": True,
                    "message": "这家是原生协议，没有统一的便宜探测接口"}

        try:
            result = await _probe()
        except Exception as exc:  # noqa: BLE001
            return success({"category": category, "provider": provider_id, "ok": False,
                            "status": "error", "latency_ms": int((time.time() - started) * 1000),
                            "message": "%s: %s" % (type(exc).__name__, exc)})
        code = int(result.get("status_code") or 0)
        ok = code in (200, 201)
        if result.get("unverifiable"):
            status = "unverified"
        elif ok:
            status = "verified"
        elif code in (401, 403):
            status = "unauthorized"
        else:
            status = "error"
        return success({"category": category, "provider": provider_id, "ok": ok or status == "unverified",
                        "status": status, "status_code": code,
                        "latency_ms": int((time.time() - started) * 1000),
                        "url": url, "model": model,
                        "message": result.get("message") or result.get("detail") or "",
                        "evidence": result.get("evidence") or ""})

    @app.get("/api/model-catalog/resolve")
    async def resolve_model_params(provider: str, model: str,
                                   resolution: str = "", duration: int = 0):
        """给定厂商+模型+想要的清晰度/时长，返回**一定合法**的组合（含是否被自动调整）"""
        from core.model_catalog import pick_valid_combo
        return success(pick_valid_combo(provider, model, resolution, duration))

    @app.get("/api/video-models/known")
    async def known_video_models(provider: str = ""):
        """列出各厂商**已知可用**的模型版本，供「换模型」弹窗直接选。

        为什么要这个：视频模型版本很多（同一个 Seedance 就有 1.0/1.5/2.0/2.5，
        海螺有 01/02 等），只给一个文本框让用户手打模型名，
        结果就是"配了 Key 却总是失败/只能本地合成"。
        这里给下拉选项；同时保留手填（自定义接入点 ID）。
        对火山方舟这类支持列清单的厂商，还可以用「拉取可用模型清单」拿账号真实清单。
        """
        # ★ 唯一数据源：core/model_catalog.py 的 VIDEO_CATALOG。
        #   以前这里手写了一份硬编码清单，和目录里的能力表各说各话，
        #   结果就是"选了一个账号里根本不存在的版本"（如 MiniMax-Hailuo-02-Fast）。
        #   现在一律从目录生成，标签直接写成用户看得懂的中文。
        from core.model_catalog import VIDEO_CATALOG
        KNOWN: Dict[str, List[Dict[str, str]]] = {}
        for prov, entry in VIDEO_CATALOG.items():
            rows: List[Dict[str, str]] = []
            for m in (entry.get("models") or []):
                caps = m.get("caps") or {}
                # 把"分辨率→可用时长"翻译成一句人话
                cap_bits = []
                for res, durs in caps.items():
                    cap_bits.append(f"{res} {'/'.join(str(d) + 's' for d in durs)}")
                dur_txt = "、".join(cap_bits)
                mode = m.get("mode_zh") or m.get("mode") or ""
                label = f"{m.get('label') or m['id']}｜{mode}"
                if dur_txt:
                    label += f"｜{dur_txt}"
                rows.append({"id": m["id"], "label": label,
                             "mode": m.get("mode") or "", "mode_zh": mode,
                             "tags": m.get("tags") or [],
                             "caps": caps})
            # 账号上确认不存在的版本（如 Hailuo-02-Fast）不列进来，避免又选到坑。
            # 注意 unavailable 是 [{"id":..., "reason":...}] 这种字典列表，
            # 不能直接 set()（dict 不可哈希 → 之前这里 500）。
            bad = {(u.get("id") if isinstance(u, dict) else str(u))
                   for u in (entry.get("unavailable") or [])}
            if bad:
                rows = [r for r in rows if r["id"] not in bad]
            if rows:
                KNOWN[prov] = rows
        if provider:
            return success({"provider": provider, "models": KNOWN.get(provider.lower(), [])})
        return success({"providers": KNOWN})

    @app.post("/api/video-models/test")
    async def test_video_model(payload: Dict[str, Any] = None):
        """一键测试视频模型连通性（不真正出片，只做鉴权 + 参数校验）。

        解决"配了 Key 到底能不能用"这个反复出现的问题：
        把厂商返回的原始错误**原样、翻译**给用户看。
        """
        payload = payload or {}
        settings = db.get_all_settings()
        provider = payload.get("provider") or settings.get("default_model_provider") or "seedance"
        model_name = payload.get("model_name") or settings.get("default_model_name") or \
            DEFAULT_MODEL_IDS.get(provider, "")
        api_key = _lookup_key_ci(settings.get("api_keys") or {}, provider)
        if not api_key:
            return success({"ok": False, "provider": provider,
                            "error": f"未配置「{provider}」的 API Key",
                            "hint": "到「设置 → 视频模型 API Keys」填写"})
        try:
            from core.adapters import get_adapter
            adapter = get_adapter(provider, api_key=api_key, config={"model_name": model_name})
        except Exception as e:
            return success({"ok": False, "provider": provider, "error": f"适配器加载失败：{e}"})

        # 用一次最小成本的合法请求探活：能鉴权通过就说明 Key 有效；
        # 厂商报"模型未开通/模型名错"也能被我们翻译出来。
        try:
            import httpx
            client = await adapter._ensure_client()
            base = getattr(adapter, "BASE_URL", "")
            probed = False
            raw_text = ""
            if base:
                url = base.rstrip("/") + "/contents/generations/tasks"
                r = await client.post(url, json={"model": model_name,
                                                 "content": [{"type": "text", "text": "连接测试"}]})
                raw_text = (r.text or "")[:600]
                probed = True
                if r.status_code == 401:
                    return success({"ok": False, "provider": provider, "model": model_name,
                                    "error": "API Key 无效或已过期（HTTP 401）", "raw": raw_text,
                                    "hint": "到厂商控制台重新生成 Key"})
                if r.status_code == 404 or "ModelNotOpen" in raw_text or "not activated" in raw_text:
                    return success({"ok": False, "provider": provider, "model": model_name,
                                    "error": "鉴权通过，但该模型未开通或模型名不对", "raw": raw_text,
                                    "hint": "到厂商控制台①开通该模型服务 ②确认模型 ID / 推理接入点 ID。"
                                            "火山方舟需在「在线推理」创建接入点，ID 形如 ep-2024xxxxxx-xxxxx"})
                if r.status_code in (200, 201, 202):
                    return success({"ok": True, "provider": provider, "model": model_name,
                                    "message": "鉴权与模型均可用（已成功创建测试任务）",
                                    "raw": raw_text})
                return success({"ok": False, "provider": provider, "model": model_name,
                                "error": f"厂商返回 HTTP {r.status_code}", "raw": raw_text,
                                "hint": _api_error_hint(provider, raw_text)})
            return success({"ok": True, "provider": provider, "model": model_name,
                            "message": "Key 已配置；该适配器无独立探活接口，请直接试生成一次"})
        except Exception as e:
            return success({"ok": False, "provider": provider, "model": model_name,
                            "error": f"{type(e).__name__}: {e}",
                            "hint": _api_error_hint(provider, str(e))})
        finally:
            try:
                await adapter.close()
            except Exception:
                pass

    @app.get("/api/video-models/configured")
    async def configured_video_models():
        """列出哪些视频模型真的配了 Key（设置页用）"""
        settings = db.get_all_settings()
        keys = settings.get("api_keys") or {}
        out = []
        for name in DEFAULT_MODEL_IDS:
            k = _lookup_key_ci(keys, name)
            if k:
                out.append({"provider": name, "model": settings.get("default_model_name")
                            or DEFAULT_MODEL_IDS.get(name, ""), "has_key": True})
        return success({"configured": out,
                        "default_provider": settings.get("default_model_provider"),
                        "default_model": settings.get("default_model_name")})

    @app.get("/api/projects/{pid}/final/info")
    async def final_info(pid: str):
        """成片信息：是否存在 / 大小 / 时长 / 修改时间（前端用来显示预览播放器）"""
        import glob as _glob
        out_dir = os.path.join(_outputs_root(), pid)
        candidates = [os.path.join(out_dir, "final.mp4")]
        candidates += sorted(_glob.glob(os.path.join(out_dir, "*.mp4")),
                             key=lambda p: os.path.getmtime(p), reverse=True)
        for p in candidates:
            if os.path.exists(p):
                dur = 0.0
                try:
                    from core.compose import probe_duration
                    dur = await probe_duration(p)
                except Exception:
                    pass
                return success({
                    "exists": True, "path": p, "filename": os.path.basename(p),
                    "size_bytes": os.path.getsize(p), "duration": dur,
                    "mtime": os.path.getmtime(p),
                    "url": f"/api/projects/{pid}/final?v={int(os.path.getmtime(p))}",
                    "download_url": f"/api/projects/{pid}/final?download=1&v={int(os.path.getmtime(p))}",
                })
        return success({"exists": False})

    @app.get("/api/projects/{pid}/final")
    async def final_video(pid: str, download: int = 0):
        """成片播放 / 下载。

        `download=1` 时返回 `Content-Disposition: attachment` ——
        否则 WebView2 会把「⬇ 下载成片」当成在线播放，用户点完看不到任何文件落地。
        """
        p = os.path.join(_outputs_root(), pid, "final.mp4")
        if not os.path.exists(p):
            raise error_response("该项目还没有成片，请先点「一键成片」", status=404)
        headers = {}
        if download:
            fn = f"VideoForge-{pid[:8]}.mp4"
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(fn)}"
        return FileResponse(p, media_type="video/mp4", headers=headers or None)

    # ══════════════════════════════════════════════════════════════════
    # ★★ 2026-10-20：补齐「一步步走」的阶段 HTTP 路由
    # ══════════════════════════════════════════════════════════════════
    # 实测缺口：前端 `frontend/js/chat.js` 的三张卡片按钮
    #   「✅ 确认，开始做」→ POST /stage/confirm
    #   「✏ 我要改一下」  → POST /stage/revise
    #   「这步跳过」      → POST /stage/skip
    # 全部直连这些路由，但**后端从来没注册过它们**
    # （`main.py` 里连 "confirm" 这个字符串都出现 0 次）→ 点确认必然 404，
    # 用户看到的就是"点确认没反应、一路交付跑不通"。
    # 这里补上；执行一律走 `core.agent_tools_stages.run_stage`（唯一执行入口），
    # 与对话路径行为完全一致。

    @app.get("/api/projects/{pid}/stage/status")
    async def api_stage_status(pid: str):
        from core import stages as S
        if not db.get_project(pid):
            raise error_response("项目不存在", status=404)
        return success(S.summary(db, pid))

    @app.post("/api/projects/{pid}/stage/propose")
    async def api_stage_propose(pid: str, payload: Dict[str, Any] = None):
        from core import stages as S
        payload = payload or {}
        if not db.get_project(pid):
            raise error_response("项目不存在", status=404)
        key = str(payload.get("stage") or "").strip().lower()
        if not key:
            key = str(S.next_stage_key(db, pid) or "").strip().lower()
        if key not in S.STAGE_BY_KEY:
            raise error_response(f"没有这个阶段：{key or '(空)'}", status=400)
        args = payload.get("args")
        args = dict(args) if isinstance(args, dict) else {}
        for k, v in payload.items():
            if k not in ("stage", "args", "note") and v not in (None, ""):
                args.setdefault(k, v)
        args["project_id"] = pid
        card = S.proposal_text(db, pid, key, args)
        if isinstance(card, dict) and card.get("ok") is False:
            raise error_response(card.get("error") or "提案失败", status=400)
        S.set_pending(pid, key, args, note=str(payload.get("note") or ""))
        return success({"stage": key, "card": card,
                        "stage_info": S.STAGE_BY_KEY[key].as_dict()})

    @app.post("/api/projects/{pid}/stage/revise")
    async def api_stage_revise(pid: str, payload: Dict[str, Any] = None):
        from core import stages as S
        payload = payload or {}
        pend = S.pending_gate(pid)
        if not pend:
            raise error_response("现在没有待确认的提案（先用 propose 提一个）", status=400)
        key = str(payload.get("stage") or pend.get("stage")).strip().lower()
        if key not in S.STAGE_BY_KEY:
            raise error_response(f"没有这个阶段：{key}", status=400)
        merged = dict(pend.get("args") or {})
        ch = payload.get("changes")
        if isinstance(ch, dict):
            merged.update(ch)
        merged["project_id"] = pid
        card = S.proposal_text(db, pid, key, merged)
        if isinstance(card, dict) and card.get("ok") is False:
            raise error_response(card.get("error") or "修改失败", status=400)
        S.set_pending(pid, key, merged, note=str(payload.get("note") or ""))
        return success({"stage": key, "card": card,
                        "stage_info": S.STAGE_BY_KEY[key].as_dict()})

    # ★★ 2026-10-20（用户实机「点了确认没反应、发消息也不动」）：
    #   确认这一步原来是**同步**跑在一个 HTTP 请求里的（白膜/剧本几分钟）。
    #   后果两层：
    #     ① 那个请求一直挂着，前端把整张卡 + 发送框都禁用 ⇒ 界面像死了；
    #     ② `core.stages` 里的 `_GATE_LOCK` 在整段执行期间被占着，
    #        于是用户**发消息**时后端要等它 —— 表现为"发了也不动"。
    #   改法：确认请求**立刻返回**（accepted），真正执行放到后台任务；
    #        前端轮询 /stage/job 拿结果。这样界面全程可点、可聊。
    _STAGE_JOBS: Dict[str, Dict[str, Any]] = {}
    _STAGE_TASKS: Dict[str, Any] = {}

    def _task_payload(task: Dict[str, Any]) -> Dict[str, Any]:
        p_ = (task or {}).get("payload")
        if isinstance(p_, dict):
            return p_
        try:
            return json.loads(p_ or "{}")
        except Exception:  # noqa: BLE001
            return {}

    async def _stage_confirm_run(pid: str, key: str, params: Dict[str, Any],
                                 task_id: str = "") -> Dict[str, Any]:
        """执行一个阶段 —— **持久化成 tasks 行**（重启不丢、可查询、可接 SSE）。

        ★★ 2026-10-20 重构（对标 MiniMax Agent 的"任务"模型）：
          之前这类长操作要么卡在一个 HTTP 请求里，要么只活在进程内的字典里
          （重启就丢、没有进度、没法取消）。而本仓**本来就有**任务表
          （`tasks`，含 status/progress/progress_message/cancellable）
          和 `/api/tasks/{tid}/stream` 的 SSE —— 只是一直没被流水线用上。
          现在：确认 → 建一条 task 行 → 后台跑 → 边跑边写 progress_message。
        """
        from core import stages as S
        from core.agent_tools_stages import run_stage as _run_stage
        jk = f"{pid}:{key}"
        job = _STAGE_JOBS.setdefault(jk, {})
        job.update({"state": "running", "stage": key, "task_id": task_id,
                    "started": job.get("started") or time.time()})
        if task_id:
            try:
                db.update_task(task_id, status="running",
                               started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                               progress_message=f"正在执行「{key}」…")
            except Exception:  # noqa: BLE001
                pass
        try:
            res = await _run_stage(db, pid, key, params)
            ok_flag = bool(isinstance(res, dict) and res.get("ok"))
            if ok_flag:
                try:
                    S.consume_pending(pid)
                except Exception:
                    pass
            nxt = ""
            try:
                nxt = str(S.next_stage_key(db, pid) or "")
            except Exception:
                nxt = ""
            out = {
                "ok": ok_flag,
                "stage": key,
                "tool": (res or {}).get("tool") or S.STAGE_BY_KEY[key].tool,
                "error": "" if ok_flag else str((res or {}).get("error") or "未知错误"),
                "summary": (str((res or {}).get("note") or "") if ok_flag else ""),
                "result": res,
                "next": S.STAGE_BY_KEY[nxt].as_dict() if nxt in S.STAGE_BY_KEY else None,
                "pending_gate": S.pending_gate(pid),
            }
            job.update({"state": "done", "ok": ok_flag, "ended": time.time(), "out": out})
            # ★ 2026-10-20：成片后把项目状态标成 completed。
            #   原来永远停在「草稿」——用户出完片回头看列表还以为没干。
            if ok_flag and key in ("compose", "final"):
                try:
                    db.update_project(pid, status="completed")
                except Exception:  # noqa: BLE001
                    pass
            if task_id:
                try:
                    db.update_task(task_id, status="done" if ok_flag else "failed",
                                   progress=1.0, result=out,
                                   error=("" if ok_flag else str(out.get("error") or "")),
                                   finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                                   progress_message=("已完成" if ok_flag else "失败"))
                except Exception:  # noqa: BLE001
                    pass
            return out
        except Exception as e:  # noqa: BLE001
            logger.exception("后台执行阶段 %s 失败：%s", key, e)
            out = {"ok": False, "stage": key, "error": f"{type(e).__name__}: {e}",
                   "summary": "", "result": {}, "next": None}
            job.update({"state": "error", "ok": False, "ended": time.time(), "out": out})
            if task_id:
                try:
                    db.update_task(task_id, status="failed", error=str(e)[:500],
                                   finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                                   progress_message="出错：" + str(e)[:120])
                except Exception:  # noqa: BLE001
                    pass
            return out
        finally:
            _STAGE_TASKS.pop(jk, None)

    @app.post("/api/projects/{pid}/stage/confirm")
    async def api_stage_confirm(pid: str, payload: Dict[str, Any] = None):
        from core import stages as S
        payload = payload or {}
        pend = S.pending_gate(pid)
        key = str(payload.get("stage") or (pend or {}).get("stage") or "").strip().lower()

        # ★★★ 2026-10-20 修的真 bug（实测撞到）：
        #   界面上的卡片是**历史消息**里存下来的，它记的 stage 是**当时**那一步；
        #   等用户真正点它的时候，那一步早就做完了 —— 于是后端回
        #   「剧本这一步已经做完了，再跑一遍会覆盖掉现有内容」。
        #   用户看到的就是"点确认就报错"，流程从这里断死。
        #
        #   修法：这一步如果**已经做完了**、而且**没有待确认的正式提案**
        #   （不是用户显式要重做），就自动改做**当前该做的那一步**。
        #   这正好也是「点确认直接做」该有的行为 ——
        #   用户点的是"下一步"，就该做下一步，而不是复述一个过期的步骤。
        #   （有 pending_gate 时保持原样：那说明用户**明确**提过这一步。）
        if key and key in S.STAGE_BY_KEY and not pend:
            try:
                if S.already_done(db, pid, key):
                    _nxt = S.next_stage_key(db, pid)
                    if _nxt and _nxt != key:
                        logger.info(
                            "stage/confirm：%s 已完成，改为执行当前该做的 %s", key, _nxt)
                        key = _nxt
            except Exception as _e:
                logger.debug("已完成判定失败（按原样执行）: %s", _e)

        if key not in S.STAGE_BY_KEY:
            raise error_response(
                "没有待确认的步骤。正确顺序：先让 AI 提一个提案（propose_step），"
                "确认卡片出现后再点确认。", status=400)
        params = dict((pend or {}).get("args") or {})
        ov = payload.get("overrides")
        if isinstance(ov, dict):
            params.update(ov)
        params["project_id"] = pid
        jk = f"{pid}:{key}"

        # 同一阶段已经在跑 → 不重复执行（重复点=重复花钱）
        if (_STAGE_JOBS.get(jk) or {}).get("state") == "running":
            return success({"accepted": True, "running": True, "stage": key,
                            "ok": True, "summary": "这一步已经在后台执行中，不用重复点",
                            "result": {}, "next": None,
                            "job": {"state": "running",
                                    "started": (_STAGE_JOBS[jk] or {}).get("started")}})

        # 兼容：`sync=true` 保留原来的同步语义（冒烟测试/脚本用）
        if payload.get("sync"):
            return success(await _stage_confirm_run(pid, key, params))

        # ★ 持久化：建一条 tasks 行（重启后仍然查得到）
        task_id = ""
        try:
            task_id = db.create_task("stage", {"project_id": pid, "stage": key})
        except Exception:  # noqa: BLE001
            task_id = ""
        _STAGE_JOBS[jk] = {"state": "running", "stage": key,
                           "started": time.time(), "task_id": task_id}
        _STAGE_TASKS[jk] = asyncio.create_task(
            _stage_confirm_run(pid, key, params, task_id))
        return success({"accepted": True, "running": True, "stage": key,
                        "ok": True, "task_id": task_id,
                        "summary": "已提交到后台执行 —— 你可以继续聊/看别的，完成后这里会更新",
                        "result": {}, "next": None,
                        "job": {"state": "running", "task_id": task_id,
                                "started": _STAGE_JOBS[jk]["started"]}})

    @app.get("/api/projects/{pid}/stage/job")
    async def api_stage_job(pid: str, stage: str = ""):
        """查后台阶段任务的状态（前端轮询用）。"""
        key = str(stage or "").strip().lower()
        jk = f"{pid}:{key}"
        j = dict(_STAGE_JOBS.get(jk) or {})
        if j.get("state") == "running" and jk not in _STAGE_TASKS:
            # 进程里没有活任务了 ⇒ 上次被打断（重启/崩溃）。如实说，别让它永远转圈。
            _tid_old = str(j.get("task_id") or "")
            j = {"state": "interrupted", "stage": key,
                 "error": "上次执行被中止了（软件重启或崩溃）—— 可以重新点一次确认"}
            if _tid_old:
                try:
                    db.update_task(_tid_old, status="failed",
                                   error="interrupted by restart",
                                   progress_message="上次执行被中止")
                except Exception:  # noqa: BLE001
                    pass
        if not j:
            # ★ 内存里没有 → 查持久任务表（重启后仍然能回答"上次那步怎么样了"）
            try:
                for _t in db.list_tasks("running", 100):
                    _pl = _task_payload(_t)
                    if _pl.get("project_id") == pid and _pl.get("stage") == key:
                        # ★★ DB 里 running、但本进程里没有活任务 ⇒ **僵尸**
                        #   （进程重启/崩溃留下的）。如实报 interrupted 并落库，
                        #   否则前端会看着它永远转圈 —— 这正是"界面像死机"的一半真相。
                        _tid_z = str(_t.get("id") or "")
                        try:
                            db.update_task(_tid_z, status="failed",
                                           error="interrupted by restart",
                                           progress_message="上次执行被中止")
                        except Exception:  # noqa: BLE001
                            pass
                        return success({"state": "interrupted", "stage": key,
                                        "task_id": _tid_z,
                                        "error": "上次执行被中止了（软件重启或崩溃）"
                                                 "—— 可以重新点一次确认"})
                for _t in db.list_tasks(None, 60):
                    _pl = _task_payload(_t)
                    if (_pl.get("project_id") == pid and _pl.get("stage") == key
                            and _t.get("status") in ("done", "completed", "failed",
                                                     "cancelled", "error")):
                        _res = _t.get("result")
                        if isinstance(_res, str):
                            try:
                                _res = json.loads(_res)
                            except Exception:  # noqa: BLE001
                                _res = None
                        return success({"state": ("done" if _t.get("status") in
                                                   ("done", "completed")
                                                   else _t.get("status")),
                                        "stage": key, "task_id": _t.get("id"),
                                        "ok": _t.get("status") in ("done", "completed"),
                                        "out": _res,
                                        "error": _t.get("error") or "",
                                        "persisted": True})
            except Exception:  # noqa: BLE001
                pass
            return success({"state": "idle", "stage": key})
        if j.get("started") and not j.get("ended"):
            j["elapsed"] = round(time.time() - float(j["started"]), 1)
        return success(j)


    @app.post("/api/projects/{pid}/stage/skip")
    async def api_stage_skip(pid: str, payload: Dict[str, Any] = None):
        from core import stages as S
        from core.agent_tools_stages import _skips_get, _skips_set, _now
        payload = payload or {}
        key = str(payload.get("stage") or "").strip().lower()
        if key not in S.STAGE_BY_KEY:
            raise error_response(f"没有这个阶段：{key or '(空)'}", status=400)
        skips = await _skips_get(db, pid)
        skips[key] = {"reason": str(payload.get("reason") or ""), "at": _now()}
        await _skips_set(db, pid, skips)
        pend = S.pending_gate(pid)
        if pend and pend.get("stage") == key:
            S.clear_pending(pid)
        nxt = str(S.next_stage_key(db, pid) or "")
        return success({"skipped": key, "title": S.STAGE_BY_KEY[key].title,
                        "next": S.STAGE_BY_KEY[nxt].as_dict() if nxt in S.STAGE_BY_KEY else None})

    # ══════════════════════════════════════════════════════════════
    # v2 sidecar（2026-10-21）：给 DSH 侧注册**真正的后台任务**
    #
    # 只新增路由，不改上面任何一条既有路由的行为。
    # 业务全在 `core/v2api.py`；三条路由只是薄薄的一层 HTTP 壳：
    #   POST /api/v2/run                  立刻 {job_id}，后台走 run_stage
    #   GET  /api/v2/jobs/{id}/events     SSE（先回放事件环，再实时）
    #   GET  /api/v2/stage/{stage}/estimate  预估金额/时长/是否要审批
    # 纪律：执行入口仍是 `core.agent_tools_stages.run_stage`（唯一入口）；
    #       进度读 `core.stages` 证据；金额读 `core.budget` +
    #       `core.shot_segments._estimate_segment_cost_yuan` —— 都不另写一套。
    # ══════════════════════════════════════════════════════════════

    @app.post("/api/v2/run")
    async def api_v2_run(payload: Dict[str, Any] = None):
        """提交一个阶段到后台执行，**立刻**返回 job_id。"""
        from core import v2api as _v2
        payload = payload or {}
        pid = str(payload.get("project_id") or "").strip()
        stage = str(payload.get("stage") or "").strip().lower()
        params = payload.get("params")
        params = dict(params) if isinstance(params, dict) else {}
        # 允许参数直接平铺在 body 上（curl 更顺手）
        for k, v in payload.items():
            if k not in ("project_id", "stage", "params") and v not in (None, ""):
                params.setdefault(k, v)
        try:
            return success(await _v2.start_job(db, pid, stage, params))
        except _v2.V2Error as e:
            raise error_response(str(e), status=400)

    @app.get("/api/v2/jobs/{job_id}")
    async def api_v2_job(job_id: str):
        """job 现状（轮询兜底；主通道是 events）。"""
        from core import v2api as _v2
        try:
            return success(_v2.get_job(db, job_id))
        except _v2.V2Error as e:
            raise error_response(str(e), status=404)

    @app.get("/api/v2/jobs/{job_id}/events")
    async def api_v2_job_events(job_id: str):
        """SSE：逐镜 / 逐资产的进度（进度来自 core.stages 的阶段证据）。"""
        from core import v2api as _v2

        async def _gen():
            try:
                async for ev in _v2.job_events(db, job_id):
                    yield _v2.sse_format(ev)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                yield _v2.sse_format({"event": "error",
                                      "message": f"{type(e).__name__}: {e}"})

        return StreamingResponse(
            _gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache",
                     "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"})

    @app.post("/api/v2/jobs/{job_id}/cancel")
    async def api_v2_job_cancel(job_id: str):
        """取消（DSH 的 ctx.jobs 取消会落到这里）。"""
        from core import v2api as _v2
        return success(_v2.cancel_job(job_id))

    @app.get("/api/v2/stage/{stage}/estimate")
    async def api_v2_estimate(stage: str, project_id: str = "",
                              num_candidates: int = 0,
                              provider: str = "", model_name: str = ""):
        """这一步预计花多少钱 / 多久 / 要不要审批 —— **只读，不花钱**。

        `provider`/`model_name` 可选：用来问"如果按这条通道跑，多少钱"
        （已决定走 token6688 网关时，把 provider=seedance_t6 传进来即可）。
        """
        from core import v2api as _v2
        params: Dict[str, Any] = {}
        if num_candidates and int(num_candidates) > 0:
            params["num_candidates"] = int(num_candidates)
        if str(provider or "").strip():
            params["provider"] = str(provider).strip()
        if str(model_name or "").strip():
            params["model_name"] = str(model_name).strip()
        try:
            return success(_v2.estimate_stage(
                db, project_id, str(stage or "").strip().lower(), params))
        except _v2.V2Error as e:
            raise error_response(str(e), status=400)

    @app.get("/api/projects/{pid}/audit/consistency")
    async def api_audit_consistency(pid: str, max_frames: int = 3):
        """出片后的角色一致性自检（只读、不花钱、不阻断）。"""
        if not db.get_project(pid):
            raise error_response("项目不存在", status=404)
        import asyncio as _aio
        from core.consistency_audit import audit_visual_consistency as _avc
        try:
            # cv2 是 CPU 活，别堵事件循环
            r = await _aio.to_thread(_avc, db, pid, max(1, min(6, int(max_frames))))
        except Exception as e:
            raise error_response(f"自检失败：{type(e).__name__}: {e}", status=500)
        if isinstance(r, dict) and r.get("ok") is False:
            raise error_response(str(r.get("error") or "自检不可用"), status=400)
        return success(r)

    @app.post("/api/projects/{pid}/compose")
    async def start_compose(pid: str, payload: Dict[str, Any] = None):
        """启动一键成片（后台跑，前端轮询 /compose/status 拿进度）"""
        payload = payload or {}
        if not db.get_project(pid):
            raise error_response("项目不存在", status=404)
        if _compose_state.get(pid, {}).get("running"):
            return success({"started": False, "message": "该项目正在合成中，请稍候",
                            "status": _compose_state[pid]})

        settings = db.get_all_settings()
        voice_id = payload.get("voice_id") or "edge:zh-CN-XiaoxiaoNeural"
        # ★★ 2026-10-24 修正（用户原话：「480p 视频配音一次性都做好，
        #   后期合成的时候不要再额外做配音了」）：
        #     480P 草稿 = **配音在这里一次做完**（True）
        #     后期/成片（非 480P） = **不再重新配音**（False，复用草稿阶段的声音）
        #   显式传 with_voice 仍优先。
        #   （上一版我把 480p 写成"默认不配音"，方向反了，已撤回。）
        _res_raw = str(payload.get("target_resolution") or "").strip().lower()
        _stage_raw = str(payload.get("stage") or "").strip().lower()
        _is_draft = _res_raw.startswith("480") or _stage_raw in ("draft", "480p", "preview")
        if "with_voice" in payload:
            with_voice = bool(payload.get("with_voice"))
        elif _is_draft:
            with_voice = True
            logger.info("480P 草稿合成：配音在本步一次性做完（后期不再重复配音）")
        else:
            with_voice = False
            logger.info("后期/成片合成：不再重新配音（复用 480P 阶段已做好的声音）")
        burn = payload.get("burn_subtitles", True)
        bgm = payload.get("bgm_path") or ""
        bgm_vol = float(payload.get("bgm_volume") or 0.25)
        resolution = payload.get("target_resolution") or ""
        # 前端传 base64 上传的 BGM 时，落盘后再用
        bgm_b64 = payload.get("bgm_base64") or ""
        if bgm_b64 and not bgm:
            try:
                import base64 as _b64
                cache = settings.get("cache_dir") or os.path.join(
                    os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
                bdir = os.path.join(cache, "bgm")
                os.makedirs(bdir, exist_ok=True)
                bgm = os.path.join(bdir, f"{pid[:8]}-{int(time.time())}.mp3")
                with open(bgm, "wb") as f:
                    f.write(_b64.b64decode(bgm_b64.split(",")[-1]))
            except Exception as e:
                bgm = ""
                logger.warning("BGM 落盘失败：%s", e)

        state = {"running": True, "stage": "compose", "percent": 0.0,
                 "message": "准备中…", "result": None, "error": "", "started_at": time.time()}
        _compose_state[pid] = state

        async def _worker():
            try:
                from core.compose import compose_project
                res = await compose_project(
                    pid, db=db, settings=settings, voice_id=voice_id,
                    with_voice=bool(with_voice), burn_subtitles=bool(burn),
                    bgm_path=bgm, bgm_volume=bgm_vol,
                    target_resolution=resolution,
                    auto_images=bool(payload.get("auto_images")),
                    image_provider=str(payload.get("image_provider") or "minimax"),
                    image_size=str(payload.get("image_size") or "1280x720"),
                    transition=str(payload.get("transition") or "fade"),
                    transition_sec=float(payload.get("transition_sec") or 0.5),
                    # 跨场景用"硬切"还是"溶解"：取向问题，两种都成立（见
                    # `continuity.pick_transition` 的说明与实测数字）。
                    # **默认硬切**（2026-09-13 用户看过两版成片后定）；
                    # 传 scene_transition="dissolve" 回到 0.9s 溶解。
                    scene_transition=str(payload.get("scene_transition") or "cut"),
                    # 剪掉"片段中间那段真冻结"：默认**关闭**（画面会出现一次跳切，
                    # 是动刀）。实测同一提示词重生成不会让这种停顿消失
                    # （2.92s → 4.42s 反而更长），所以它是内容驱动的，
                    # 管线要么如实报、要么剪掉。
                    cut_stalls=bool(payload.get("cut_stalls")),
                    progress=lambda stage, pct, msg: state.update(
                        {"stage": stage, "percent": round(pct * 100, 1), "message": msg}),
                )
                state["result"] = res.to_dict()
                state["running"] = False
                state["percent"] = 100.0
                if res.ok:
                    state["message"] = f"成片完成：{os.path.basename(res.output_path)}"
                    logger.info("compose OK: %s (%.1fs)", res.output_path, res.duration)
                else:
                    state["error"] = "；".join(res.errors) or "合成失败"
                    state["message"] = state["error"]
            except Exception as e:
                logger.exception("compose worker crashed")
                state["running"] = False
                state["error"] = f"{type(e).__name__}: {e}"
                state["message"] = state["error"]

        asyncio.create_task(_worker())
        return success({"started": True, "voice_id": voice_id,
                        "with_voice": bool(with_voice), "burn_subtitles": bool(burn),
                        "status": {k: v for k, v in state.items() if k != "result"}})

    @app.get("/api/projects/{pid}/compose/status")
    async def compose_status(pid: str):
        """轮询合成进度"""
        st = _compose_state.get(pid)
        if not st:
            return success({"running": False, "percent": 0.0, "message": "尚未开始",
                            "result": None, "error": ""})
        return success({k: v for k, v in st.items() if k != "result"} | {"result": st.get("result")})

    @app.post("/api/projects/{pid}/compose/cancel")
    async def compose_cancel(pid: str):
        st = _compose_state.get(pid)
        if st and st.get("running"):
            st["running"] = False
            st["error"] = "已请求取消"
            st["message"] = "已请求取消（当前片段渲染完成后停止）"
        return success({"cancelled": True})

    # ──────────── 配音 / TTS（借鉴 MPT voice.py）────────────

    # ── 音色克隆 / 音色设计（中转站 /v1/audio/voices）────────────────────
    # ★ 2026-10-23（用户要求）：「音色要保持人物的一致性！」「我给你开魔法」。
    #   实测：multipart 上传样本 → 202 带 task_id → 进度在 /v1/tasks/{task_id}；
    #   **样本必须 10~300 秒**（8 秒会判 invalid_audio_duration）。
    #   拿到 voice_id 后写 `agentai:speech-2.8:<voice_id>` 进 characters.voice_id，
    #   同一角色所有台词就都用这把嗓子。
    @app.post("/api/voice/clone")
    async def voice_clone(file: UploadFile = File(...), name: str = Form("vf-voice"),
                          model: str = Form("speech-2.8")):
        from core.voice import agentai_voice as _av
        settings = db.get_all_settings() or {}
        # ★★ 2026-10-23（用户实测账单后叫停）：
        #   中转站 **voice-clone 每次约 14.89 算力** —— 单次就是全项目耗材的 16%，
        #   比 seedance 一条视频还贵。所以这个能力**默认封死**，要显式开才给调。
        if str(settings.get("voice_clone_enabled") or "").strip().lower() not in (
                "1", "true", "yes", "on"):
            raise error_response(
                "音色克隆默认关闭：实测中转站 **每次约 14.89 算力**"
                "（≈ 本项目全部耗材的 16%，比一条视频还贵）。"
                "确实要用，把设置 voice_clone_enabled 设为 true 再调；"
                "日常配音用 speech-2.8 + 现成音色即可（约 0.1 算力/次）。",
                status=403)
        tmp_dir = os.path.join(str(settings.get("cache_dir") or ""), "voice_clone")
        os.makedirs(tmp_dir, exist_ok=True)
        ext = os.path.splitext(file.filename or "sample.mp3")[1] or ".mp3"
        dst = os.path.join(tmp_dir, "sample" + ext)
        with open(dst, "wb") as f:
            f.write(await file.read())
        r = await _av.clone_voice(dst, name, settings, model=model)
        if not r.get("ok"):
            raise error_response(r.get("reason") or "音色克隆失败", status=500)
        return success({"voice_id": r.get("voice_id"), "task_id": r.get("task_id"),
                        "duration": r.get("duration"),
                        "voice_handle": "agentai:%s:%s" % (model, r.get("voice_id")),
                        "note": ("把 voice_handle 写进角色的 voice_id，"
                                 "这个角色的所有台词就都用这把嗓子")})

    @app.get("/api/voice/cloned")
    async def voice_cloned():
        from core.voice import agentai_voice as _av
        items = await _av.list_cloned(db.get_all_settings() or {})
        return success({"voices": items, "count": len(items)})

    # ── 台词回填（剧本有、分镜没有 → 写回分镜）──────────────────────────
    # ★ 2026-10-23：用户反馈「人物感情不对/没感情」的真病根是
    #   4 个分镜的 layer2_timeline[*].dialogue 全是 null（剧本里明明有对白）。
    #   配音阶段有临时回填，但分镜表里仍是空的 —— 这里做**持久化**回填，
    #   让分镜页看得见、字幕/配音/体检读到同一份。幂等：已有台词的镜头不动。
    @app.post("/api/projects/{pid}/dialogue/backfill")
    async def dialogue_backfill(pid: str):
        from core import dialogue_backfill as _bf
        r = _bf.backfill_project(db, pid)
        if not r.get("ok"):
            raise error_response(r.get("reason") or "台词回填失败", status=400)
        return success(r)

    # ── 唇形同步（云端 · 中转站 kling-lip-sync）────────────────────────
    # ★ 2026-10-23（用户要求）：「要跟视频里面人物的嘴型、唇语同步」「本地太慢了！」
    #   所以走云端，而不是本机跑 Wav2Lip。按次计费，**默认关闭**：
    #     · 设置 lipsync_mode=cloud 才会在合成链路自动跑；
    #     · 也可以直接打这个接口，按需跑一条。
    @app.post("/api/shots/{sid}/lipsync")
    async def shot_lipsync(sid: str):
        from core import lipsync as _ls
        shot = None
        try:
            shot = db.get_shot(sid)
        except Exception:  # noqa: BLE001
            shot = None
        if not shot:
            # 有些版本没有 get_shot：扫项目找（分镜数不多，够快）
            try:
                for _pj in (db.list_projects() or []):
                    for _x in (db.list_shots(_pj.get("id")) or []):
                        if str(_x.get("id")) == str(sid):
                            shot = _x
                            break
                    if shot:
                        break
            except Exception:  # noqa: BLE001
                shot = None
        if not shot:
            raise error_response("找不到这个分镜", status=404)
        settings = db.get_all_settings() or {}
        if not _ls._key(settings):  # noqa: SLF001
            raise error_response("中转站 Key 没配，云端口型同步用不了", status=400)
        out_dir = os.path.join(str(settings.get("output_dir") or ""),
                               str(shot.get("project_id") or ""), "lipsync")
        r = await _ls.sync_shot(db, shot, settings, out_dir)
        if not r.get("ok"):
            raise error_response(r.get("reason") or "唇形同步失败", status=500)
        try:
            db.update_shot(sid, lipsync_path=r.get("path") or "")
        except Exception:  # noqa: BLE001
            pass
        return success(r)

    @app.get("/api/voice/providers")
    async def get_voice_providers():
        """列出所有 TTS provider（前端下拉框按 provider 分组）"""
        settings = db.get_all_settings()
        return success({"providers": await voice_list_providers(settings)})

    @app.get("/api/voice/voices")
    async def get_voice_voices():
        """列出所有可用音色（每个 provider 完整 list_voices）。

        ★ 每条都带上 `provider` / `has_key` / `usable`：
        以前前端拿到的是一张**扁平的 30 项列表**，其中 8 个属于 SiliconFlow，
        而那家没配 Key —— 用户选中它，配音静默失败、片子变成静音，
        界面上只留一句容易被忽略的 warning。这就是"配音音色是摆设"的由来。
        """
        settings = db.get_all_settings()
        raw = await voice_list_voices(settings)
        provs = {p.get("name"): p for p in await voice_list_providers(settings)}
        voices: List[Dict[str, Any]] = []
        for v in raw:
            # ★ list_voices() 返回的是 VoiceInfo dataclass，不是 dict ——
            #   这里必须先转成 dict 再富化，否则 .get() 直接 AttributeError。
            if isinstance(v, dict):
                d = dict(v)
            else:
                d = {"voice_id": getattr(v, "voice_id", ""),
                     "display_name": getattr(v, "display_name", ""),
                     "language": getattr(v, "language", ""),
                     "gender": getattr(v, "gender", ""),
                     "style": getattr(v, "style", ""),
                     "is_builtin": getattr(v, "is_builtin", True)}
            name = str(d.get("voice_id") or "").split(":")[0]
            p = provs.get(name) or {}
            d["provider"] = name
            d["provider_display"] = p.get("display_name") or name
            d["provider_description"] = p.get("description") or ""
            d["requires_api_key"] = bool(p.get("requires_api_key"))
            # 不需要 Key 的（edge / silent）永远可用
            d["has_key"] = bool(p.get("has_api_key")) or not p.get("requires_api_key")
            d["usable"] = bool(d["has_key"])
            if not d["usable"]:
                d["unusable_reason"] = (
                    f"「{d['provider_display']}」还没配 API Key，选它会合成失败。"
                    f"到「设置 → 🔊 语音合成 API Keys」填上即可，"
                    f"或改用免费的 Edge TTS 音色（标着「免费」的那些）。")
            voices.append(d)
        return success({"voices": voices})

    @app.post("/api/voice/synthesize")
    async def synthesize_voice(payload: dict):
        """
        合成配音。

        payload:
          - text: str                # 必填，要合成的文本
          - voice_id: str            # 必填，如 "siliconflow:FunAudioLLM/CosyVoice2-0.5B:alex-Male"
          - output_path: str         # 可选，不传则自动生成在 output_dir/voice/<timestamp>.mp3
          - rate: float              # 倍速，默认 1.0
          - volume: float            # 音量，默认 1.0
        """
        text = (payload.get("text") or "").strip()
        voice_id = (payload.get("voice_id") or "").strip()
        if not text:
            raise error_response("text 不能为空", status=400)
        if not voice_id:
            raise error_response("voice_id 不能为空", status=400)

        settings = db.get_all_settings()
        # 输出路径
        out_path = payload.get("output_path") or ""
        if not out_path:
            output_dir = Path(settings.get("output_dir", "./data/outputs")) / "voice"
            output_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            ext = ".wav" if voice_is_no_voice(voice_id) else ".mp3"
            out_path = str(output_dir / f"{ts}{ext}")

        req = TTSRequest(
            text=text,
            voice_id=voice_id,
            rate=float(payload.get("rate") or 1.0),
            volume=float(payload.get("volume") or 1.0),
            output_path=out_path,
            language=payload.get("language") or "zh-CN",
        )

        result: TTSResult = await voice_synthesize(req, settings)
        if not result.success:
            raise error_response(result.error or "TTS 合成失败", status=500)

        return success({
            "audio_path": result.audio_path,
            "duration_seconds": result.duration_seconds,
            "subtitles": result.subtitles,
            "provider": result.provider,
            "voice_id": result.voice_id,
        })

    @app.post("/api/voice/test")
    async def test_voice_provider(payload: dict = None):
        """测试某个 TTS provider 的 Key 到底能不能用（真合成一小段，约几分钱）。

        用户的原话是"配音音色是摆设"——根因就是配了 Key 也没地方填、
        填了也不知道通不通。这里给一个按钮，几秒出结果，
        失败就把厂商原始报错 + 中文解释一并返回。
        """
        payload = payload or {}
        provider = (payload.get("provider") or "").strip()
        if not provider:
            raise error_response("请指定要测试的语音 provider", status=400)
        settings = db.get_all_settings()
        text = (payload.get("text") or "这是一段语音合成连通性测试。").strip()[:40]

        provs = {p.get("name"): p for p in await voice_list_providers(settings)}
        meta = provs.get(provider)
        if not meta:
            raise error_response(
                f"未知的语音 provider：{provider}（可选：{', '.join(provs)}）", status=400)

        # 取该 provider 的第一个音色来试
        voices = []
        try:
            for v in await voice_list_voices(settings):
                vid = v.get("voice_id") if isinstance(v, dict) else getattr(v, "voice_id", "")
                if str(vid).startswith(f"{provider}:"):
                    voices.append(vid)
        except Exception:
            pass
        voice_id = (payload.get("voice_id") or "").strip() or (voices[0] if voices else "")

        if meta.get("requires_api_key") and not meta.get("has_api_key"):
            return success({
                "ok": False, "provider": provider,
                "error": f"「{meta.get('display_name')}」还没有配置 API Key。",
                "hint": "到「设置 → 🔊 语音合成 API Keys」填上对应厂商的 Key 再测。",
            })
        if not voice_id:
            return success({"ok": False, "provider": provider,
                            "error": "该 provider 没有可用音色", "hint": ""})

        import tempfile
        ext = ".wav" if voice_is_no_voice(voice_id) else ".mp3"
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tmp.close()
        t0 = time.time()
        result: TTSResult = await voice_synthesize(
            TTSRequest(text=text, voice_id=voice_id, output_path=tmp.name), settings)
        elapsed = round(time.time() - t0, 1)
        if not result.success:
            return success({
                "ok": False, "provider": provider, "voice_id": voice_id,
                "elapsed": elapsed,
                "error": result.error or "合成失败",
                "hint": "常见原因：Key 填错 / Key 无效 / 账户余额不足 / "
                        "该音色需要实名认证 / 网络无法访问该厂商接口。",
            })
        size = os.path.getsize(result.audio_path) if os.path.exists(result.audio_path) else 0
        chars = len(text)
        return success({
            "ok": True, "provider": provider, "voice_id": voice_id,
            "elapsed": elapsed, "size_bytes": size, "chars": chars,
            "duration_seconds": result.duration_seconds,
            "audio_url": f"/api/media?path={quote(str(result.audio_path))}",
            "note": f"合成成功（{chars} 字 → {size} 字节）。这个音色可以用。",
        })

    @app.get("/api/voice/preview")
    async def preview_voice_get(voice_id: str, text: str = "你好，这是音色试听。",
                               rate: float = 1.0):
        """试听的 GET 版本 —— 前端可以直接把它塞进 <audio src> 播放，
        不用把二进制转成 blob URL。逻辑与 POST 版完全一致。"""
        return await _do_voice_preview(voice_id, text, rate)

    @app.post("/api/voice/preview")
    async def preview_voice(payload: dict):
        """试听一段短文本（约 8 字以内），生成临时音频供前端播放"""
        payload = payload or {}
        return await _do_voice_preview(payload.get("voice_id") or "",
                                       payload.get("text") or "",
                                       float(payload.get("rate") or 1.0))

    async def _do_voice_preview(voice_id: str, text: str, rate: float = 1.0):
        text = (text or "").strip()
        voice_id = (voice_id or "").strip()
        if not text or not voice_id:
            raise error_response("text / voice_id 不能为空", status=400)

        settings = db.get_all_settings()
        # 试听文本截短，避免长音频
        if len(text) > 30:
            text = text[:30]

        # 临时输出
        import tempfile
        tmp = tempfile.NamedTemporaryFile(
            suffix=".mp3" if not voice_is_no_voice(voice_id) else ".wav",
            delete=False,
        )
        tmp.close()

        req = TTSRequest(
            text=text,
            voice_id=voice_id,
            output_path=tmp.name,
            rate=max(0.25, min(4.0, float(rate or 1.0))),
            volume=1.0,
        )
        result: TTSResult = await voice_synthesize(req, settings)
        if not result.success:
            raise error_response(result.error or "试听合成失败", status=500)

        # 直接返回音频文件
        from fastapi.responses import FileResponse
        return FileResponse(
            result.audio_path,
            media_type="audio/mpeg" if result.audio_path.endswith(".mp3") else "audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    # ──────────── 文件管理 ────────────

    @app.get("/api/files/{pid}/{path:path}")
    async def get_file(pid: str, path: str):
        """获取项目内文件（用于前端显示视频/图片）"""
        settings = db.get_all_settings()
        base = Path(settings.get("output_dir", "./data/outputs")) / pid
        file_path = base / path
        if not file_path.exists():
            raise error_response(f"File not found: {file_path}", status=404)
        return FileResponse(file_path)

    # ──────────── 启动事件 ────────────

    @app.on_event("startup")
    async def on_startup():
        # ★★ 2026-10-20：**启动时清理僵尸任务**。
        #   进程被杀/崩溃时，库里那些 `running` 的任务**永远不会自己结束**。
        #   之前只有"被查询时才判死"，于是界面上一直转圈、
        #   而且"同一阶段已在跑"的判断会被它误导。
        #   这里在启动时一次性收拾：标成 failed + 写清原因。
        try:
            _z = 0
            for _t in db.list_tasks("running", 500):
                try:
                    db.update_task(_t.get("id"), status="failed",
                                   error="interrupted by restart",
                                   progress_message="上次执行被中止（软件重启）")
                    _z += 1
                except Exception:  # noqa: BLE001
                    pass
            if _z:
                logger.info("启动清理：%d 个僵尸任务已标为中断", _z)
        except Exception as _e:  # noqa: BLE001
            logger.warning("启动清理僵尸任务失败：%s", _e)
        logger.info("VideoForge started")
        logger.info(f"  DB: {db.db_path}")
        logger.info(f"  Providers: {len(list_providers())}")
        settings = db.get_all_settings()
        ffmpeg_path = settings.get("ffmpeg_path", "ffmpeg")
        logger.info(f"  FFmpeg: {'OK' if check_ffmpeg(ffmpeg_path) else 'NOT FOUND'} ({ffmpeg_path})")

    @app.on_event("shutdown")
    async def on_shutdown():
        await queue.shutdown()

    return app


# ──────────── 工具 ────────────

def _resolve_frontend_dir() -> Path:
    """定位前端目录（开发 / PyInstaller 打包多种布局兼容）"""
    here = Path(__file__).resolve().parent
    meipass = getattr(sys, "_MEIPASS", None)
    exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None
    candidates = [
        here.parent / "frontend",                          # 开发：backend/../frontend
        here / "frontend",                                 # backend/frontend
        Path(meipass) / "frontend" if meipass else None,   # 打包：_MEIPASS/frontend
        exe_dir / "frontend" if exe_dir else None,         # exe 同级
        exe_dir / "_internal" / "frontend" if exe_dir else None,
        here.parent / "_internal" / "frontend",
    ]
    for c in candidates:
        try:
            if c and c.exists() and (c / "index.html").exists():
                return c
        except Exception:
            continue
    return here.parent / "frontend"


def _make_llm(settings: Dict[str, Any]) -> LLMClient:
    """按设置构造 LLM 客户端：**走统一解析器**，不再精确匹配 Key。

    2026-10-25 修：原实现是 `llm_keys.get(provider)` 精确匹配 + 无条件透传全局
    `llm_base_url`。实测两个后果：
      · 库里存的是 "MiniMax"（大写），provider 是 "minimax" → 取不到 Key；
      · 切到 anthropic / agentai 这类"Key 在别的桶、或与视频同网关"的平台时
        同样取不到，用户会以为"配了却没用"。
    `LLMClient.from_settings()` 已统一处理：大小写、key_field 桶、同网关兄弟 Key、
    环境变量、enable_llm_local / ollama_url，以及"过期全局 base_url 不要污染新平台"。
    """
    try:
        return LLMClient.from_settings(settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLMClient.from_settings 失败，回退旧构造：%s", exc)
        provider = settings.get("llm_provider") or settings.get("remote_llm_provider") or "deepseek"
        llm_keys = settings.get("llm_api_keys") or {}
        model = settings.get("llm_model") or settings.get("remote_llm_model") or "deepseek-chat"
        return LLMClient(provider=provider, api_key=llm_keys.get(provider) or "",
                         model=model, base_url=str(settings.get("llm_base_url") or ""))

# ──────────── 入口 ────────────

# 创建默认 app 实例（uvicorn 入口）
app = create_app()


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("VIDEOFORGE_HOST", "127.0.0.1")
    port = int(os.environ.get("VIDEOFORGE_PORT", "8765"))

    print(f"VideoForge starting on http://{host}:{port}")
    print(f"  Health: http://{host}:{port}/api/health")
    print(f"  Docs:   http://{host}:{port}/docs")

    uvicorn.run(app, host=host, port=port, log_level="info")
