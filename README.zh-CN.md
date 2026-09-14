# VideoForge

> 个人 AI 短片 Agent · Windows 桌面 · 一句话想法 → 5 秒或 30 分钟短片成片

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078d4.svg)](https://www.microsoft.com/windows)
[![Tests: 80+](https://img.shields.io/badge/tests-80%2B-brightgreen.svg)](tests/)

---

## 这是什么？

VideoForge 是一个 Windows 桌面 AI 短片 Agent，把你的一句话想法一键变成短片：剧本、角色、场景、配音、视频分镜、字幕、BGM、合成，全部由一个 FastAPI + pywebview 应用搞定。

它是 **11 个视频生成 API + 5 个 TTS 提供商 + 多家 LLM + 本地 FFmpeg** 的**集成层**。它对模型产出做"结构强约束"，所以出来的是真的片子，不是幻灯片。

最初是个人做中文 AI 短片的工具，现在开源作为"有自己主张的端到端视频 Agent"的参考实现。

## ✨ 特性

### 多 provider 是设计核心
- **11 个视频模型适配器**：CogVideoX / Hailuo / Jimeng / Kling / Luma / MiniMax / Pika / Runway / Sora / WanX / Seedance 2.0 / 2.0-fast / 2.5
- **5 个 TTS 提供商**：Edge TTS（无需 key）/ SiliconFlow / MiniMax / DashScope / Silent（占位）
- **多家 LLM**：MiniMax / DeepSeek / OpenAI 兼容 / Qwen / GLM / Kimi / Doubao / Ollama（本地）
- 按项目切换 provider，不用重写提示词

### 生产级流水线
- 剧本生成 **强制总时长**（告别"我要 5 秒给了 60 秒"）
- 13 维 **连续性矩阵**（镜头间：镜头 / 光线 / 角色身份 / …）
- 按角色 **音色选角**（年龄段、性别、气质），不再"8 个固定 NPC 走到底"
- **一键配音**整片（**不重生成视频**，零视频成本）
- **参考图完整性**：`MISSING_REFERENCE` + `MENTIONED_BUT_NOT_LINKED` 都是硬错，不是静默兜底
- 厂商参数严格按官方契约（如 `ratio: "16:9"` 不是 `"16x9"`、顶层 body 字段、三选一互斥模式）

### 真实失败码，不是"重试一下"
24 个文档化的失败码（`F-ASPECT-MISMATCH`、`F-REF-MISSING`、…），按责任层归类（资产 / 契约 / 提示词 / 适配器 / 随机性 / 后期），每条配"先检查 / 最小修复"。

### Windows 硬件友好
- 尊重 NVIDIA TDR 阈值（FFmpeg 路径缓存，避免 200Hz spawn 风暴 → 屏幕抽搐）
- FFmpeg 自带（imageio-ffmpeg，无需单独装）
- pywebview 桌面窗口（无需开浏览器）；WebView2 缺失时自动降级到默认浏览器

## 🚀 快速开始

### 环境
- Windows 10 1809（build 17763）或更新
- Python 3.11+（已在 3.14.5 验证）
- ~500MB 磁盘装代码，几 GB 装产出视频

### 安装（开发模式）

```bash
git clone https://github.com/your-username/VideoForge.git
cd VideoForge
pip install -r requirements.txt
```

### 运行

```bash
# 中文友好方式（Windows）
启动.bat

# 或
python launcher.py

# 或纯浏览器模式（不开桌面窗口）
python launcher.py --browser
```

应用启动后开桌面窗口 `http://127.0.0.1:<port>/`（默认端口自动分配，`--port 8899` 可固定）。

### 配置 API Key

1. 打开应用 → **设置** 页
2. 填入各 provider 的 API key（加密存本地 SQLite）
3. 或复制 `.env.example` 到 `.env` 设环境变量（命令行/开发用）

**启动不需要任何 key**。只在用到对应 provider 时才需要。

## 🏗️ 架构

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   前端       │  │  FastAPI     │  │  LLM         │
│  (HTML/JS)   │◀─│  backend/    │──│  MiniMax /   │
│  pywebview   │  │  main.py     │  │  DeepSeek /  │
└──────────────┘  └──────┬───────┘  │  Qwen / ...  │
                         │          └──────────────┘
              ┌──────────┼──────────┐
              ▼          ▼          ▼
       ┌──────────┐ ┌────────┐ ┌──────────┐
       │  视频    │ │  TTS   │ │  图像    │
       │ 11 适配器│ │ 5 提供 │ │ MiniMax/ │
       │ Seedance │ │ Edge / │ │ WanX /   │
       │ Hailuo … │ │ SiF    │ │ OpenAI   │
       └────┬─────┘ └────┬───┘ └────┬─────┘
            ▼            ▼          ▼
       ┌─────────────────────────────────┐
       │  core/postprocess (FFmpeg)      │
       │  拼接 / 混流 / 烧字幕           │
       │  导出画幅 / 混 BGM              │
       └────────────┬────────────────────┘
                    ▼
              ┌───────────┐
              │ outputs/  │  ← 项目成片
              └───────────┘
```

完整说明见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 📚 文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 模块布局、请求流、数据模型
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — 开发环境、调试、常见坑
- [`docs/PROVIDERS.md`](docs/PROVIDERS.md) — 各 provider 笔记（能力、费用、坑）
- [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) — **还没做**的（stock视频、audio-cues 表、…）

## 🧪 测试

测试套件用**真实数据驱动**。很多测试是从开发期发现的真实 bug 反推出来的。

```bash
# 跑一个测试
python tests/test_ref_integrity.py

# 跑整个套件
for f in tests/test_*.py; do python "$f" || echo "FAIL: $f"; done
```

`tests/test_*.py` 共 80+ 个，每个结尾打 `PASS N   FAIL M`。

## 🛠️ 开发

```bash
# Python 环境
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 后端 + 热重载
cd backend
uvicorn main:app --reload --port 8899

# 另开一个终端跑 launcher（窗口模式）
python launcher.py --port 8899
```

## 📦 打包（PyInstaller）

```bash
pip install pyinstaller pyinstaller-hooks-contrib
pyinstaller --clean --noconfirm VideoForge.spec
# 产物：dist/VideoForge.exe（Windows 便携版，约 24MB）
```

完整开发风格见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 🗺️ 路线图

**还没做**的（下一阶段）：

- **素材获取**（Pexels / Pixabay）—— 现在唯一的出画路径是付费文生视频
- **字幕对齐**（faster-whisper）—— 现在 action 时间轴 → SRT（粗糙）
- **资产状态机**（proposed → reference_ready → approved → deprecated）
- **五元组记录表**（prompt_id+version / batch_id / generation_id / selection_id / iteration_id）
- **9 维选片评分** + 硬 gate
- **连续性矩阵 13 维** + 自动 `continuity_out == next.continuity_in` 比对
- **audio-cues 表**（7 类声音 + 混音优先级 + 连续性键）
- **时长预算表**（读清状态 / 完整动作 / 冲击落定）
- **内置 BGM 曲库**（现在 BGM 用户自带）
- **跨平台发布**（抖音 / B站 / YouTube 上传）
- **单实例锁 + 崩溃恢复队列**（现在队列在内存）
- **生成前门**（资产 approved + prompt approved + 预算授权）

完整列表 + 理由见 `docs/LIMITATIONS.md`。

## 🤝 贡献

看 [CONTRIBUTING.md](CONTRIBUTING.md)。简版：

1. Fork → feature 分支 → 写代码 + `tests/test_*.py`
2. 跑测试：`python tests/test_your_feature.py`
3. 按 PR 模板提 PR

## 🔐 安全

发现漏洞**别**开公开 issue，看 [SECURITY.md](SECURITY.md)。

默认安全姿态：
- **不硬编码 API key**。所有凭据从环境变量/运行时设置读。
- **无遥测**。出站 HTTP 只到你配置的 provider。
- **本地优先**。项目数据在 `%LOCALAPPDATA%\VideoForge\data`。除你发起的 provider API 调用外不发给任何第三方。

## 📜 License

[MIT](LICENSE) — Copyright (c) 2026 Lance (VideoForge Authors)

## 🙏 致谢

- **[Hell-Grind-AIGC-Skill](https://github.com/renmu2017/Hell-Grind-AIGC-Skill)** —— 从 95 分钟 AI 故事片《Hell Grind》抽取的方法论；我们的 13 份参考文档（连续性矩阵、失败码、audio-cues、时长预算表、…）的来源
- **MoneyPrinterTurbo (MPT)** —— 早期借鉴的 FastAPI + adapter 模式
- **Pavo AI** —— SQL/Pydantic schema 模式（雪花 ID → UUID 解决 JS 精度）
- **所有模型 provider** —— 没有它们的 API 这就是个幻灯片生成器

---

> Made with [Proma](https://proma.cool) · [GitHub](https://github.com/proma-ai/Proma)