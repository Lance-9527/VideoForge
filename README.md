<div align="center">

# VideoForge

**用一句话，把创意变成成片的私人 AI 短片 Agent 桌面软件。**

[![Release](https://img.shields.io/github/v/release/Lance-9527/VideoForge?label=Release&color=blue)](https://github.com/Lance-9527/VideoForge/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-0078D6?logo=windows)](https://github.com/Lance-9527/VideoForge/releases/latest)
[![Built with](https://img.shields.io/badge/built%20with-Electron%20%2B%20Python%20sidecar-47848F)](./docs)
[![stars](https://img.shields.io/github/stars/Lance-9527/VideoForge?style=social)](https://github.com/Lance-9527/VideoForge)

</div>

---

## 这是什么

你在对话框里说一句话，它把整条流水线做完 —— 每一步递一张**确认卡**给你，
点「确认，开始做」就推进，不用反复打字、不用在多个页面之间跳。

```
AI 写剧本 → 抽角色/场景 → 出定妆照与场景图 → 拆分镜
          → 白膜预演 → 480P 小样 → 1080P 成片 → 后期合成
```

## 快速开始

### 下载（Windows x64）

1. 打开 **[Releases](https://github.com/Lance-9527/VideoForge/releases/latest)**，下载
   `videoforge-0.2.1-rc.2-win-x64-unsigned.exe`（约 398 MB）
2. 用 `checksum-MD5.txt` 校验完整性
3. 双击运行。**未签名版本**首次运行需要放行（Windows SmartScreen →「更多信息」→「仍要运行」）
4. 打开「设置」填上你自己的模型 API Key（VideoForge 不内置任何 Key）

> 数据只存在本机 `%LOCALAPPDATA%\VideoForge\data`，不上传。

### 从源码构建

```bash
# 1) 桌面端（Electron + 我们的 bundle）
cd deepseek-harness-master
pnpm install
pnpm --filter @videoforge/desktop build

# 2) Python sidecar（打进 apps/desktop/resources/videoforge-sidecar/）
#    见仓库根的 sync-sidecar-backend.py：把 backend/ 同步到 sidecar 的
#    _internal/backend/，再用 PyInstaller 生成 _internal/ 运行时
```

## 架构

```
┌──────────────────────────────────────────────────────────┐
│  DSH 底座（deepseek-harness-master）                        │
│    agent 运行时 · 工具协议 · 后台任务(jobs) · 审批 · UI 外壳  │
│    └─ packages/video/vf-video   ← 我们的 bundle（薄壳）      │
├──────────────────────────────────────────────────────────┤
│  Python sidecar（HTTP + SSE，唯一数据源 = 它的 SQLite）      │
│    apps/desktop/resources/videoforge-sidecar/_internal/backend/│
│      core/stages.py         九阶段流水线（唯一执行入口）      │
│      core/agent_tools*.py   79 个工具                        │
│      core/consistency.py    一致性基准（用户给定的优先）       │
│      core/continuity.py     转镜衔接 + 尾帧接力               │
│      core/shot_chain.py     跨镜接龙判定                    │
│      core/scene_anchors.py  场景锚点表（跨镜空间一致）        │
│      core/shot_prompts.py   分镜提示词编译（三段式）           │
│      core/keyframe.py       白膜 → 关键帧 → 成片             │
│      core/budget.py         花钱预算刹车                    │
│      core/adapters/*        视频平台适配器                    │
└──────────────────────────────────────────────────────────┘
```

### 四条纪律

写在 `packages/video/vf-video` 里，**别破坏**：

1. **执行入口只有一个** —— `core.agent_tools_stages.run_stage`。
   sidecar 只负责「提交 + 记账 + 转播进度」，不重写任何阶段业务。
2. **进度不另造一套** —— 一律读 `core.stages` 的 EVIDENCE，
   不自己数文件、不自己造百分比。
3. **金额不另写一套** —— 单价走 `core.shot_segments` 的价格表，
   余额/闸门走 `core.budget`。
4. **不动业务数据** —— sidecar 只写 `tasks` 表；
   `project_id` / `stage` / `params` 全在 payload 里，不新增表、不改 schema。

## 亮点

| 能力 | 说明 |
|---|---|
| **角色一致性** | 用户给定的角色基准**优先于**项目内多数派；基准角色每一镜都带参考图（哪怕分镜漏挂） |
| **转镜衔接** | 跨镜接龙：上一镜的**尾帧**直接当下一镜首帧；同场景换机位/时间跳跃时自动退回独立镜（带参考图），连续 2 镜后强制重校准防角色漂移 |
| **白膜 → 关键帧 → 成片** | 白膜预演的构图**真的落到成片**，不是只给用户看一眼 |
| **场景锚点** | 同一场景跨镜，道具不换边、角色不瞬移 |
| **花钱刹车** | 预算不够**直接拒绝生成**，不会「点了没反应」；每次候选生成前重新核算 |
| **一键推进** | 做完一步自动递下一步的确认卡，一路点到成片 |

## 目录结构

```
deepseek-harness-master/          DSH 底座源码
  packages/video/vf-video/        我们的 bundle（10 个工具 + 9 步 DAG + 面板）
  apps/desktop/                   Electron 桌面端
    resources/videoforge-sidecar/ Python sidecar（含业务源码 backend/）
docs/                             设计与使用文档
brand/                            品牌资源
```

> `_internal/` 下除 `backend/`、`frontend/` 外的都是 PyInstaller 生成物，
> 不入库（由构建脚本生成）。各 package 的 `lib/` 同理。

## 数据与隐私

- 项目数据只存在**本机**（`%LOCALAPPDATA%\VideoForge\data\videoforge.db`），不上传。
- API Key 由你在「设置」里填，存本地库。**仓库与安装包均不含任何凭据。**
- 视频生成走你自己配置的厂商账号，费用由你承担；软件内置预算刹车。

## 贡献

欢迎提 issue / PR。改之前先看 `deepseek-harness-master/CONTRIBUTING.zh.md`
（DSH 底座的贡献规范）与 `deepseek-harness-master/AGENTS.md`。

## License

[MIT](./LICENSE) —— 底座部分版权归 DeepSeek，业务部分版权归本项目作者。
第三方依赖清单见 [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md)。

## 致谢

- **[deepseek-harness-master](https://github.com/deepseek-ai/deepseek-harness-master)** — 底座
  （agent 运行时 / 工具协议 / 后台任务 / 审批 / Electron 桌面外壳）
- 视频平台：可灵、Seedance（豆包）、海螺、Runway、Sora、Vidu 等 —— 由用户自行配置账号与额度
