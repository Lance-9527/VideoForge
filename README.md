# VideoForge

**私人 AI 短片 Agent 桌面软件** —— 用一句话把创意变成成片。

VideoForge 把「AI 写剧本 → 抽角色场景 → 出定妆照与场景图 → 拆分镜 → 白膜预演 →
480P 小样 → 1080P 成片 → 后期合成」整条流水线做成一个**对话式 agent**：
你在对话框里描述想法，它负责把每一步的**确认卡**递给你，你点「确认，开始做」即可推进，
不用反复打字确认，也不用自己在多个页面之间来回跳。

---

## 架构

```
┌─────────────────────────────────────────────────────────┐
│  DSH 底座（deepseek-harness-master）                     │
│   · agent 运行时 / 工具协议 / 后台任务(jobs) / 审批       │
│   · 界面外壳（Electron 桌面端）                          │
│   └─ packages/video/vf-video     ← 我们的 bundle（薄壳）│
├─────────────────────────────────────────────────────────┤
│  Python sidecar（HTTP + SSE，唯一数据源 = 它的 SQLite）  │
│   apps/desktop/resources/videoforge-sidecar/_internal/backend/ │
│   · core/stages.py        九阶段流水线（唯一执行入口）   │
│   · core/agent_tools*.py  79 个工具（薄壳 → sidecar）     │
│   · core/consistency.py   一致性基准（用户给定的优先）     │
│   · core/continuity.py    转镜衔接 + 尾帧接力             │
│   · core/shot_chain.py    跨镜接龙判定                   │
│   · core/scene_anchors.py 场景锚点表（跨镜空间一致）      │
│   · core/shot_prompts.py  分镜提示词编译（三段式）         │
│   · core/keyframe.py      白膜 → 关键帧 → 成片            │
│   · core/budget.py        花钱预算刹车                   │
│   · core/adapters/*       视频平台适配器（可灵/豆包/海螺…）│
└─────────────────────────────────────────────────────────┘
```

**几条纪律**（写在 `packages/video/vf-video` 里，别破坏）：

1. **执行入口只有一个**：`core.agent_tools_stages.run_stage`。
   sidecar 只负责「提交 + 记账 + 转播进度」，不重写任何阶段业务。
2. **进度不另造一套**：一律读 `core.stages` 的 EVIDENCE，不自己数文件、不自己造百分比。
3. **金额不另写一套**：单价走 `core.shot_segments` 的价格表，余额/闸门走 `core.budget`。
4. **不动业务数据**：sidecar 只写 `tasks` 表；`project_id` / `stage` / `params`
   全在 payload 里，不新增表、不改 schema。

---

## 构建

```bash
# 1. DSH 侧（桌面端 + 我们的 bundle）
cd deepseek-harness-master
pnpm install
pnpm --filter @videoforge/desktop build        # 或仓库自带的打包脚本

# 2. Python sidecar（打进 apps/desktop/resources/videoforge-sidecar/）
#    见 sync-sidecar-backend.py：把 backend/ 同步到 sidecar 的 _internal/backend/
#    再用 PyInstaller 生成 _internal/ 运行时
```

打包产物：`videoforge-0.2.1-rc.2-win-x64-unsigned.exe`（Windows x64）。

---

## 目录

| 路径 | 内容 |
|---|---|
| `deepseek-harness-master/` | DSH 底座源码 + `packages/video/vf-video`（我们的 bundle） |
| `.../videoforge-sidecar/_internal/backend/` | Python sidecar 源码（业务本体） |
| `.../videoforge-sidecar/_internal/frontend/` | 前端源码 |
| `docs/` | 设计与使用文档 |

> **注意**：`_internal/` 下除 `backend/` 与 `frontend/` 外的都是 **PyInstaller 生成物**，
> 不入库（由构建脚本生成）。`dist/` 与 `*.exe` 同理。

---

## 数据与隐私

- 所有项目数据只存在**本机**（`%LOCALAPPDATA%\VideoForge\data\videoforge.db`），不上传。
- API Key 由用户在「设置」里填，存本地库，**不入库、不进版本库**。
- 视频生成走用户自己配置的厂商账号，费用由用户承担；软件内置**预算刹车**
  （超预算直接拒绝生成，见 `core/budget.py`）。

## License

见 `LICENSE`（沿用 DSH 底座的许可证）与 `THIRD_PARTY_NOTICES.md`。
