# VideoForge 对话卡片载荷契约（`kind:'vf'`）

> 状态：**v1 草案，等 lead + flow-auditor 确认后冻结**
> 写于：2026-10-21
> 读者：① 写工具的人（host 侧，`packages/video/vf-video`）② 渲染卡片的人（client 侧，`packages/client/ui-vf-cards`）③ 接审批的人（`ctx.approval`，flow-auditor）
> 一句话：**工具只把"事实"放进载荷，卡片只负责把它摆出来 —— 卡片永远不编数字。**

---

## 0. 载荷怎么到卡片手里

```
工具 execute() 返回 value（JSON，模型看得到）
        ↓
工具 output.presentationMeta(args, value) → 卡片载荷（纯函数，同步）
        ↓   宿主把它记进工具结果的 meta
对话流 ToolResultNode.meta
        ↓   keyed 槽 tool.call.toolview（key = 工具名）
VfToolRow({ phase, block }) → readPayload(block.meta) → <卡片>
```

- 槽声明：`packages/client/ui-tool/src/client/contract/slots.ts:26` `'tool.call.toolview': { kind:'keyed', scope:'session' }`
- 槽只给 `phase` + `block`，**不给业务回调**；按钮走 client 侧注入的 `conversation.send(text)`（与输入框同一个发送路径）。
- 兜底：`meta` 缺失时，渲染层退回通用工具行（**不报错、不白屏**）。

---

## 1. 顶层字段（所有臂共用）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `card` | `'vf'` | ✅ | 固定字面量，用于一眼认出是我们的载荷 |
| `kind` | `'gate' \| 'progress' \| 'artifact' \| 'note'` | ✅ | 选哪一臂；未知值 → `note` |
| `title` | `string` | ✅ | 卡片标题（如 `下一步：剧本`） |
| `stage` | `string` | ⭕ | 阶段 key（`script`/`looks`/…），渲染成左上角蓝色 pill |
| `evidence` | `string` | ⭕ | **上一步的证据原文**，逐字照抄，不许改写/截断成"已完成" |

### 三臂必填子集

| 臂 | 必填 | 建议 | 用途 |
|---|---|---|---|
| `gate`（提案/审批） | `title`, `cost_label` | `why` `stage` `eta` `doing[]` `tweaks[]` `evidence` `cost_yuan` `cost_floor_yuan` `warn` `note` `can_skip` | 「下一步做什么，花多少，你点不点」 |
| `progress` | `title` | `stage` `have` `total` `message` `items[]` `error` `job_id` | 在跑哪一步 + 几/几 + 失败原因原文 |
| `artifact` | `title`, `media[]` 或 `body` | `final_url` `script_text` `shots[]` | 成片/白膜帧/定妆照/剧本/分镜 |
| `note` | `title` 或 `body` | — | 其它一切（纯文本兜底） |

---

## 2. `gate` 臂：金额口径（**最容易被写错的地方**）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `cost_label` | `string` | ✅ | 成本档位人话，取值域固定五种（见下） |
| `cost_yuan` | `number \| null` | ✅ | **具体金额**（来自 sidecar `estimate.cost_yuan`）。**拿不到就必须是 `null`** |
| `cost_floor_yuan` | `number \| null` | ⭕ | 只有下界时填这里（`estimate.cost_floor_yuan`） |
| `can_skip` | `boolean` | ⭕ | 默认 `true`；`false` 时不出「跳过」按钮 |
| `spend` | `boolean` | ⭕ | `true` = 这一步真花钱 → 由 `ctx.approval` 出原生审批 UI |

**`cost_label` 取值域**（与 `core/stages.py:51 COST_LABEL` 同口径，不要自造）：

| key | 文案 |
|---|---|
| `free` | `免费（本地跑，不花钱）` |
| `cheap` | `很少的钱（一次文字模型调用）` |
| `image` | `按张计费（图像模型）` |
| `video` | `按条计费（视频模型，明显花钱）` |
| `expensive` | `最贵的一步（1080P 成片）` |

### `cost_yuan: null` 的语义（硬规则）

1. **必须**渲染成「**金额：待确认**」，并给出「后端没给这一档的价目表 —— 不编数字」之类的说明；
2. `cost_floor_yuan` 存在时渲染成「下界 ¥8.40（**只是下界，不是报价**）」，**不得**把下界写成金额；
3. `null` **绝不能**被当作 `0`（那是"冒充免费"）、也**绝不能**用别的 provider 的价目表顶上；
4. `cost_yuan` 有数时，照实显示 `¥x.xx`，并保留 `cost_label` 作为档位说明。

> 真实案例：出片决定走 token6688 网关（`seedance_t6`）后，本地没有该通道价目表，
> `estimate` 返回 `cost_yuan: null` + `cost_floor_yuan: 8.40`（0.42 元/秒 × 10 秒 × 2 镜的下界）。
> 卡上就写「待确认 / 下界 ¥8.40」，**不能**写「¥8.40」或「免费」。

---

## 3. `progress` 臂

| 字段 | 类型 | 说明 |
|---|---|---|
| `have` / `total` | `number` | 逐项进度（几/几），来自 sidecar 阶段证据（`evidence.have/total`） |
| `message` | `string` | 当前一句话（sidecar `progress_message`） |
| `items` | `string[]` | 逐条明细（如 `第 2/3 镜出好了`） |
| `error` | `string` | **失败原因原文**，逐字 |
| `job_id` | `string` | 取消用 |
| `cancellable` | `boolean` | 默认 `true`；取消走 `POST /api/v2/jobs/{job_id}/cancel` |

---

## 4. `artifact` 臂：媒体 URL 形状（**零内联字节**）

媒体**一律**用 HTTP URL，**不传 base64、不传本地文件字节**（原则：媒体走 HTTP，控制信号走 IPC；`<img>/<video>` 不受 CORS 限制，只有 canvas 回读才需要）。

| 情形 | URL 形状 |
|---|---|
| 成片（可播放） | `http://127.0.0.1:8765/api/projects/{project_id}/final` |
| 成片（下载） | 同上 + `?download=1` |
| 白膜帧 / 定妆照 / 场景图 / 联络表 | `http://127.0.0.1:8765/api/media?path=<urlencode(绝对路径)>` |

- `path` **必须** `encodeURIComponent`；Windows 反斜杠要编码（`%5C`）。
- `/api/media` 只放行 `data/`、`cache/`、`outputs/` 下的文件（sidecar `main.py:3002`）；越界回 403。
- `media[]` 每项：`{ kind: 'video'|'image', url, name, tag? }`；`kind:'video'` 出 `<video muted preload=metadata>` 缩略图 + 「▶ 视频」tag，其它出 `<img>` + 「🖼 图片」tag。
- 缩略图 132×92（老界面 `.msg-media-cell` 尺寸），点一下开灯箱全屏。

---

## 5. 真实 JSON 样例（**全部取自 4767a191「雨夜便利店」实测，未编造**）

> 项目：`4767a191-6268-4bed-9bf3-07f2e4a9413c`，标题「雨夜便利店」
> 数据来源：2026-10-21 12:24–12:26 对 sidecar `127.0.0.1:8765` 的实测响应（`/stage/status`、`/api/v2/stage/draft/estimate`、`/final`、`/framesheet`）
> 注：sidecar 之后被停掉做 autostart 改造，样例中的数值是当时抓到并落进本会话的原文，未重新生成。

### 5.1 `gate` 臂（花钱那一步 → 真机上由 `ctx.approval` 出原生按钮）

```json
{
  "card": "vf",
  "kind": "gate",
  "stage": "draft",
  "title": "下一步：480P 小样",
  "why": "用便宜档位出一版小样，确认「脸对不对、动作对不对」。小样不满意就驳回，别急着出成片 —— 成片贵得多。",
  "cost_label": "按条计费（视频模型，明显花钱）",
  "cost_yuan": null,
  "cost_floor_yuan": 8.40,
  "eta": "每条 2 ~ 8 分钟",
  "spend": true,
  "can_skip": true,
  "doing": ["给 2 个还没出过小样的镜头出 480P 片段"],
  "tweaks": ["再来一版", "换模型", "这一段重出"],
  "evidence": "0/2 个镜头出过样片",
  "note": "不确认就不会扣费；执行中也可以取消。"
}
```

> `cost_yuan: null` 是**实测值**：该项目 shot 的 provider 在本地没有价目表，
> `estimate` 原文即 `{"cost_yuan": null, "cost_floor_yuan": 8.4, "eta": "每条 2 ~ 8 分钟", "needs_approval": true}`。

### 5.2 `progress` 臂（工具 `phase:'start'` 或「正在跑」的结果）

```json
{
  "card": "vf",
  "kind": "progress",
  "stage": "compose",
  "title": "正在跑：后期成片",
  "have": null,
  "total": null,
  "message": "还没有合成过成片",
  "items": [],
  "error": "",
  "job_id": "e4933d20-7326-4d76-a1f8-efa858b7b566",
  "cancellable": true
}
```

> `message` 原文来自 sidecar SSE `event: progress` 的 `data.message`；
> 同一 job 的下一条就是 `"已有成片 final.mp4"`（`source:"evidence"`，来自 `core.stages` 的 `_ev_compose`）。
> 失败时 `error` 放**原文**，例如 `「立项」这一步**已经做完了**，再跑一遍会覆盖掉现有内容。…`（实测 3ca99c5b 那条 job 的报错原文）。

### 5.3 `artifact` 臂（成片 + 白膜帧 + 剧本 + 分镜）

```json
{
  "card": "vf",
  "kind": "artifact",
  "stage": "compose",
  "title": "产物：雨夜便利店",
  "final_url": "http://127.0.0.1:8765/api/projects/4767a191-6268-4bed-9bf3-07f2e4a9413c/final",
  "media": [
    {
      "kind": "video",
      "url": "http://127.0.0.1:8765/api/projects/4767a191-6268-4bed-9bf3-07f2e4a9413c/final",
      "name": "final.mp4",
      "tag": "▶ 视频"
    },
    {
      "kind": "image",
      "url": "http://127.0.0.1:8765/api/media?path=D%3A%5CVideoForge-dev%5Cbackend%5Cdata%5Coutputs%5C4767a191-6268-4bed-9bf3-07f2e4a9413c%5Cpreviz%5C%E9%95%9C01%5Cf0001.png",
      "name": "白膜帧 · 第1镜.png",
      "tag": "🖼 图片"
    },
    {
      "kind": "image",
      "url": "http://127.0.0.1:8765/api/media?path=D%3A%5CVideoForge-dev%5Cbackend%5Cdata%5Coutputs%5C4767a191-6268-4bed-9bf3-07f2e4a9413c%5Cpreviz%5C%E9%95%9C02%5Cf0001.png",
      "name": "白膜帧 · 第2镜.png",
      "tag": "🖼 图片"
    }
  ],
  "script_text": "雨夜里的便利店，在雨幕与灯光之间藏着未明的故事。",
  "shots": [
    { "index": 1, "text": "雨夜街边的便利店外空无一人，暖白灯光从玻璃窗内透出，雨水沿玻璃缓慢滑落，在积水的路面倒映出破碎光影。" },
    { "index": 2, "text": "雨夜街边的便利店内空无一人，镜头贴着玻璃门从街边缓慢移入，门外的雨声被玻璃隔成沉闷连续的白噪音。" }
  ]
}
```

实测核对（同一项目，当次抓到）：
- `/final` → `video/mp4`，`1280×720`，`duration = 20.021s`（浏览器 `<video>` `readyState=4`）
- 两张白膜帧 → `/api/media` 返回 `960` px 宽，均为 `镜01/f0001.png`、`镜02/f0001.png`
- `shots` 两条 `layer1_overview` 原文如上方 `text`；`character_ids` 全空（`/stage/status` 判词：「2 个镜头（其中 2 个没关联角色）」）
- `script`：`{ "summary/logline": "雨夜里的便利店，在雨幕与灯光之间藏着未明的故事。" }`，另含 2 场戏

---

## 6. 两处已知的"历史写法"，渲染层必须兼容

| 历史写法 | 现行规范 | 处理 |
|---|---|---|
| `kind: 'approval'` + `card_kind` | `kind: 'gate'` + `spend: true` | 渲染层把 `approval` 映射成 `gate`（`spend=true`），字段名 `cost_yuan` 优先、`costYuan` 兼容 |
| `costYuan`（驼峰，ui-vf-cards 旧稿） | `cost_yuan`（下划线，与 sidecar/estimate 一致） | 渲染层两个都读，**写载荷一律用 `cost_yuan`** |

---

## 6.5 归属：谁注册、谁渲染（2026-10-21 去重后定稿）

| 东西 | 归属 | 证据 |
|---|---|---|
| `tool.call.toolview` 的 keyed 注册（8 个 key：`vf_script` `vf_assets` `vf_looks` `vf_shots` `vf_previz` `vf_draft` `vf_final` `vf_compose`） | **`packages/client/ui-vf-cards`**（唯一） | `src/client/index.ts` `STEP_TOOLS` |
| 卡片渲染（gate / progress / artifact / note） | 同上 `src/client/VfCard.tsx` | 老站 CSS 逐条抄、收在 `.vf-cards` |
| `vf_status` / `vf_project` | **不接管**，走 DSH 通用工具行 | 它们是查询，不是"步骤" |
| 整屏工作台布局 | `packages/client/ui-vf-workbench`，**默认不挂**（`?vf=workbench` 才开） | 默认挂会盖住对话流里的卡片 |
| 按钮动作 | client 侧 `conversation.send(text)`（与输入框同一路径） | `ui-vf-cards/src/client/index.ts` |

> ⚠️ **两个包不许注册同一批 key**：keyed 槽后注册者覆盖前者，会变成"时好时坏"的故障。

---

## 7. 验收清单（写载荷的人自查）

- [ ] `card:'vf'` + `kind` 都在，且 `kind` 在四值域内
- [ ] `cost_label` 只用那五句，没有自造文案
- [ ] 花钱步骤：`cost_yuan` 是 `number` 或 `null`；**没有把 `null` 写成 0**
- [ ] `null` 时若知道下界，填了 `cost_floor_yuan`，且 UI 上明确写「下界，不是报价」
- [ ] `evidence` 是**原文**，不是"已完成"
- [ ] `media[].url` 是 `http://127.0.0.1:8765/...` 形状，`path` 做过 `encodeURIComponent`，**没有 base64**
- [ ] `error` 是失败原文，没有被改写成"出错了"
- [ ] 载荷里**没有**敏感信息（api key、绝对家目录以外的路径）
