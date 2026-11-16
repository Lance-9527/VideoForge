/**
 * 掌柜 AI · **拍摄任务**（电商方案 → 高斯泼溅场景机位）。
 *
 * ★ 这一层是「电商」和「高斯泼溅」真正的接缝：
 *
 *   电商 agent 出的**镜头表**（beat/景别/动作/时长）
 *        ↓  planShotCameras()
 *   splat 场景的**机位序列**（yaw/pitch/zoom + 运镜）
 *        ↓  场景里逐镜摆机位 → captureStill()
 *   成片/帧序列
 *
 * 为什么用 localStorage 传：电商面板和场景工作台**都在客户端**（同一个浏览器上下文），
 * 而 agent 在宿主侧 —— 所以走客户端自己的存储最直接，也不用改冻结的 sidecar。
 *
 * @module
 */

/** 电商方案里的一个镜头（与 sidecar 的 scenes schema 对齐）。 */
export interface ShootShot {
  readonly scene_number: number
  readonly title: string
  /** 节拍（抛需求 / 给方案 / 展示细节 / 促单 …）。 */
  readonly beat?: string | undefined
  readonly duration_seconds?: number | undefined
  readonly actions?: readonly string[] | undefined
}

/** 一次拍摄任务。 */
export interface ShootTask {
  readonly product: string
  readonly platform: string
  readonly shots: readonly ShootShot[]
  /** 建任务的时间（ISO）。 */
  readonly createdAt: string
  /** 拍完后回填的帧文件名（sidecar 的成片目录）。 */
  readonly frames?: readonly string[]
}

/** 一个机位。 */
export interface CameraPlan {
  readonly shotNumber: number
  readonly label: string
  /** 方位角° / 俯仰角° / 距离倍数 —— 与 `StudioHandle.lookFrom` 同口径。 */
  readonly yaw: number
  readonly pitch: number
  readonly zoom: number
  /** 要不要走环绕运镜。 */
  readonly orbit: boolean
}

/** localStorage 键。 */
const STORAGE_KEY = 'shop.shootTask'

/**
 * 节拍 → 机位预设。
 *
 * ★ 电商的四段式节拍天然对应四种景别，这里就是那张映射表；
 *   机位数值与 `ShotScene` 的 CAMERA_PRESETS 同一套（yaw/pitch/zoom）。
 */
const BEAT_CAMERA: Record<string, { yaw: number; pitch: number; zoom: number; orbit: boolean }> = {
  // ── 默认四段（不指定品类时）──
  抛需求: { yaw: 40, pitch: 22, zoom: 0.8, orbit: false },
  给方案: { yaw: 55, pitch: 16, zoom: 0.55, orbit: false },
  验信任: { yaw: 60, pitch: 12, zoom: 0.45, orbit: false },
  催下单: { yaw: 40, pitch: 6, zoom: 0.9, orbit: false },
  // ── 品类五段（指定品类时）──
  钩子: { yaw: 50, pitch: 14, zoom: 0.6, orbit: false },
  痛点: { yaw: 35, pitch: 20, zoom: 0.7, orbit: false },
  '痛点/场景': { yaw: 35, pitch: 20, zoom: 0.7, orbit: false },
  演示: { yaw: 45, pitch: 18, zoom: 0.6, orbit: true },
  证据: { yaw: 63, pitch: 13, zoom: 0.46, orbit: false },
  CTA: { yaw: 43, pitch: 7, zoom: 0.92, orbit: false },
}

/** 兜底机位（节拍不认识时）。 */
const FALLBACK_CAMERA = { yaw: 40, pitch: 18, zoom: 0.85, orbit: false }

/**
 * 把镜头表翻译成机位序列。
 *
 * @param shots - 电商方案的镜头表。
 * @returns 每个镜头一个机位（顺序与镜头表一致）。
 */
export function planShotCameras(shots: readonly ShootShot[]): CameraPlan[] {
  return shots.map((shot) => {
    const beat = (shot.beat ?? '').trim()
    const preset = BEAT_CAMERA[beat] ?? FALLBACK_CAMERA
    return {
      shotNumber: shot.scene_number,
      label: shot.title,
      yaw: preset.yaw,
      pitch: preset.pitch,
      zoom: preset.zoom,
      // 「展示细节」走环绕：splat 场景的独门优势 —— 同一场景环绕，商品绝对一致
      orbit: preset.orbit,
    }
  })
}

/** 写入拍摄任务。 */
export function writeShootTask(task: ShootTask): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(task))
  } catch {
    /* 存不了就算了（隐私模式）：不影响主流程 */
  }
}

/**
 * 读取拍摄任务。
 * @returns 任务；没有或坏了就返回 null。
 */
export function readShootTask(): ShootTask | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (raw === null || raw.trim() === '') { return null }
    const parsed = JSON.parse(raw) as ShootTask
    if (!Array.isArray(parsed.shots) || parsed.shots.length === 0) { return null }
    return parsed
  } catch {
    return null
  }
}

/** 清掉拍摄任务。 */
export function clearShootTask(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* 忽略 */
  }
}

/**
 * 把拍好的帧回填到任务里（电商那边能看到"片子拍好了"）。
 * @param frames - 帧文件名列表。
 */
export function attachFrames(frames: readonly string[]): ShootTask | null {
  const task = readShootTask()
  if (task === null) { return null }
  const next: ShootTask = { ...task, frames }
  writeShootTask(next)
  return next
}
