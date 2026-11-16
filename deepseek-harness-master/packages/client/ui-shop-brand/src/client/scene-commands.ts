/**
 * 掌柜 AI · **具名指令总线 + 侧车轮询器**。
 *
 * ★ 这是 openVideoForge 的 AGENT-PROTOCOL 在掌柜 AI 里的对应物：
 *   openvideoforge 的内核是「**一条具名指令，人和 AI 走同一套**」——
 *   页面内输入框 → 关键词解析 → 指令总线 → 同一份实现；
 *   外部 Agent 经 HTTP `/cmd` 或 MCP 进同一座桥 → SSE → 页面执行 → 结果回传。
 *
 *   掌柜 AI 原来只有页面内的关键词解析，**外部 agent 完全进不来**：
 *   sidecar 的 `/api/studio/splat/commands` 是个**没人消费的队列**
 *   （实测：GET 只返回最后一条，没有客户端轮询，DELETE 404）。
 *
 *   本模块把那一环补上：
 *     agent ──POST /commands──▶ sidecar 队列
 *     客户端轮询 GET /commands → 执行具名指令 → POST /result 回传
 *
 *   侧车的三个接口都是现成的（2026-11-04 实测）：
 *     GET  /api/studio/splat/commands → {"commands":[…]}
 *     POST /api/studio/splat/result   → {"id","ok","result","error","finished"}
 *     GET  /api/studio/splat/results  → {"results":[…]}
 *   ⇒ **不需要改冻结的 sidecar** ✓
 *
 * @module
 */

/** 一条命令。 */
export interface SceneCommand {
  readonly id: string
  readonly tool: string
  readonly args?: Record<string, unknown>
}

/** 执行器：跑一条具名指令，返回结果。 */
export type CommandRunner = (tool: string, args: Record<string, unknown>) => Promise<unknown>

/** 轮询器句柄。 */
export interface CommandPoller {
  stop(): void
  /** 手动跑一轮（测试用）。 */
  pollOnce(): Promise<void>
}

/** 一条还没回传成功的结果。 */
interface PendingResult {
  readonly id: string
  readonly ok: boolean
  readonly result: unknown
  readonly error: string | null
}

/** 回传结果的重试次数（含第一次）。 */
const RESULT_POST_ATTEMPTS = 3

/** 回传结果的重试间隔基数（毫秒）：第 n 次等 base × 2ⁿ⁻¹。 */
const RESULT_POST_BACKOFF_MS = 400

/** 回传结果的单次超时（毫秒）。 */
const RESULT_POST_TIMEOUT_MS = 10_000

/** 取命令的单次超时（毫秒）。 */
const COMMAND_GET_TIMEOUT_MS = 10_000

/**
 * 从各种可能的返回形状里取出命令数组。
 *
 * ★ 兼容三种形状：冻结 sidecar 实测返回 `{"commands":[…]}`；
 *   源码版 `take()` 返回的是单条；有些版本返回 `{"command":{…}}`。
 * @param payload - 解析后的 JSON。
 * @returns 命令数组。
 */
export function extractCommands(payload: unknown): SceneCommand[] {
  if (Array.isArray(payload)) {
    return payload.filter((item): item is SceneCommand =>
      typeof item === 'object' && item !== null && typeof (item as SceneCommand).id === 'string')
  }
  if (typeof payload !== 'object' || payload === null) { return [] }
  const record = payload as { commands?: unknown; command?: unknown }
  if (Array.isArray(record.commands)) { return extractCommands(record.commands) }
  if (typeof record.command === 'object' && record.command !== null) { return extractCommands([record.command]) }
  return []
}

/**
 * 回传一条执行结果，失败按指数退避重试。
 *
 * ★ sidecar 的 `take()` 是**取出即出队** —— 客户端挂了这条命令就没了。
 *   所以回传必须重试；重试还不成功就留在待回传队列里，下一轮再送
 *   （而不是把命令标记成「已处理」后丢掉）。
 *
 * @param base - sidecar 基址。
 * @param pending - 要回传的结果。
 * @returns 是否已成功回传。
 */
async function postResult(base: string, pending: PendingResult): Promise<boolean> {
  const body = JSON.stringify({
    id: pending.id, ok: pending.ok, result: pending.result, error: pending.error,
  })
  for (let attempt = 0; attempt < RESULT_POST_ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch(`${base}/api/studio/splat/result`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        signal: AbortSignal.timeout(RESULT_POST_TIMEOUT_MS),
      })
      if (response.ok) { return true }
    } catch {
      /* 网络失败或超时：退避后再试 */
    }
    if (attempt < RESULT_POST_ATTEMPTS - 1) {
      await new Promise(resolve => setTimeout(resolve, RESULT_POST_BACKOFF_MS * (2 ** attempt)))
    }
  }
  return false
}

/**
 * 起轮询器：把侧车队列里的命令取出来执行，再把结果回传。
 *
 * @param options - 配置。
 * @returns 轮询器句柄。
 */
export function startCommandPoller(options: {
  /**
   * 侧车地址（形如 http://127.0.0.1:8767）。
   *
   * 可以给函数：端口由主进程决定，可能在轮询器起来之后才确定 ——
   * 每轮现取，别把过期地址烤进来。
   */
  readonly sidecarBase: string | (() => string)
  /** 指令执行器。 */
  readonly run: CommandRunner
  /** 轮询间隔（毫秒）。 */
  readonly intervalMs?: number
  /** 出错时的回调。 */
  readonly onError?: (error: unknown) => void
}): CommandPoller {
  const interval = options.intervalMs ?? 1200
  const resolveBase = (): string => (typeof options.sidecarBase === 'function' ? options.sidecarBase() : options.sidecarBase)
  // 已经**回传成功**的命令 id —— 只有结果真的送出去了才算处理完。
  const handled = new Set<string>()
  // 已经**开始执行**的命令 id —— 防止同一条命令被重复执行（结果回传失败时
  // 由待回传队列负责补送，不靠重新执行）。
  const executed = new Set<string>()
  // 回传失败的结果：留在队列里，下一轮继续送（命令已经被 sidecar 出队了）。
  const undelivered: PendingResult[] = []
  let stopped = false
  let running = false

  const remember = (set: Set<string>, id: string): void => {
    set.add(id)
    // 只留最近 200 条，防止无限增长
    if (set.size > 200) {
      const first = set.values().next()
      if (first.done !== true) { set.delete(first.value) }
    }
  }

  const pollOnce = async (): Promise<void> => {
    if (running || stopped) { return }
    running = true
    try {
      const base = resolveBase()
      // ★ 先补送上一轮没送出去的结果。
      for (const pending of [...undelivered]) {
        if (await postResult(base, pending)) {
          const index = undelivered.indexOf(pending)
          if (index >= 0) { undelivered.splice(index, 1) }
          remember(handled, pending.id)
        }
      }
      const response = await fetch(`${base}/api/studio/splat/commands`, {
        cache: 'no-store',
        signal: AbortSignal.timeout(COMMAND_GET_TIMEOUT_MS),
      })
      if (!response.ok) { return }
      const payload = await response.json() as unknown
      const commands = extractCommands(payload)
      for (const command of commands) {
        if (handled.has(command.id) || executed.has(command.id)) { continue }
        remember(executed, command.id)
        let ok = true
        let result: unknown = null
        let error: string | null = null
        try {
          result = await options.run(command.tool, command.args ?? {})
        } catch (thrown) {
          ok = false
          error = thrown instanceof Error ? thrown.message : String(thrown)
        }
        const pending: PendingResult = { id: command.id, ok, result, error }
        if (await postResult(base, pending)) {
          remember(handled, command.id)
        } else {
          // 送不出去就留着，下一轮再送 —— 绝不因为一次网络抖动把命令丢掉。
          undelivered.push(pending)
          options.onError?.(new Error(`命令结果回传失败：${command.id}`))
        }
      }
    } catch (thrown) {
      options.onError?.(thrown)
    } finally {
      running = false
    }
  }

  const timer = setInterval(() => { void pollOnce() }, interval)
  return {
    stop: () => { stopped = true; clearInterval(timer) },
    pollOnce,
  }
}

/**
 * 指令清单（给 agent 看 —— 这份必须和实际实现对得上）。
 *
 * ★ 之前 AGENT_BRIEF 里写的「操作通道」是**假的**（没人执行那个队列），
 *   现在这份是真能跑的：客户端每 1.2 秒取一次命令，执行完回传结果。
 */
export const SCENE_COMMANDS: readonly { name: string; args: string; summary: string }[] = [
  { name: 'get_scene', args: '{}', summary: '当前场景状态（相机、物件列表）' },
  { name: 'set_camera', args: '{yaw?, pitch?, zoom?, position?, target?}', summary: '设置机位' },
  { name: 'set_view', args: '{preset:"全景|中景|特写|俯拍|仰拍|平视"}', summary: '叙事景别一键机位' },
  { name: 'focus_camera', args: '{scope:"scene|selection", label?}', summary: '把相机对到场景/选中物件' },
  { name: 'camera_mark_save', args: '{label?}', summary: '保存当前机位（机位书签）' },
  { name: 'camera_mark_list', args: '{}', summary: '列出机位书签' },
  { name: 'camera_mark_jump', args: '{id}', summary: '跳回某个机位' },
  { name: 'select_property', args: '{label}', summary: '选中某个物件' },
  { name: 'hide_selection', args: '{visible?}', summary: '隐藏/显示选中物件' },
  { name: 'delete_selection', args: '{}', summary: '删除选中物件' },
  { name: 'transform', args: '{x?, y?, z?, ry?, s?}', summary: '移动/旋转/缩放选中物件' },
  { name: 'separate_selection', args: '{}', summary: '分离选中物件' },
  { name: 'undo', args: '{}', summary: '撤销' },
  { name: 'redo', args: '{}', summary: '重做' },
  { name: 'import_scene', args: '{name}', summary: '载入本地场景（.ply/.splat）' },
  { name: 'import_url', args: '{url}', summary: '从 URL 载入场景' },
  { name: 'capture_viewport', args: '{}', summary: '渲染当前视口并保存成片' },
  { name: 'clear_scene', args: '{}', summary: '清空场景' },
  { name: 'play_rig', args: '{kind:"drone|phone|orbit"}', summary: '播放运镜' },
  { name: 'shoot_shotlist', args: '{}', summary: '按电商镜头表逐镜拍摄并出图' },
]
