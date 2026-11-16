/**
 * 掌柜 AI · **成片链**（电商方案 → 可发布的 mp4）。
 *
 * ★ 为什么这是主线（2026-11-04 审视后的改道）：
 *   用户要的是一个**电商 agent 应用**，产出是**内容**。
 *   之前把高斯泼溅当成产出主体，结果只能出几张静帧 —— 没有商品、没有视频、没有字幕配音，
 *   离「能发布」差得很远；而服务端的成片零件其实**全部现成**。
 *
 *   这条链在冻结 sidecar 上实测通过（2026-11-04 12:40）：
 *     POST /api/studio/splat/exports  → 图片落盘并返回**绝对路径**
 *     POST /api/projects               → 建项目
 *     POST /api/shots                 → 建镜头（时长/三层提示词/画幅）
 *     PATCH /api/shots/{sid}          → 把上面那张图的绝对路径挂上去
 *     POST /api/projects/{pid}/compose {with_voice:true, auto_images:false}
 *                                  → 后台合成（配音用 **免费** Edge TTS，字幕烧录）
 *     GET  /api/projects/{pid}/compose/status → 轮询进度
 *     POST /api/ecommerce/ai-label {apply:true, video_path} → **三层 AI 标识**（L1/L2/L3）
 *
 *   实测：4 个镜头 → 42 秒产出 final.mp4（34.53 秒、65.9MB），
 *   再 ai-label 产出 final.ailabel.mp4（40.6MB，1280×692，
 *   注释「AI生成内容标识…依据《人工智能生成合成内容标识办法》/GB 45438-2025」）。
 *
 *   **零付费**：`auto_images:false`（不用图像模型补画面）、配音走免费 Edge TTS。
 *
 * @module
 */

/** 一镜的画面素材。 */
export interface ShotImage {
  /** 图片文件名（会落到成片目录）。 */
  readonly filename: string
  /** 图片字节。 */
  readonly bytes: Uint8Array
}

/** 一个镜头（来自电商方案）。 */
export interface PlanShot {
  readonly scene_number: number
  readonly title: string
  readonly beat: string
  readonly duration_seconds: number
  readonly actions?: readonly string[] | undefined
  /**
   * 台词 —— ★ 必须接上，否则成片既没字幕也没配音。
   *
   * sidecar 的 `core/dialogue.timeline_lines()` 以 `layer2_timeline` 里的 `dialogue`
   * 为**唯一**数据源；字段缺失时它会静默按「这一镜没有台词」处理
   * （实测：不传 dialogue ⇒ 成片无字幕、无配音音轨，2026-11-04）。
   */
  readonly dialogues?: readonly {
    readonly character?: string | undefined
    readonly text?: string | undefined
  }[] | undefined
}

/** 成片进度。 */
export interface ProduceProgress {
  readonly stage: string
  readonly percent: number
  readonly message: string
}

/** 成片结果。 */
export interface ProduceResult {
  /** 项目 id。 */
  readonly projectId: string
  /** 成片绝对路径。 */
  readonly videoPath: string
  /** 带 AI 标识的成片绝对路径（标识失败时等于 videoPath）。 */
  readonly labelledPath: string
  /** 时长（秒）。 */
  readonly duration: number
  /** 片段数。 */
  readonly clipCount: number
  /** sidecar 给的警告（例如"多镜画面几乎相同"）。 */
  /** 真警告。 */
  readonly warnings: readonly string[]
  /** 制作记录（每一步做了什么）。 */
  readonly notes: readonly string[]
}

/**
 * sidecar 返回的包装（VideoForge 的成功响应都是 `{code, message, data}`）。
 */
interface Wrapped<T> {
  readonly code?: string
  readonly message?: string
  readonly data?: T
}

/**
 * 调一次 sidecar，解包 `{code,message,data}`。
 * @param base - sidecar 地址。
 * @param path - 路径。
 * @param init - fetch 参数。
 * @returns data 字段。
 */
async function call<T>(base: string, path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      ...(init.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(init.headers ?? {}),
    },
  })
  const text = await response.text()
  let payload: unknown
  try {
    payload = JSON.parse(text)
  } catch {
    throw new Error(`sidecar 返回了非 JSON（HTTP ${response.status}）：${text.slice(0, 200)}`)
  }
  const wrapped = payload as Wrapped<T>
  // VideoForge 的失败响应有两种形状：{detail:{code,message}} 与 {code,message}
  const detail = (payload as { detail?: { message?: string } }).detail
  const message = detail?.message ?? wrapped.message
  if (!response.ok || (wrapped.code !== undefined && wrapped.code !== '000000')) {
    throw new Error(message ?? `sidecar HTTP ${response.status}`)
  }
  return (wrapped.data ?? payload) as T
}

/**
 * 把一张图片交给 sidecar 落盘。
 * @param base - sidecar 地址。
 * @param image - 图片。
 * @returns 绝对路径。
 */
export async function uploadShotImage(base: string, image: ShotImage): Promise<string> {
  // base64 要分块转，否则大图会爆调用栈
  let binary = ''
  const chunk = 0x8000
  for (let index = 0; index < image.bytes.length; index += chunk) {
    binary += String.fromCharCode(...image.bytes.subarray(index, index + chunk))
  }
  const response = await fetch(`${base}/api/studio/splat/exports`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      filename: image.filename,
      data: btoa(binary),
      media_type: 'image/png',
    }),
  })
  const payload = await response.json() as { export?: { path?: string }; ok?: boolean }
  const path = payload.export?.path
  if (path === undefined) { throw new Error('图片落盘失败（sidecar 没返回路径）') }
  return path
}

/**
 * 把方案里的镜头拼成 sidecar 的第 2 层时间线（动作 + 台词）。
 *
 * ★ 台词必须落在 `dialogue` 字段（结构 `{character,text,emotion}` 或裸字符串）——
 *   sidecar 的 `core/dialogue.timeline_lines()` 只认它，缺了就静默按"没有台词"处理。
 *
 * @param shot - 方案里的镜头。
 * @returns layer2_timeline 数组。
 */
/**
 * 按节拍生成一句旁白。
 *
 * ★ 为什么需要它：sidecar 的**电商方案接口不产台词**（`scenes[].dialogues` 通常为空），
 *   而配音/字幕只认 timeline 里的 `dialogue` ——
 *   不给台词 ⇒ 成片「配音失败：没有可配音的文本」+ 没有字幕（2026-11-04 实测）。
 *
 *   sidecar 自己有「旁白兜底」但默认关闭（理由：自动念剧情会影响观感）。
 *   这里做成**显式、按节拍的模板旁白**，用户看得见、可改，
 *   也不依赖 sidecar 的隐藏开关。
 *
 * @param product - 商品名。
 * @param shot - 镜头。
 * @returns 旁白文本。
 */
function generateVoiceover(product: string, shot: PlanShot): string {
  const name = product.trim() === '' ? '这件商品' : product.trim()
  const selling = (shot.actions ?? []).filter((action) => action !== '').join('、')
  switch (shot.beat) {
    case '抛需求':
      return `${name}，${selling === '' ? '这是很多人都会遇到的问题' : selling}。`
    case '痛点':
      return `如果${selling === '' ? '你也遇到过这个问题' : selling}，这条一定要看完。`
    case '痛点/场景':
      return `${selling === '' ? '先说场景' : selling}，问题就出在这儿。`
    case '钩子':
      return `${name}，三秒告诉你它凭什么。`
    case '给方案':
      return `${name}的做法是：${selling === '' ? '把最关键的一处做对' : selling}。`
    case '演示':
      return `实际用起来是这样：${selling === '' ? '上手就能感觉到差别' : selling}。`
    case '验信任':
      return `实测数据在这里，${selling === '' ? '参数都摆出来' : selling}。`
    case '证据':
      return `这是实测结果：${selling === '' ? '参数可查' : selling}。`
    case '催下单':
      return `需要的话现在就下单，${name}的尺码表在下面。`
    case 'CTA':
      return `点击下方链接，${name}就在那里。`
    default:
      return selling === '' ? `${name}，值得看一眼。` : `${name}：${selling}。`
  }
}

/**
 * 把方案里的镜头拼成 sidecar 的第 2 层时间线（动作 + 台词）。
 *
 * ★ 台词必须落在 `dialogue` 字段（结构 `{character,text,emotion}` 或裸字符串）——
 *   sidecar 的 `core/dialogue.timeline_lines()` 只认它，缺了就静默按"没有台词"处理。
 *
 * @param product - 商品名（用于生成旁白）。
 * @param shot - 方案里的镜头。
 * @returns layer2_timeline 数组。
 */
function buildTimeline(product: string, shot: PlanShot): Array<Record<string, unknown>> {
  const duration = Math.max(1, Math.round(shot.duration_seconds))
  const actions = shot.actions ?? []
  const lines: Array<Record<string, unknown>> = []
  const spoken = (shot.dialogues ?? [])
    .map((dialogue) => ({
      character: (dialogue.character ?? '').trim(),
      text: (dialogue.text ?? '').trim(),
    }))
    .filter((dialogue) => dialogue.text !== '')
  // 没有台词就按节拍生成旁白 —— 否则成片既没配音也没字幕
  const dialogues = spoken.length > 0
    ? spoken
    : [{ character: '主播', text: generateVoiceover(product, shot) }]
  // 台词按镜头时长均分到时间线上
  const step = Math.max(1, Math.floor(duration / dialogues.length))
  dialogues.forEach((dialogue, index) => {
    const start = index * step
    const end = index === dialogues.length - 1 ? duration : Math.min(duration, start + step)
    lines.push({
      start,
      end: Math.max(start + 1, end),
      action: actions.join('；'),
      camera: 'static',
      expression: '',
      props_used: [],
      dialogue: {
        character: dialogue.character,
        text: dialogue.text,
        emotion: '自然',
      },
    })
  })
  return lines
}

/**
 * 跑完整条成片链。
 *
 * @param options - 参数。
 * @returns 成片结果。
 */
export async function produceVideo(options: {
  /** sidecar 地址。 */
  readonly base: string
  /** 商品标题。 */
  readonly title: string
  /** 商品名（旁白里用）。 */
  readonly product?: string | undefined
  /** 方案里的镜头表。 */
  readonly shots: readonly PlanShot[]
  /** 每镜的画面（按顺序；数量不足的镜头用上一张）。 */
  readonly images: readonly ShotImage[]
  /** 画幅。 */
  readonly aspect?: string
  /** 进度回调。 */
  readonly onProgress?: (progress: ProduceProgress) => void
}): Promise<ProduceResult> {
  const { base, shots, images } = options
  const report = options.onProgress ?? (() => { /* 忽略 */ })
  report({ stage: '项目', percent: 0.02, message: '建项目…' })

  const project = await call<{ id?: string }>(base, '/api/projects', {
    method: 'POST',
    body: JSON.stringify({ title: options.title || '掌柜AI 成片', kind: 'video' }),
  })
  const projectId = project.id
  if (projectId === undefined) { throw new Error('建项目失败（sidecar 没返回 id）') }

  // 逐镜：建镜头 + 挂图
  const imagePaths: string[] = []
  for (let index = 0; index < shots.length; index += 1) {
    const image = images[Math.min(index, images.length - 1)]
    if (image !== undefined) {
      report({ stage: '素材', percent: 0.05 + (index / Math.max(1, shots.length)) * 0.15, message: `第 ${index + 1} 镜素材入库…` })
      imagePaths.push(await uploadShotImage(base, image))
    }
  }

  const created: { id?: string }[] = []
  for (let index = 0; index < shots.length; index += 1) {
    const shot = shots[index]!
    const overview = [
      shot.title,
      shot.actions !== undefined && shot.actions.length > 0 ? `动作：${shot.actions.join('；')}` : '',
      shot.beat !== '' ? `节拍：${shot.beat}` : '',
    ].filter((part) => part !== '').join('\n')
    const createdShot = await call<{ id?: string }>(base, '/api/shots', {
      method: 'POST',
      body: JSON.stringify({
        project_id: projectId,
        order_index: index,
        duration_seconds: Math.max(1, Math.round(shot.duration_seconds)),
        layer1_overview: overview,
        // ★ 台词写进 layer2_timeline —— sidecar 只认这里的 `dialogue`（2026-11-04 实测：
        //   不传 ⇒ 成片无字幕、无配音）。
        layer2_timeline: buildTimeline(options.product ?? options.title, shot),
        model_provider: 'kling',
        aspect_ratio: options.aspect ?? '9:16',
        resolution: '1080p',
      }),
    })
    created.push(createdShot)
    const path = imagePaths[index]
    if (path !== undefined && createdShot.id !== undefined) {
      await call(base, `/api/shots/${createdShot.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ image_path: path }),
      })
    }
  }

  // ── 后期流水线 ──
  // ★ 为什么用 pipeline 而不是 compose（2026-11-04 实测）：
  //   compose 的字幕依赖**配音 provider 返回的逐字时间轴**，而免费的 Edge TTS 不返回
  //   ⇒ 成片有配音、**没有字幕**（sidecar 警告「这一部片子没有字幕」）。
  //   pipeline 的 subtitle 步骤按**真实片段时长**排轴 ⇒ 字幕正常（实测「4 条字幕」）。
  //   所以主链路走 pipeline：base → voice → subtitle → aspect → trim → finalize。
  const steps: readonly { key: string; label: string; params?: Record<string, unknown> }[] = [
    { key: 'base', label: '接底片', params: { transition: 'fade', transition_sec: 0.4, auto_images: false } },
    { key: 'voice', label: '配音' },
    { key: 'subtitle', label: '字幕' },
    { key: 'aspect', label: '画幅' },
    { key: 'trim', label: '裁时长' },
  ]
  /** 步骤完成记录（不是警告）。 */
  const notes: string[] = []
  /** 真警告（失败/风险）。 */
  const warnings: string[] = []
  let stepIndex = 0
  for (const step of steps) {
    report({
      stage: step.label,
      percent: 0.25 + (stepIndex / (steps.length + 1)) * 0.6,
      message: `${step.label}…`,
    })
    await call(base, `/api/projects/${projectId}/pipeline/run`, {
      method: 'POST',
      body: JSON.stringify({ step: step.key, params: step.params ?? {} }),
    })
    // 轮询到这一步结束
    for (let attempt = 0; attempt < 90; attempt += 1) {
      await new Promise((resolve) => { setTimeout(resolve, 2500) })
      const status = await call<{
        running?: boolean
        percent?: number
        message?: string
        error?: string
        result?: unknown
      }>(base, `/api/projects/${projectId}/pipeline/status`)
      if (!status.running) {
        if (status.error !== undefined && status.error !== '') {
          throw new Error(`${step.label}失败：${status.error}`)
        }
        break
      }
      report({
        stage: step.label,
        percent: 0.25 + (stepIndex / (steps.length + 1)) * 0.6,
        message: status.message ?? `${step.label}…`,
      })
    }
    stepIndex += 1
  }

  // finalize（首次可能连接被中止 —— 后台其实已经做完了，所以重试一次）
  report({ stage: '合成成片', percent: 0.88, message: '输出成片…' })
  let finalized: { output_path?: string; duration?: number; clip_count?: number } | null = null
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const done = await call<{ output_path?: string; duration?: number; clip_count?: number }>(
        base, `/api/projects/${projectId}/pipeline/finalize`, { method: 'POST', body: JSON.stringify({}) },
      )
      finalized = done
      break
    } catch (error) {
      if (attempt === 2) { throw error }
      await new Promise((resolve) => { setTimeout(resolve, 4000) })
    }
  }
  const outcome = finalized ?? {}
  const videoPath = outcome.output_path ?? ''
  if (videoPath === '') { throw new Error('流水线跑完了，但没拿到成片路径') }

  // 时长在 pipeline/status 的 result 里（finalize 本身不返回）
  let duration = outcome.duration ?? 0
  if (duration === 0) {
    try {
      const status = await call<{ result?: { duration?: number } }>(
        base, `/api/projects/${projectId}/pipeline/status`,
      )
      duration = status.result?.duration ?? 0
    } catch {
      /* 拿不到时长不影响成片 */
    }
  }

  // 收集 sidecar 的诊断（接缝/节奏/电影感门），这些对用户很有用
  try {
    const pipeline = await call<{ steps?: Array<{ note?: string; error?: string }> }>(
      base, `/api/projects/${projectId}/pipeline`,
    )
    for (const item of pipeline.steps ?? []) {
      if (item.note !== undefined && item.note !== '') { notes.push(item.note) }
      if (item.error !== undefined && item.error !== '') { warnings.push(item.error) }
    }
  } catch {
    /* 诊断拿不到不影响成片 */
  }


  // AI 标识（合规闭环）
  report({ stage: '标识', percent: 0.94, message: '加 AI 生成标识…' })
  let labelledPath = videoPath
  try {
    const labelled = await call<{ out_path?: string }>(base, '/api/ecommerce/ai-label', {
      method: 'POST',
      body: JSON.stringify({ apply: true, video_path: videoPath }),
    })
    if (labelled.out_path !== undefined) { labelledPath = labelled.out_path }
  } catch (error) {
    // 标识失败不该让整条链失败 —— 成片仍然可用（但要如实告知）
    report({ stage: '标识', percent: 0.98, message: `AI 标识没加上：${error instanceof Error ? error.message : String(error)}` })
  }
  report({ stage: '完成', percent: 1, message: '成片完成' })

  return {
    projectId,
    videoPath,
    labelledPath,
    duration,
    clipCount: outcome.clip_count ?? shots.length,
    warnings,
    notes,
  }
}
