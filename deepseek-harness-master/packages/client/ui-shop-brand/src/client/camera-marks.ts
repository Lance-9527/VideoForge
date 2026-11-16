/**
 * 掌柜 AI · **机位书签**（对应 openVideoForge 工作台的「＋保存机位」）。
 *
 * ★ 为什么要有这个：openvideoforge 的操作方法里，**保存机位**是核心动作 ——
 *   「底部点『中景』→ 视角自动收到叙事景别；觉得构好图了，点『＋保存机位』把这个角度存下来，
 *   以后随时跳回」。
 *   掌柜 AI 的 splat 工作台原来只有"一次性"的景别快捷条，构好的角度留不住。
 *
 * 存储：localStorage（客户端侧 —— 引擎也在客户端，两边同一个上下文）。
 *
 * @module
 */

/**
 * 一个书签。
 *
 * ★ 为什么存 `distance` 而不是 `zoom`：
 *   引擎的 `getCamera()` 给的是 **orbit 模型**（yaw/pitch/distance/target），
 *   `distance` 是相机到目标的**距离（米）**；
 *   而 `lookFrom(yaw, pitch, zoom)` 的 `zoom` 是**倍数**（0.55=特写，1=中景）。
 *   两者不是一回事 —— 存错了跳回来就丢景别。
 *   所以书签存 orbit 的四元组，跳回时用 `setCamera()` 精确还原 ✓
 */
export interface CameraMark {
  readonly id: string
  readonly label: string
  /** 方位角° */
  readonly yaw: number
  /** 俯仰角° */
  readonly pitch: number
  /** 相机到目标的距离（米） */
  readonly distance: number
  /** 目标点 */
  readonly target: readonly [number, number, number]
  /** 保存时间（ISO）。 */
  readonly createdAt: string
}

/**
 * 由距离粗略推出景别标签（只给人看，不参与还原）。
 * @param distance - 距离（米）。
 * @returns 景别名。
 */
export function cameraSizeLabel(distance: number): string {
  if (distance < 4) { return '特写' }
  if (distance < 9) { return '近景' }
  if (distance < 18) { return '中景' }
  if (distance < 32) { return '全景' }
  return '远景'
}

/** localStorage 键。 */
const STORAGE_KEY = 'shop.cameraMarks'

/**
 * 读出全部书签。
 * @returns 书签列表（新的在后）；坏了就返回空数组。
 */
export function readCameraMarks(): CameraMark[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (raw === null || raw.trim() === '') { return [] }
    const parsed = JSON.parse(raw) as CameraMark[]
    if (!Array.isArray(parsed)) { return [] }
    // ★ 兼容旧格式：早期存的是 yaw/pitch/zoom，没有 distance/target ——
    //   这种记录会让「距离.toFixed」崩掉，整个 overlay 一起挂。直接丢掉。
    return parsed.filter((m) => typeof m?.id === 'string'
      && typeof m?.distance === 'number' && Number.isFinite(m.distance)
      && Array.isArray(m?.target) && m.target.length === 3)
  } catch {
    return []
  }
}

/**
 * 写回全部书签。
 * @param marks - 新的书签列表。
 */
export function writeCameraMarks(marks: readonly CameraMark[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(marks))
  } catch {
    /* 存不了就算了 */
  }
}

/**
 * 新增一个书签（同名会加序号，不覆盖）。
 * @param label - 机位名（用户给的）。
 * @param yaw - 方位角。
 * @param pitch - 俯仰角。
 * @param distance - 相机到目标的距离（米）。
 * @param target - 目标点。
 * @returns 新增后的书签列表。
 */
export function addCameraMark(
  label: string,
  yaw: number,
  pitch: number,
  distance: number,
  target: readonly [number, number, number],
): CameraMark[] {
  const marks = readCameraMarks()
  const base = label.trim() === '' ? '机位' : label.trim()
  const sameName = marks.filter((m) => m.label === base).length
  const name = sameName === 0 ? base : `${base} ${sameName + 1}`
  const mark: CameraMark = {
    id: `${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`,
    label: name,
    yaw,
    pitch,
    distance,
    target: [target[0], target[1], target[2]],
    createdAt: new Date().toISOString(),
  }
  const next = [...marks, mark]
  writeCameraMarks(next)
  return next
}

/**
 * 删一个书签。
 * @param id - 书签 id。
 * @returns 删除后的列表。
 */
export function removeCameraMark(id: string): CameraMark[] {
  const next = readCameraMarks().filter((m) => m.id !== id)
  writeCameraMarks(next)
  return next
}
