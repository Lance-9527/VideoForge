# @deepseek-ai/dsh-client-ui-vf-workbench

VideoForge 一屏工作台（面向「拍片子的人」，不是面向「搭 agent 的人」）。

```
项目（可搜索列表） │ 9 步流水线 │ 产物（剧本 / 定妆照 / 分镜 / 成片）
```

数据全部来自本机 Python sidecar（默认 `http://127.0.0.1:8765`，面板右上角可改）：
**库是唯一真相，面板不持有业务状态。**

## 它做什么

| 区域 | 数据源 | 行为 |
|---|---|---|
| 项目列表 + 搜索 | `GET /api/projects` | 点一下切换当前项目 |
| 9 步流水线 | `GET /api/projects/{pid}/stage/status` | 每步显示 **状态 / 证据（evidence.detail）/ 耗时 / 花钱与否**；`还没轮到` 的步骤按钮置灰并说明原因 |
| 执行（免费） | `POST /api/v2/run` | 点了就排队，立刻拿 `job_id` |
| 执行（花钱） | `GET /api/v2/stage/{stage}/estimate` → 审批卡 | 先出金额/耗时卡，**确认才执行，未确认不扣费**；后端说"金额待确认"时如实显示，不编数字 |
| 实时进度 | `GET /api/v2/jobs/{id}/events`（SSE） | 逐条推进度日志 + 进度条；`event: done` 收尾后自动刷新流水线 |
| 产物 | `GET /api/projects/{pid}/script`、`/characters`、`/scenes`、`/shots` | 直接显示**正文/清单**，不给"已生成"一句话 |

## 怎么加载进 DSH（**需要别人加一行，不在本包写范围内**）

本包只负责 bundle 本身。要在应用里看到它，需要在 bundle 的插件清单里登记一行
（`packages/bundle/web-app/cordis.patch.yml` 的 `config.plugins` 列表内，
与 `- id: ui-chat` 并列）：

```yaml
    - id: vf-workbench
      name: '@deepseek-ai/dsh-client-ui-vf-workbench'
```

注册后它挂在 `shell.overlay` 槽（全窗口覆盖层），不抢聊天视图的 `main` 槽。

## 构建

```powershell
# 这台机器没 git，构建必须带这些变量
$env:DSH_CLIENT_COMMIT_HASH="0f1a2b3"
$env:DSH_CLIENT_VERSION="2.1.0"
$env:DSH_DESKTOP_BUILD_COMMIT="0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c"
$env:DSH_DESKTOP_BUILD_DIRTY="1"
cd packages/client/ui-vf-workbench
& <repo>\node_modules\.bin\tsdown.CMD          # → lib/index.js + lib/client.js
```

产物：`lib/index.js`（host 半，空 apply）与 `lib/client.js`（浏览器半，
`window.__ModuleLoader__.load({id, factory})` 契约）。

## 本地渲染自检（不需要 DSH 也能看界面）

```powershell
& <repo>\node_modules\.bin\tsdown.CMD --config harness/tsdown.config.ts
python -m http.server 8100 --bind 127.0.0.1      # 在包根目录起
# 打开 http://127.0.0.1:8100/harness/index.html
```

`harness/` 用**同一个 `lib/client.js`** 走一遍真实加载契约（`__ModuleLoader__` + 注入
`require`），再拿真实 sidecar 数据渲染 —— 用来证明 UI 本身是对的，并产出截图。

> `node_modules/{react,react-dom}` 是指向 pnpm store 的 junction，只为让 harness 能
> 把 React 打进单文件；本包运行时不依赖它（React 由宿主通过 `require` 提供）。

## 已知边界

- 面板本身**不实现**业务：所有写操作都打 sidecar；sidecar 连哪个库由 `VIDEOFORGE_DATA_DIR` 决定。
- 花钱步骤的"金额"完全来自 sidecar 的 estimate；sidecar 说 `null` 就显示"待确认"。
- 预览视频/图片**没有内联**（v2 设计 D 条：大字节不进 IPC），当前只显示路径与存在性。
