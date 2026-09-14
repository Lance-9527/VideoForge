# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('frontend', 'frontend')]
binaries = []
hiddenimports = [
    'main', 'models', 'api.chat',
    'core.db', 'core.llm', 'core.llm_robust', 'core.orchestrator', 'core.task_queue',
    'core.ffmpeg_manager', 'core.postprocess', 'core.imagegen', 'core.compose',
    'core.proc', 'core.assets', 'core.docimport', 'core.videoprompt', 'core.model_catalog',
    'core.pipeline',
    # 这一轮新增的「质量层」模块：台词 / 分段规划 / 衔接 / 配音选角 / 成片装配 / 字幕
    # ★ 少一个就会在运行时 ImportError —— 而这些模块都在关键路径上（出成片才会走到）
    'core.dialogue', 'core.shotplan', 'core.continuity',
    'core.voicecast', 'core.assemble', 'core.subtitle', 'core.videoref',
    # 接缝处理（画幅归一化 / 转场摊薄）与生成条件一致性。
    # ★ 这两个是**在函数体里**用 `from core import seamfix` 惰性导入的，
    #   静态分析不一定跟得进去 —— 而它们都在"出成片"的关键路径上。
    #   历史上已经栽过一次同类跟头（打包版少了 2 家 TTS，
    #   原因是回退清单漏了新模块），所以这里显式列出来。
    'core.seamfix', 'core.consistency', 'core.aspect', 'core.duraledger',
    # 配音韵律与提示词审计：同样是**在函数体里**惰性导入的
    # （`synthesize_line` 里 `from core.prosody import plan_prosody`、
    #   `build_video_prompt` 里 `from core import promptaudit`），
    # 两者都在"出成片 / 提交付费生成"的关键路径上，必须显式列出来。
    # ★ 漏了不会在打包时报错，只会在用户点"生成/出片"时才 ImportError。
    'core.prosody', 'core.promptaudit',
    'core.adapters',
    'core.adapters.kling', 'core.adapters.wanx', 'core.adapters.jimeng',
    'core.adapters.hailuo', 'core.adapters.runway', 'core.adapters.sora',
    'core.adapters.pika', 'core.adapters.luma', 'core.adapters.minimax',
    'core.adapters.seedance', 'core.adapters.cogvideox',
    # TTS / 配音（一键成片依赖，少一个就会在运行时 ImportError）
    'core.voice', 'core.voice.base', 'core.voice.dispatcher',
    'core.voice.edge_tts', 'core.voice.siliconflow_tts',
    'core.voice.minimax_tts', 'core.voice.dashscope_tts', 'core.voice.silent',
    'edge_tts', 'edge_tts.communicate', 'edge_tts.submaker',
    'pydub', 'loguru',
    # 剧本导入（文档解析）
    'pypdf', 'docx', 'fitz',
    'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on',
    'imageio_ffmpeg',
]
tmp_ret = collect_all('imageio_ffmpeg')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# edge-tts 自带数据文件（voices 列表等），必须一并收集
tmp_ret = collect_all('edge_tts')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['launcher.py'],
    pathex=['backend'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'tkinter', '_tkinter', 'matplotlib', 'IPython', 'pytest', 'notebook'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VideoForge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VideoForge',
)
