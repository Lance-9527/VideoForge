# -*- coding: utf-8 -*-
r"""产品元信息与版权/许可声明（**单一出处**）。

用户 2026-09-14：
    「对了，假如我要分享怎么证明版权是我的。。。我不能做大好人」

版权保护是三层，这个模块负责**第一层：署名与许可声明**（看得见、带得走）：
  ① 署名与协议（本文件 + `packaging/版权声明与许可协议.txt` + exe 版本资源）；
  ② 法律登记（软著登记 / 可信时间戳 —— 见 vault 里的清单，与代码无关）；
  ③ 分发留痕（每版 SHA256 清单，`packaging/make_release_manifest.py`）。
第三层"技术限制"（试用版水印）在 `core/trial.py` + `core/watermark.py`，
它只是**礼貌门槛**，真正的保护在前两层。

★ 要改成你自己的名字/公司：只改下面 4 个常量（HOLDER / YEAR / LICENSE_TEXT / CONTACT），
  再重打包即可；界面「关于」页、`/api/about` 会一起跟着变。
"""
from __future__ import annotations

APP_NAME = "VideoForge"
APP_TITLE = "VideoForge · 私人 AI 短片 Agent"
APP_VERSION = "2.1.0"

# ↓↓↓ 署名：改成你的真名或公司名（对外分享时这一条就是"作者是谁"）
COPYRIGHT_HOLDER = "VideoForge"
COPYRIGHT_YEAR = "2026"
CONTACT = "（把你的联系方式写在这里：邮箱 / 微信 / 主页）"

LICENSE_TEXT = (
    "专有软件许可（Proprietary License）。允许：个人试用、学习、评测，以及在本机制作"
    "你自己的作品。禁止：转售、出租、二次分发（含打包进你自己的产品或课程）、"
    "反编译/反汇编或修改后去除署名与水印标识。"
    "商用（含公司/团队内部使用、对外承接业务）需另行取得书面授权。"
    "本软件按“现状”提供，不附带任何明示或暗示担保。"
)

# 第三方组件：**必须如实列举**（这是许可义务，也是"成熟软件"该有的东西）。
# 打包版把它们静态打进 exe/`_internal`，所以要把出处与许可写清楚。
THIRD_PARTY = [
    {"name": "FFmpeg", "license": "LGPL-2.1-or-later / GPL-2.0-or-later（取决于构建）",
     "note": "视频拼接/转场/编码/字幕/混音。以独立可执行文件形式随包分发（imageio-ffmpeg 构建）。"},
    {"name": "PyInstaller", "license": "GPL-2.0-or-later，附“打包产物不受 GPL 传染”例外条款",
     "note": "把 Python 程序打成 exe。"},
    {"name": "Python", "license": "PSF License",
     "note": "运行时。"},
    {"name": "FastAPI / Starlette / Uvicorn / Pydantic", "license": "MIT / BSD-3-Clause",
     "note": "本地后端接口。"},
    {"name": "pywebview + Microsoft Edge WebView2", "license": "BSD-3-Clause / 微软专有",
     "note": "桌面窗口（缺 WebView2 时自动降级为默认浏览器）。"},
    {"name": "Pillow", "license": "MIT-CMU (HPND)",
     "note": "图片处理、文字卡、水印绘制。"},
    {"name": "NumPy", "license": "BSD-3-Clause", "note": "画面分析（接缝/连贯度测量）。"},
    {"name": "edge-tts / 各厂商 API SDK", "license": "各自许可（见其项目页）",
     "note": "免费 Edge 音色与付费模型的调用；**API Key 由使用者自己提供**。"},
]


def info() -> dict:
    """给界面「关于」页与 `/api/about` 用的结构化信息。"""
    return {
        "product": APP_TITLE,
        "name": APP_NAME,
        "version": APP_VERSION,
        "copyright": f"© {COPYRIGHT_YEAR} {COPYRIGHT_HOLDER}，保留所有权利。",
        "holder": COPYRIGHT_HOLDER,
        "contact": CONTACT,
        "license": LICENSE_TEXT,
        "third_party": THIRD_PARTY,
        "notice": ("如需商用或去除试用版水印，请联系作者取得正式版授权。"
                   "本程序的署名与许可信息可通过界面「设置 → 关于 / 版权」查看。"),
    }
