# VideoForge（dsh 基座版）使用说明

> 这份文档说的是**装好之后怎么用**，以及**哪些已经真机验证过、哪些还没有**。
> 版本：`0.2.1-rc.2`（构建日期 2026-10-21）。

---

## 1. 安装与启动

两种形态，任选其一：

| 形态 | 路径 | 说明 |
|---|---|---|
| 安装版 | `videoforge-0.2.1-rc.2-win-x64-unsigned.exe` | 双击安装。开始菜单/桌面快捷方式会自动带上 `--user-data-dir`（Chromium 缓存必须，否则秒退） |
| 免安装 | `win-unpacked\VideoForge.cmd` | **双击这个 .cmd**，不要直接双击 `VideoForge.exe`（裸 exe 因缓存目录权限会闪退） |

首次启动会做三件事（都只做一次）：

1. 建独立数据目录 `%LOCALAPPDATA%\VideoForge\`（`electron/` = 浏览器数据，`dsh/` = 会话与设置）
2. 把你的模型凭证与设置种进 `dsh/`（见第 6 节；你自己配过的凭证不会被覆盖）
3. 拉起 Python 视频服务（`resources\videoforge-sidecar\VideoForgeService.exe`，端口 8765）

窗口标题是 **VideoForge**；左栏底部输入框上方有一行 `● VideoForge 后端已连接 http://127.0.0.1:8765`。
**这一行是唯一需要你确认的启动信号**——绿点 + 已连接，说明视频服务活着；红点说明它没起来，点「重试」。

---

## 2. 界面

```
左栏（三段）                     主区（对话）
──────────────────────          ─────────────────────────────
◤ VideoForge                    当前会话标题            ⚙
  ＋ 新会话
                                你：做个 12 秒雨夜短片
  会话                           [阶段卡片] ← 九步都在这里
   · <历史会话>
                                助手回复（含媒体）
  工作区
   AI 对话 ●
   资产
   后期
                                 ┌─ 输入框 ─────────────┐
  项目                           │ ● VideoForge 后端已连接 │
   · 雨夜便利店                  │ 说个想法，我帮你做成片子 │
   · （你的真实项目）            └──────────────────────┘
```

- 三段左栏由 **DSH 的会话/工作区服务**驱动，不是自己维护的假列表：多会话、新建、切换都真的可用。
- 「项目」段直接读 Python 服务的 `/api/projects`（和旧版 VideoForge 同一个库）。

---

## 3. 做一条片子：九步

对话里说一句话就行，比如：**「做个 12 秒的雨夜便利店短片，日系冷调」**。
之后每一轮你只需要看卡片、点按钮。

| 步 | 卡片上叫什么 | 谁干活 | 花钱 |
|---|---|---|---|
| 1 | 立项 | 建项目 | 免费 |
| 2 | 剧本 | LLM 生成分场/台词/时长 | 便宜（0 元档） |
| 3 | 角色 / 场景 | LLM + 资产库 | 便宜 |
| 4 | 定妆照 / 场景图 | 图像模型 | **要审批** |
| 5 | 分镜表 | LLM 出镜头表 | 免费 |
| 6 | 白膜预演 | 程序化 3D 白模（Blender） | 免费 |
| 7 | 480P 小样 | 视频模型（便宜档） | **要审批** |
| 8 | 1080P 成片 | 视频模型（高档） | **要审批（最贵）** |
| 9 | 后期成片 | FFmpeg 拼接/字幕/配音 | 免费 |

**每张卡片上有三个按钮**：

- `✅ 确认后开始` —— 同意这一步（花钱的步还会再弹一次**原生审批**，两个都点才算批准）
- `✏️ 我要改一下` —— 会发一句话给 agent，然后你接着说怎么改（卡片下面还有一排常用改法 chip，点一下就行）
- `⏭ 这步跳过` —— 跳过并记住原因

进度卡会显示 `3/5 · 正在渲染第 3 镜`，可以取消。**所有长任务都是后台作业**：调用后立刻返回 `job_id`，你可以继续说话，关掉对话页也不会杀掉它。

图片和成片直接在对话里：

- 图片：缩略图，点开看大图
- 成片：`▶ 视频`，点开在灯箱里播放；底层地址是 `http://127.0.0.1:8765/api/projects/<项目id>/final`

---

## 4. 花钱的规则（硬规则，不是提示）

1. `looks / draft / final` 三步**一定**先说清"要花钱"，金额来自后端估算。
2. 后端没给价目表时显示 **「金额：待确认」 + 下界**，**绝不编数字**、绝不兜 0。历史数据里的 `¥0.00` 是旧口径，新卡片已经不这么显示了。
3. 审批是 **DSH 原生审批卡**（允许一次 / 拒绝）。拒绝后不会重试同一件事。
4. 执行中可以取消，取消会真的停掉 Python 侧的活（不是只停前端）。

### 4.1 模型手里有哪些工具（刻意的限制）

VideoForge 只给模型 **vf_\* 那套做视频的工具** + 后台任务控制（`job_kill` / `job_list`）。
**shell（bash/pwsh）、文件读写、glob/grep、联网抓取、子代理、工作流全部关闭**（见
`packages/video/vf-video/cordis.patch.yml` 第 ⑦ 段）。

原因不是洁癖，是实测：把这些工具开着，模型第一轮会先去 `ls`、`glob **/*`、试 PowerShell，
而打包环境里 `pwsh` 起不来（`0xC0000142` DLL 初始化失败），于是**一步都进不了流水线**，
用户看到的就是"软件坏了"。关掉之后它只能按 SOP 直接进 vf_project / vf_script。

---

## 5. 已知限制（诚实的部分）

- **它是 unsigned 包**：Windows 会弹 SmartScreen「未知发布者」，点「更多信息 → 仍要运行」。
- **首次启动会慢**（10–30 秒）：要先解包 Python 载荷 + 起 uvicorn。
- **一次只能跑一条流水线**：端口固定 8765，同机开两个实例会抢端口（第二个会提示视频服务未启动）。
- **视频模型的质量取决于你在旧版里配的那套 key/模型**；这一步没有重做，沿用 `backend/data` 里的 provider 配置。
- 白膜预演是**程序化**白模（按分镜表算机位与时长），不是写实 3D；它的用途是核对节奏与构图，不是成片。
- 「资产 / 后期」两个工作区 tab 目前只切换左栏高亮，主区视图尚未接（下一步）。
- **Python 侧的 LLM 供应商要从「设置」里配对**（见 6.1）。这台机器已经配好；换新机器要重配。

### 5.1 这台机器上的一次真实故障与修法（留档）

现象：点「剧本」，卡片正常、job 正常启动，但 0 秒后失败，错误正文是空的（`生成剧本失败：`）。

排查链（都在本机实测）：
1. 直连网关 `https://agentai.token6688.com/v1/chat/completions` → **200**，key 有效；
2. 但 `core/orchestrator.py` 构造 `LLMClient` 时把 **base_url 传成了空串** →
   `resolve_endpoint()` 回落到 `https://api.openai.com` → 本机连不通 → `httpx.ConnectTimeout`（异常 message 为空，所以界面只显示一个冒号）；
3. 把 sidecar 设置改成 `llm_provider=deepseek / llm_model=deepseek-chat / llm_api_keys.deepseek=<官方 key>`
   （`PUT /api/settings`，已写入 `%LOCALAPPDATA%\VideoForge\data\videoforge.db`）→ 剧本 0.7 秒返回，链路继续往下走通。

⇒ 教训：**空错误正文的失败先怀疑"网络/供应商"，不要怀疑业务逻辑**；并且 `LLMClient` 的 base_url 必须显式给。

---

## 6. 数据与配置在哪（升级/迁移必读）| 内容 | 位置 |
|---|---|
| 业务数据（项目/分镜/成片） | `%LOCALAPPDATA%\VideoForge\data\`（`videoforge.db` + `cache/` + `outputs/`） |
| 会话记录、设置 | `%LOCALAPPDATA%\VideoForge\dsh\`（`sessions/`、`settings.yaml`、`.credentials.yaml`） |
| Chromium 数据 | `%LOCALAPPDATA%\VideoForge\electron\` |
| 视频服务载荷 | 安装目录 `resources\videoforge-sidecar\` |

**不会碰**你的 `C:\Users\<你>\.dsh\`（那是你 DSH 自己的目录）。

首次启动的种子文件在 `resources\videoforge-seed\`：`settings.yaml`（模型目录 + 暗色主题 + 对话视图）
和 `credentials.yaml`（`refs:` 里的 API key）。规则：

- 目标**不存在** → 写入
- 目标是自动生成的空壳（没有 `refs:` / 没有 `llm-*` 记录）→ 备份成 `.bak-<时间戳>` 后写入
- 目标是你自己配过的 → **跳过，绝不覆盖**

---

## 7. 坏了怎么办

| 症状 | 先看 |
|---|---|
| 双击 exe 闪退 | 用 `VideoForge.cmd` 启动，或确认快捷方式带 `--user-data-dir=%LOCALAPPDATA%\VideoForge\electron` |
| 底部是红点「连不上后端」 | 点「重试」；还不行看 `%LOCALAPPDATA%\VideoForge\dsh\logs\` |
| 卡片不显示 / 只显示「已完成 用时 3 秒」 | 设置里把「对话记录视图」改成 **verbose（详细）**；这版默认已经设好了 |
| 模型选择器是空的 | `%LOCALAPPDATA%\VideoForge\dsh\.credentials.yaml` 里缺 `refs:`，把 `resources\videoforge-seed\credentials.yaml` 复制过去 |
| 提示端口被占 | 你旧版 VideoForge 还开着，关掉它 |

---

## 8. 这次改了什么（给将来维护的人）

- 底座是 **DSH**（`deepseek-harness` 0.2.1-rc.2），**core 未改**，只加了三个包 + 一个 patch 层：
  - `packages/video/vf-video` —— 9 个阶段工具 + 花钱步原生审批 + 系统提示里的 9 步 SOP
  - `packages/client/ui-vf-brand` —— 侧栏品牌、左栏三段、后端状态条
  - `packages/client/ui-vf-cards` —— 对话流里的阶段卡片（提案 / 进度 / 产物）
- 桌面壳（`apps/desktop`）只加了：独立数据目录、sidecar 生命周期、首启种子、打包配置。
- 分歧清单见 `docs/vf-fork-divergence.md`；卡片载荷契约见 `docs/vf-card-payload.md`。

## 6.2 加一个模型中转站（通用做法）

设置面板里的「模型」页，provider 全部来自 `llm-pi-ai` 的配置 —— 只要是 **OpenAI 兼容**的
中转站，加一段配置就能用（不用改代码、不用重新打包）：

```yaml
# %LOCALAPPDATA%\VideoForge\dsh\profiles\desktop\cordis.patch.yml
- id: llm-pi-ai
  config:
    providers:
      <你给这个中转站起的名字>:
        displayName: 显示名
        api: openai-completions            # 必须是这个协议
        baseURL: https://你的中转站/v1
        apiKeyEnv: <KEY 的引用名>           # 值放在 .credentials.yaml 的 refs 里
        defaultInput: [text, image]
        models:
          - id: 模型id1
          - id: 模型id2
            name: 显示名
```

再把 key 写进 `%LOCALAPPDATA%\VideoForge\dsh\.credentials.yaml`：

```yaml
refs:
  <KEY 的引用名>: sk-xxxx
records:
  llm-pi-ai/<中转站名字>:
    kind: api-key
    key: sk-xxxx
```

这台机器上已经加好了 **agentai 中转站**（`agentai`，baseURL `https://agentai.token6688.com/v1`），
模型目录里带了 claude / gpt / gemini / deepseek / qwen / glm / kimi / minimax / doubao 各一档，
在输入框右下角的模型选择器里直接能选。

**注意两套模型各管一段**：
- DSH 的模型（上面这些）= 负责**对话与调度**（决定下一步做什么、怎么改）。
- Python 侧的模型（`/api/settings` 里的 `llm_provider`、`image_provider`、`default_model_provider`）
  = 负责**真正干活**（写剧本、出图、出片）。两边可以不同，也可以都用 DeepSeek/agentai。

## 7. 第三轮改动（2026-10-21 晚）

| 改动 | 说明 |
|---|---|
| 首屏标题 | 改成「VideoForge 把想法做成片子」，前面那个图标换成你的 V 标 |
| 卡片按钮 | 以前点了没反应 —— 根因是**作用域错了**：`conversation.send()` 要求会话作用域，旧代码在根作用域调，异常被 `void` 吞掉。现在按 `ctx.sessions.scope(sessionId)` 取会话上下文，点一下就真发出去，并且按钮会变成「已发送 ✓」 |
| 剧本正文 | 只要项目里有剧本，卡片就会把**大纲 + 场次 + 对白**摊开（可滚动），并问你改不改 |
| 每步三个选择 | 每张卡都是「确认 / 我要改一下 / 跳过」；最后一步（后期成片）确认文案是「✅ 确认完成」 |
| 设置分区 | 设置里多了「VideoForge 模型」一页，按**文本 / 视频 / 音频 / 图像**四类分开配（旧版 VideoForge 的分法） |
| 旁白 | ① 没有台词就**不再自己编旁白**（原来会拿分镜概述/动作描述去 TTS）；② 语速默认 0.92；③ 配音优先用 minimax 自然音色，不再默认 edge 的机械音 |
| 白膜一致性 | 成片优先把**白膜首帧**当首帧图发给视频模型（构图/机位跟白膜一致，画面由模型重绘），并在结果里如实说明"本次以白膜首帧为首帧" |
