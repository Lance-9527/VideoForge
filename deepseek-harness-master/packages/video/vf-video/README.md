# @deepseek-ai/dsh-vf-video

VideoForge 的 DSH bundle 层：把 9 阶段流水线以**每步一个工具、每步一个 job** 的方式
暴露给 agent，业务全部在 Python sidecar（`VideoForge-dev/backend`，`/api/v2/*`）。

## 这是什么形状

一个 cordis 插件包 + 一层 `cordis.patch.yml`。装进 profile 后：

- `inject = ['tools', 'jobs']`，`apply(ctx, config)` 里用
  `ctx.effect(() => ctx.tools.register(defineTool({...})))` 注册工具；
- 长任务走 `ctx.jobs.start({ kind, label, run })`：立刻返回 `job_id`，进度写
  `handle.updateProgress` / `handle.append(..., { channel: 'log' })`，取消调 `hooks.cancel`；
- 取消是**真取消**：除了 abort SSE，还会 `POST /api/v2/jobs/{id}/cancel` 让 sidecar
  停掉 asyncio task（否则 Python 侧的活会继续跑完）。

## 工具

| 工具 | 作用 |
|---|---|
| `vf_status` | 只读：9 阶段状态 / 下一步 / 待确认提案 |
| `vf_project` | `action=list/get/create`（create = 阶段 brief） |
| `vf_<stage>` | 每个免费阶段一个工具：`vf_shots` / `vf_previz` / `vf_compose` |

`vf_project create` requires a known executing session, writes its id to project settings, and refuses a second project in that same session. The client opens a project's explicitly bound conversation; missing, archived or conflicting bindings require an explicit repair instead of guessing from session order or titles.

花钱阶段（`looks`=image、`draft`=video、`final`=expensive）**默认不注册**：
S1 只证明 job 机制，审批卡（`ctx.approval`）是 S3 的事。它们即使被
`config.stageKeys` 显式注册，`execute()` 也会拒绝并说明"需审批卡"。

## 关键：tool-jobs 必须打开

`packages/bundle/web-app/cordis.patch.yml:496` 把 `tool-jobs`（`@deepseek-ai/dsh-tool-jobs`）
设成 `disabled: true`（上游理由：web 目录不再暴露 `job_*` 控制工具）。

对 VideoForge 是**致命**的：`ctx.jobs.start()` 会抛
`background jobs unavailable: no job controller serves this agent`。

所以本包的 `cordis.patch.yml` 第一件事就是把它重新打开（同一行最后写入者为准）：

```yaml
- id: tool-jobs
  disabled: false
```

## 为什么是 JS 不是 TS

本包由 `D:\VideoForge-dev\dsh-plugin\` 的**已验证原型**直接搬来（selftest 22/22、
smoke-live 8/8、smoke-job 10/10 全绿），那份代码就是纯 ESM JS，运行时由宿主解析
`@deepseek-ai/dsh-tools`。

迁进 monorepo 时刻意**不引入构建步骤**：包体只有 `lib/*.js` + patch，
`main: lib/index.js`。这样不动 `tsconfig.host.json` 的 project references，
也不进 tsdown 的 TS 构建图 —— 对现有 `build:lib:host` 的失败面为零。
需要 TS 类型时再加 tsconfig 与 `src/`，不影响现在的加载路径。

## 验证

```powershell
cd D:\VideoForge-dev\dsh-plugin
node selftest.mjs      # 22/22
node smoke-live.mjs    # 8/8   （需 sidecar 在 8765）
node smoke-job.mjs     # 10/10 （真 status 轮询 + 打桩 confirm，零写入）
```

打包后：启动 VideoForge → 工具目录里应出现 `vf_status/vf_project/vf_shots/vf_previz/vf_compose`。
