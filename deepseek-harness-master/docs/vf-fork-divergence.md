# VideoForge fork — DSH divergence 清单

> 目的：这份文件是我们 fork 与上游 DSH 的**全部差异**。DSH 升级时按它逐条重打；
> 不在清单里的改动都不该存在（发现即视为事故）。
>
> 建立：2026-10-21。基线版本 `0.2.1-rc.2`。

---

## 0. 两条铁律

1. **不许手改 app 自己生成的 profile 层**
   `%LOCALAPPDATA%\VideoForge\dsh\profiles\desktop\cordis.patch.yml`（以及同目录 `package.json`）
   是 **app 启动时自己生成**的，会写入口令、模型选择、welcome 版本等运行时状态。
   手改它 = 覆盖掉 app 自己的状态，而且下次启动还会被覆盖回去。
   **要改行为 → 改 fork 里的 bundle / patch 源文件，让 app 重新生成。**
   (> 2026-10-21 踩过：为了做实验把这个文件覆盖成两条，导致 app 的 5 条运行时配置丢失，最后靠删目录重建。)

2. **用户的 `C:\Users\Administrator\.dsh\` 一个字都不许动**
   我们的 app 用自己 `DSH_HOME`（`%LOCALAPPDATA%\VideoForge\dsh`），见 §4-A。
   注意：用户那个 `cordis.patch.yml` **会随他自己的 DSH app 运行而变**（写 MCP 服务器配置），
   所以校验方式是"我们的键不在里面"，不是"哈希永久不变"。
   我们已知的基线：`package.json = E6D67364…`、`cordis.patch.yml = B2E71F94…`（2026-10-21）。

---

## 1. 上游 core 改动（3 类，全部最小化）

### A. 文案 4 行 — `packages/client/ui-conversation/src/client/locales.ts`

| 行 | key | 改为 |
|---|---|---|
| 79 | `hero.headline`（zh） | `把想法做成片子` |
| 459 | `hero.headline`（en） | `Turn ideas into films` |
| 24 | `placeholder.hero`（zh） | `说个想法，我帮你做成片子` |
| 405 | `placeholder.hero`（en） | `Describe an idea and I will turn it into a film` |

**为什么不能用自己的 bundle 覆盖**：client 的 locale 服务按 `(ns, locale)` 注册，
**同一个 key 重复注册直接抛错**（`packages/client/locale/src/client/index.ts:382` "single occupant"），
`addLanguage()` 只能新增语言、不能部分覆盖字典。所以这 4 行只能改 core。

### B. bundle 注册 1 条 — `packages/bundle/web-app/cordis.patch.yml`

```yaml
- id: vf-workbench
  name: '@deepseek-ai/dsh-client-ui-vf-workbench'
```

**为什么必须改 core**：DSH **不会自动发现 client bundle**，必须在 bundle patch 里按 id 注册。
（放在我们自己的 patch 层也可以，但 S-C 的包与 `ui-chat` 等同属 UI 层，放一起可读性最好。）

---

## 2. 我们的 bundle（不碰 core，纯新增）

| 包 | 作用 |
|---|---|
| `packages/video/vf-video/` | 9 阶段工具（`vf_status` / `vf_project` / `vf_<stage>`）+ 组合层 patch |
| `packages/client/ui-vf-brand/` | 品牌位 + 侧边栏导航 + 落地工作台外壳 |
| `packages/client/ui-vf-workbench/` | 9 步工作台三栏内容（S-C） |

`packages/video/vf-video/cordis.patch.yml` 是**我们唯一的组合入口**，它做 6 件事：
1. `tool-jobs` → `disabled: false`（web-app 层默认关掉了它；不开则 `ctx.jobs` 报 "no job controller"）
2. `insert` → `- id: vf-video, name: '@deepseek-ai/dsh-vf-video'`
3. `insert` → `- id: ui-vf-brand, name: '@deepseek-ai/dsh-client-ui-vf-brand'`
4. `ui-brand-official` → `disabled: true`（它只在 `DSH_CLIENT_BUILD_PROFILE=official` 时注册，会抢品牌位）
5. `ui-plugin-manager` / `ui-schedule` / `ui-skill` → `disabled: true`
   （关掉侧边栏的「插件 / 自动化任务 / 技能」三行；**实测安全，启动 0 pending**）
6. `ui-workspace` → **保持启用**，用 slot priority 覆盖它的 section 区（见 §3-A）

---

## 3. 两条必须知道的平台事实（升级时最容易被坑）

### A. `ui-workspace` 是**承重墙**，不能 disable

它提供 client 端 `uiWorkspace` 服务。关掉后 boot 直接失败：

```
Error: web boot: 13 entries did not activate
  @deepseek-ai/dsh-client-ui-sidebar: pending (waiting for service: uiWorkspace)
  @deepseek-ai/dsh-client-ui-conversation: pending (waiting for service: uiWorkspace)
  @deepseek-ai/dsh-client-ui-chat: pending (waiting for services: uiWorkspace, uiConversation)
  @deepseek-ai/dsh-client-ui-reference / goal / plan / deliverables / trajectory / user-questions …
```

**正确做法**：保留 `ui-workspace`，用 `sidebar.workspaces` 槽的 **priority 覆盖**它注册的内容。

### B. `single` 槽是**按 priority 覆盖**，不是"后写覆盖"

同一个 `single` 槽在同 priority（默认 0）注册两次会**抛错**：

```
Uncaught Error: single slot "sidebar.workspaces" already has a registration at priority 0
  (registered by Kb) — register at a different priority to shadow it (lowest renders)
```

语义：**priority 升序，最低的那个渲染**（`packages/client/ui-slots/src/index.ts:1346` 一带）。
所以要压过上游（priority 0），我们注册 **`priority: -10`**。

`sidebar.workspaces` / `sidebar.brand.mark` / `sidebar.brand.name` / `shell.overlay`…
凡是 `single` 的都按这条规律；`list` 槽（如 `shell.overlay`）则是多人共存、按 `order` 排序。

---

## 4. apps/ 改动（打包与启动）

### A. 数据目录隔离 — `apps/desktop/src/main.ts`（模块作用域）

- `userData` / `sessionData` → `%LOCALAPPDATA%\VideoForge\electron`
- `DSH_HOME` → `%LOCALAPPDATA%\VideoForge\dsh`
- **两个实测坑**：
  1. 隔离不能以"`DSH_HOME` 为空"为前提 —— 从 DSH 派生的 shell 会把 `DSH_HOME` 带进来，
     导致隔离整体跳过，host 直接拿用户的 `.dsh\profiles\desktop`。
     ⇒ 只要继承值不在我们自己 root 下就覆盖它（逃生阀 `VIDEOFORGE_KEEP_DSH_HOME=1`）。
  2. Windows 数据根要用 **`%LOCALAPPDATA%`**，不是 `%APPDATA%`（Roaming）。

### B. 默认组合 — `apps/desktop/src/project-manager.ts`

`VF_PROFILE_BUNDLES = [...WEB_PROFILE.bundles, '@deepseek-ai/dsh-vf-video']`，
四处引用（含 `sanitizeProfile` 恢复路径，避免"恢复"把我们的 bundle 摘掉）。

### C. 启动方式 — Chromium 缓存在 JS 之前就定了

双击裸 `VideoForge.exe` 会 `Unable to move the cache: 拒绝访问` 秒退；
**必须**在进程启动前给出 `--user-data-dir`。三处落地：

| 位置 | 做法 |
|---|---|
| `apps/desktop/scripts/installer.nsh` | `customInstall` 里**覆盖**桌面/开始菜单快捷方式，追加 `--user-data-dir=$LOCALAPPDATA\VideoForge\electron`（`installSection.nsh` 先建链接再调 `customInstall`，所以是覆盖不是新增） |
| `apps/desktop/resources/win/VideoForge.cmd` | 免安装版启动器；**纯 ASCII + CRLF**（UTF-8 中文注释会被 cmd.exe 按 OEM 代码页拆行当命令执行）；并清掉 `ELECTRON_RUN_AS_NODE` |
| `apps/desktop/scripts/electron-builder-config.mjs` | `extraFiles` 把上面的 `.cmd` 放进安装目录/`win-unpacked` |

### D. 运行时闭包 — `apps/cli/package.json`

加入 3 个 workspace 依赖，否则 `prepare-dsh` 的闭包选择不会把它们放进 `resources/app.asar/dsh/node_modules`：
`@deepseek-ai/dsh-vf-video`、`@deepseek-ai/dsh-client-ui-vf-brand`、`@deepseek-ai/dsh-client-ui-vf-workbench`。

### E. 手写 ESM 包不进 tsdown — `tsdown.config.ts`

`PREBUILT_PACKAGES = ['packages/client/ui-vf-workbench', 'packages/video/vf-video']`。
原因：workspace pass 要求每个成员有 `lib/types/{index,startup}.js` 入口，这两个包是手写 ESM。
**排除是构建专用**：`release:pack` 的家族发现用另一套 glob，仍会打包它们。

### F. 打包网络坑

`runtime:lockfile` 那步会**重新联网解析整棵运行时依赖**（本地 store 全命中也会拉元数据）。
网络劣化时单请求能到 200–460 秒，看起来像卡死。
`prefer_offline` **绕不过**（解析需要新鲜元数据）。失败/超时就**重试**；正常时约 16 秒。

---

## 5. S-C 包 `ui-vf-workbench` 的 manifest（为进 tarball 必须改）

`packages/client/ui-vf-workbench/package.json`（备份 `.bak-vfbrand`）：

| 字段 | 改动 | 原因 |
|---|---|---|
| `private` | **删除** | release 家族发现跳过 `private: true` 的清单 ⇒ 永远不会被打包 |
| `version` | `0.1.0` → `0.2.1-rc.2` | dsh 家族要求所有成员共用一个版本（`verifyVersions`） |
| `files` | 白名单（不含 `lib/*.map`） | 出版策略 `scripts/publication-payload.ts:53` **拒绝 tarball 内含 source map** |
| `publishConfig` / `repository` | 新增 | 公开发布政策要求 |

---

## 6. 升级 DSH 时的重打顺序

1. 合并上游 → 按 §1 重打 4 行文案 + 1 条 bundle 注册
2. 按 §4 重打 apps/ 的 5 处（数据目录、默认组合、启动方式、闭包、tsdown 排除）
3. 检查 §3 的两条平台事实是否仍然成立（槽位 priority 语义、`uiWorkspace` 依赖面）
4. 检查 §5 的 manifest 政策是否仍然如此
5. 打包 → 双击 `VideoForge.cmd` → 验：落地是 VideoForge 工作台、侧边栏是 项目/流水线/产物、
   用户 `.dsh` 无我们写入的键
