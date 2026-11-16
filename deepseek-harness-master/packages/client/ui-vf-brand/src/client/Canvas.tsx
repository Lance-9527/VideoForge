/**
 * VideoForge · 无限画布 v2（2026-11-01 重做）
 *
 * ## 为什么重做
 * 上一版能显示节点，但**正常人用不了**：
 *  · 进页面先撞一句「先从左边选一个项目」的死胡同；
 *  · 滚轮缩放不以光标为中心、没有「适应画布」，缩没了就找不回来；
 *  · 只能单选 + 拖，没有框选、多选、删除键、撤销、复制；
 *  · 拖完要手动点「保存」，忘了就丢；没有任何保存状态提示；
 *  · 缩略图下面的标题是 `shots-4c1608` 这种 id，看不出是哪一镜。
 *
 * 这一版按成熟无限画布（Figma / Krea / LibTV）的**肌肉记忆**重画：
 *  滚轮=以光标为中心缩放；空格/中键/抓手=平移；拖空白=框选；
 *  Shift 点选、Ctrl+A 全选、Delete 删、Ctrl+Z 撤销、Ctrl+D 复制、
 *  方向键微调；右下角缩放控件 + 小地图；改完自动保存并显示状态。
 *
 * ## 三条红线（与上一版一致，不破）
 * 1. **画布是视图，不是新真源**：节点引用 shots / characters / scenes 的真实 id，
 *    只有位置、尺寸、连线存在 `canvas_nodes` 表 ⇒ 库里那些表仍是唯一真源。
 * 2. **不新增运行时**：数据走 sidecar(8765) 的 REST，界面是本包里的一段 React。
 * 3. **不绕过花钱闸门**：节点上的 ⚡ 只是把一句话发给 agent，真正干活仍走 vf_* 链。
 *
 * ## 渲染方式：节点用 DOM，连线用 SVG
 * （foreignObject 塞媒体在 Electron 里是黑框，实测过，别再试。）
 *
 * @module
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react'

/** sidecar 地址（与资产/后期面板同一套）。 */
const SIDECAR = 'http://127.0.0.1:8765'

/** 节点卡片尺寸（缩略图 16:9 + 底栏）。 */
const NODE_W = 216
const NODE_H = 176
const THUMB_H = NODE_W * 9 / 16

const MIN_K = 0.25
const MAX_K = 2.5

/** 配色（与面板其余部分同一套暗色 token）。 */
const C = {
  bg: '#0b0b0f',
  panel: '#121218',
  panel2: '#17171e',
  card: '#16161d',
  line: '#26262e',
  text: '#eef0f4',
  dim: '#9a9aa6',
  dimmer: '#64646d',
  accent: '#7ac130',
} as const

/** 节点类型的外观。 */
const KIND: Record<string, { readonly label: string; readonly color: string; readonly glyph: string }> = {
  shots: { label: '分镜', color: '#7ac130', glyph: '▤' },
  characters: { label: '角色', color: '#5aa9ff', glyph: '☺' },
  scenes: { label: '场景', color: '#f0a13c', glyph: '⌂' },
  note: { label: '便签', color: '#b39ddb', glyph: '✎' },
}

/** 画布节点（与后端 canvas_nodes 表一一对应）。 */
interface CanvasNode {
  readonly id: string
  readonly kind: string
  readonly ref_type: string
  readonly ref_id: string
  readonly title: string
  readonly x: number
  readonly y: number
  readonly w: number
  readonly h: number
  readonly z: number
  readonly meta: Record<string, unknown>
  readonly edges: readonly { readonly to: string }[]
}

/** 还没上画布的对象。 */
interface CanvasCandidate {
  readonly kind: string
  readonly ref_id: string
  readonly title: string
  readonly thumb: string
}

/** 一条连线。 */
interface EdgeRef {
  readonly from: string
  readonly to: string
}

/** 视角（世界坐标 → 屏幕坐标：screen = world * k + offset）。 */
interface View {
  readonly x: number
  readonly y: number
  readonly k: number
}

/** 一个项目（画布为空时选项目用）。 */
interface ProjectBrief {
  readonly id?: string
  readonly title?: string
}

/** 分镜补充信息（把 `shots-xxxx` 变成「镜01 · 15s」）。 */
interface ShotBrief {
  readonly id?: string
  readonly order_index?: number
  readonly duration_seconds?: number
  readonly layer1_overview?: string
}

const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v))

/** 把 sidecar 的路径变成 <img>/<video> 能吃的 URL。 */
function mediaUrl(path: string | null | undefined): string | null {
  const value = String(path ?? '').trim()
  if (value === '') return null
  if (/^https?:/u.test(value)) return value
  return `${SIDECAR}/api/media?path=${encodeURIComponent(value)}`
}

/** 拉一个 JSON（失败返回 null，绝不抛）。 */
async function fetchJson<T>(path: string, init?: RequestInit): Promise<T | null> {
  try {
    const response = await fetch(`${SIDECAR}${path}`, init)
    if (!response.ok) return null
    const body = await response.json() as { data?: unknown }
    return (body.data ?? body) as T
  } catch {
    return null
  }
}

/** 标题是不是「后端兜底 id」（`shots-4c1608`），是就换成类型名。 */
function looksLikeId(title: string): boolean {
  return /^(shots|characters|scenes)-[0-9a-z]{4,10}$/iu.test(title.trim())
}

/** 一组节点的外接矩形（世界坐标）。 */
function boundsOf(list: readonly CanvasNode[]): { x: number; y: number; w: number; h: number } {
  if (list.length === 0) return { x: 0, y: 0, w: 0, h: 0 }
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity
  for (const node of list) {
    x0 = Math.min(x0, node.x)
    y0 = Math.min(y0, node.y)
    x1 = Math.max(x1, node.x + node.w)
    y1 = Math.max(y1, node.y + node.h)
  }
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 }
}

/** 把一块世界坐标区域装进视口。 */
function fitView(box: { x: number; y: number; w: number; h: number }, el: HTMLDivElement | null): View {
  const width = el?.clientWidth ?? 900
  const height = el?.clientHeight ?? 600
  if (box.w <= 0 || box.h <= 0) return { x: 72, y: 48, k: 1 }
  // 适应画布只**缩小**不放大：放大了反而让人以为丢了东西。
  const k = clamp(Math.min((width - 140) / box.w, (height - 140) / box.h), MIN_K, 1)
  return {
    x: (width - box.w * k) / 2 - box.x * k,
    y: (height - box.h * k) / 2 - box.y * k,
    k,
  }
}

/** 两个节点是否（带 8px 余量）相交。 */
function overlaps(a: CanvasNode, b: CanvasNode): boolean {
  const margin = 8
  return a.x < b.x + b.w + margin && a.x + a.w + margin > b.x
    && a.y < b.y + b.h + margin && a.y + a.h + margin > b.y
}

/**
 * 老数据的节点高 132、新卡片高 176 ⇒ 原来「132+28」的竖向间距现在会叠在一起。
 * 只把**真正相交**的节点往下挪，不动用户有意分开排的布局。
 */
function declutter(list: readonly CanvasNode[]): { readonly list: readonly CanvasNode[]; readonly moved: boolean } {
  const sorted = [...list].sort((a, b) => (a.y - b.y) || (a.x - b.x))
  const placed: CanvasNode[] = []
  let moved = false
  for (const node of sorted) {
    let candidate = node
    let guard = 0
    while (placed.some((other) => overlaps(other, candidate)) && guard < 80) {
      candidate = { ...candidate, y: candidate.y + 24 }
      moved = true
      guard += 1
    }
    placed.push(candidate)
  }
  return { list: placed, moved }
}

/**
 * 无限画布。
 *
 * @param props.projectId - 当前项目（`null` 时页面自己给项目列表，不再死胡同）。
 * @param props.sessionId - 这个项目绑定的会话（发消息给 agent 要用）。
 * @param props.onSend - 把一句话发进会话（由 shell 注入）。
 * @param props.onPickProject - 还没选项目时，选中一个项目进画布（由面板注入）。
 */
export function VfCanvas(props: {
  readonly projectId: string | null
  readonly sessionId: string | null
  readonly onSend?: ((sessionId: string, text: string) => void) | undefined
  readonly onPickProject?: ((id: string, title: string) => void) | undefined
}) {
  const [nodes, setNodes] = useState<readonly CanvasNode[]>([])
  const [candidates, setCandidates] = useState<readonly CanvasCandidate[]>([])
  const [view, setView] = useState<View>({ x: 72, y: 48, k: 1 })
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set<string>())
  const [edgePicked, setEdgePicked] = useState<string | null>(null)
  const [tool, setTool] = useState<'select' | 'hand'>('select')
  const [linking, setLinking] = useState<{ from: string; x: number; y: number } | null>(null)
  const [marquee, setMarquee] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null)
  const [editing, setEditing] = useState<{ id: string; value: string } | null>(null)
  const [hover, setHover] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [showMap, setShowMap] = useState(true)
  const [search, setSearch] = useState('')
  const [shotMap, setShotMap] = useState<ReadonlyMap<string, ShotBrief>>(() => new Map())
  const [, bumpHistory] = useState(0)

  const viewportRef = useRef<HTMLDivElement | null>(null)
  const nodesRef = useRef<readonly CanvasNode[]>([])
  const pastRef = useRef<Array<readonly CanvasNode[]>>([])
  const futureRef = useRef<Array<readonly CanvasNode[]>>([])
  const panRef = useRef<{ sx: number; sy: number; ox: number; oy: number } | null>(null)
  const dragRef = useRef<{ sx: number; sy: number; origin: ReadonlyMap<string, { x: number; y: number }> } | null>(null)
  const marqueeRef = useRef<{ sx: number; sy: number } | null>(null)
  const spaceRef = useRef(false)
  const skipSaveRef = useRef(true)

  nodesRef.current = nodes

  /* ───────────────────────── 载入 ───────────────────────── */

  const load = useCallback(async () => {
    if (props.projectId === null) {
      setLoading(false)
      return
    }
    setLoading(true)
    const data = await fetchJson<{ nodes?: CanvasNode[]; candidates?: CanvasCandidate[] }>(
      `/api/projects/${encodeURIComponent(props.projectId)}/canvas`,
    )
    if (data === null) {
      setError('读不到画布数据 —— 后端（8765）没连上？')
      setLoading(false)
      return
    }
    setError(null)
    const mapped = (data.nodes ?? []).map((node) => ({
      ...node,
      w: NODE_W,
      h: NODE_H,
      meta: node.meta ?? {},
      edges: Array.isArray(node.edges) ? node.edges : [],
    }))
    // 卡片尺寸从 132 长到 176：只把**真的叠在一起**的节点挪开（老数据的间距会撞）。
    const { list, moved } = declutter(mapped)
    setNodes(list)
    setCandidates(data.candidates ?? [])
    skipSaveRef.current = !moved
    setSaveState('idle')
    setEditing(null)
    setSelected(new Set<string>())
    setLoading(false)
    if (moved) void persist(list)

    // 分镜补充信息：把 `shots-4c1608` 这种标题变成「镜01 · 15s」。
    const shots = await fetchJson<{ shots?: ShotBrief[] }>(
      `/api/projects/${encodeURIComponent(props.projectId)}/shots`,
    )
    const map = new Map<string, ShotBrief>()
    for (const shot of shots?.shots ?? []) if (typeof shot.id === 'string') map.set(shot.id, shot)
    setShotMap(map)

    // 首次进来把内容「适应画布」—— 老数据可能散在很远的地方。
    if (list.length > 0) {
      const box = boundsOf(list)
      setView(fitView(box, viewportRef.current))
    }
  }, [props.projectId])

  useEffect(() => { void load() }, [load])

  /* ───────────────────────── 保存（自动） ───────────────────────── */

  const persist = useCallback(async (list: readonly CanvasNode[]) => {
    if (props.projectId === null) return
    setSaveState('saving')
    const body = JSON.stringify({
      nodes: list.map((node) => ({
        id: node.id, kind: node.kind, ref_type: node.ref_type, ref_id: node.ref_id,
        title: node.title, x: Math.round(node.x), y: Math.round(node.y),
        w: node.w, h: node.h, z: node.z, meta: node.meta, edges: node.edges,
      })),
    })
    const ok = await fetchJson(`/api/projects/${encodeURIComponent(props.projectId)}/canvas`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body,
    })
    if (ok === null) {
      setSaveState('error')
      setError('保存失败 —— 后端没连上，改动只在这台屏幕里')
      return
    }
    setError(null)
    setSaveState('saved')
  }, [props.projectId])

  // 改动后 700ms 自动存：不需要用户记得点保存。
  useEffect(() => {
    if (props.projectId === null || loading) return
    if (skipSaveRef.current) { skipSaveRef.current = false; return }
    const timer = window.setTimeout(() => { void persist(nodesRef.current) }, 700)
    return () => window.clearTimeout(timer)
  }, [nodes, loading, persist, props.projectId])

  /* ───────────────────────── 撤销 / 重做 ───────────────────────── */

  const commit = useCallback((next: readonly CanvasNode[], record = true) => {
    if (record) {
      pastRef.current = [...pastRef.current, nodesRef.current].slice(-60)
      futureRef.current = []
    }
    setNodes(next)
    bumpHistory((v) => v + 1)
  }, [])

  const undo = useCallback(() => {
    const past = pastRef.current
    if (past.length === 0) return
    const previous = past[past.length - 1]
    if (previous === undefined) return
    pastRef.current = past.slice(0, -1)
    futureRef.current = [nodesRef.current, ...futureRef.current].slice(0, 60)
    setNodes(previous)
    bumpHistory((v) => v + 1)
  }, [])

  const redo = useCallback(() => {
    const future = futureRef.current
    if (future.length === 0) return
    const next = future[0]
    if (next === undefined) return
    futureRef.current = future.slice(1)
    pastRef.current = [...pastRef.current, nodesRef.current].slice(-60)
    setNodes(next)
    bumpHistory((v) => v + 1)
  }, [])

  /* ───────────────────────── 几何 ───────────────────────── */

  const fitAll = useCallback(() => {
    if (nodesRef.current.length === 0) {
      setView({ x: 72, y: 48, k: 1 })
      return
    }
    setView(fitView(boundsOf(nodesRef.current), viewportRef.current))
  }, [])

  /** 以屏幕上的某个点为中心缩放。 */
  const zoomAt = useCallback((factor: number, cx: number, cy: number) => {
    setView((prev) => {
      const k = clamp(prev.k * factor, MIN_K, MAX_K)
      const ratio = k / prev.k
      return { k, x: cx - (cx - prev.x) * ratio, y: cy - (cy - prev.y) * ratio }
    })
  }, [])

  const viewCenter = useCallback((): { x: number; y: number } => {
    const el = viewportRef.current
    const width = el?.clientWidth ?? 900
    const height = el?.clientHeight ?? 600
    return { x: (width / 2 - view.x) / view.k, y: (height / 2 - view.y) / view.k }
  }, [view])

  /* ───────────────────────── 节点操作 ───────────────────────── */

  const place = useCallback((candidate: CanvasCandidate) => {
    const id = `cn_${candidate.kind}_${candidate.ref_id}`
    if (nodesRef.current.some((node) => node.id === id)) return
    // 落在视口中心附近，避开已有节点（按 36px 步进找空位）。
    const center = viewCenter()
    let x = center.x - NODE_W / 2
    let y = center.y - NODE_H / 2
    for (let step = 0; step < 24; step += 1) {
      const collides = nodesRef.current.some((node) => Math.abs(node.x - x) < 28 && Math.abs(node.y - y) < 28)
      if (!collides) break
      x += 36
      y += 32
    }
    const next: CanvasNode = {
      id,
      kind: candidate.kind,
      ref_type: candidate.kind,
      ref_id: candidate.ref_id,
      title: candidate.title,
      x, y, w: NODE_W, h: NODE_H,
      z: nodesRef.current.length,
      meta: candidate.thumb === '' ? {} : { thumb: candidate.thumb },
      edges: [],
    }
    commit([...nodesRef.current, next])
    setSelected(new Set([id]))
    setCandidates((prev) => prev.filter((item) => !(item.kind === candidate.kind && item.ref_id === candidate.ref_id)))
  }, [commit, viewCenter])

  const placeAll = useCallback(() => {
    if (candidates.length === 0) return
    const start = nodesRef.current.length
    const added: CanvasNode[] = candidates.map((candidate, index) => ({
      id: `cn_${candidate.kind}_${candidate.ref_id}`,
      kind: candidate.kind,
      ref_type: candidate.kind,
      ref_id: candidate.ref_id,
      title: candidate.title,
      x: 80 + (Math.floor(index / 4)) * (NODE_W + 40),
      y: 80 + (index % 4) * (NODE_H + 30),
      w: NODE_W, h: NODE_H, z: start + index,
      meta: candidate.thumb === '' ? {} : { thumb: candidate.thumb },
      edges: [],
    }))
    commit([...nodesRef.current, ...added])
    setCandidates([])
    window.setTimeout(() => { fitAll() }, 30)
  }, [candidates, commit, fitAll])

  const tidy = useCallback(() => {
    // 按类型分列：角色 → 场景 → 分镜，列内自上而下。
    const order: Record<string, number> = { characters: 0, scenes: 1, shots: 2, note: 3 }
    const columns = new Map<number, number>()
    const sorted = [...nodesRef.current].sort((a, b) => {
      const kindDiff = (order[a.kind] ?? 9) - (order[b.kind] ?? 9)
      if (kindDiff !== 0) return kindDiff
      return a.title.localeCompare(b.title)
    })
    const next = sorted.map((node) => {
      const column = order[node.kind] ?? 9
      const row = columns.get(column) ?? 0
      columns.set(column, row + 1)
      return { ...node, x: 80 + column * (NODE_W + 48), y: 80 + row * (NODE_H + 30) }
    })
    commit(next)
    window.setTimeout(() => { fitAll() }, 30)
  }, [commit, fitAll])

  const removeSelected = useCallback(() => {
    if (selected.size === 0) return
    const next = nodesRef.current
      .filter((node) => !selected.has(node.id))
      .map((node) => ({ ...node, edges: node.edges.filter((edge) => !selected.has(edge.to)) }))
    commit(next)
    setSelected(new Set<string>())
    setEdgePicked(null)
  }, [commit, selected])

  const duplicateSelected = useCallback(() => {
    if (selected.size === 0) return
    const copies: CanvasNode[] = []
    const idMap = new Map<string, string>()
    for (const node of nodesRef.current) {
      if (!selected.has(node.id)) continue
      const id = `${node.id}_copy_${Math.random().toString(36).slice(2, 6)}`
      idMap.set(node.id, id)
      copies.push({ ...node, id, x: node.x + 36, y: node.y + 36, z: nodesRef.current.length + copies.length, edges: [] })
    }
    commit([...nodesRef.current, ...copies])
    setSelected(new Set(copies.map((node) => node.id)))
  }, [commit, selected])

  const nudge = useCallback((dx: number, dy: number) => {
    if (selected.size === 0) return
    commit(nodesRef.current.map((node) => (
      selected.has(node.id) ? { ...node, x: node.x + dx, y: node.y + dy } : node
    )))
  }, [commit, selected])

  const rename = useCallback((id: string, title: string) => {
    const clean = title.trim()
    if (clean === '') { setEditing(null); return }
    commit(nodesRef.current.map((node) => (node.id === id ? { ...node, title: clean } : node)))
    setEditing(null)
  }, [commit])

  /* ───────────────────────── 连线 ───────────────────────── */

  const edges = useMemo<readonly EdgeRef[]>(() => {
    const list: EdgeRef[] = []
    for (const node of nodes) for (const edge of node.edges) list.push({ from: node.id, to: edge.to })
    return list
  }, [nodes])

  const byId = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes])

  const connect = useCallback((from: string, to: string) => {
    if (from === to) return
    const source = nodesRef.current.find((node) => node.id === from)
    if (source === undefined) return
    if (source.edges.some((edge) => edge.to === to)) return
    commit(nodesRef.current.map((node) => (
      node.id === from ? { ...node, edges: [...node.edges, { to }] } : node
    )))
  }, [commit])

  // 连线中：跟手 + 松开落在哪个节点上就连过去。
  useEffect(() => {
    if (linking === null) return
    const onMove = (event: PointerEvent): void => {
      const rect = viewportRef.current?.getBoundingClientRect()
      if (rect === undefined) return
      setLinking({ from: linking.from, x: event.clientX - rect.left, y: event.clientY - rect.top })
    }
    const onUp = (event: PointerEvent): void => {
      const el = document.elementFromPoint(event.clientX, event.clientY)
      const target = el?.closest('[data-node-id]')?.getAttribute('data-node-id') ?? null
      if (target !== null) connect(linking.from, target)
      setLinking(null)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp, { once: true })
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [linking, connect])

  /* ───────────────────────── 指针 / 滚轮 ───────────────────────── */

  // 滚轮：以光标为中心缩放（React 的 onWheel 是 passive，必须用原生监听才能 preventDefault）。
  // ★ 依赖里必须带 projectId：没选项目时组件提前 return（视口还不存在），
  //   只依赖 zoomAt 的话这个 effect 在视口挂载后不会再跑 ⇒ 滚轮完全没反应（实测踩过）。
  useEffect(() => {
    if (props.projectId === null) return
    const el = viewportRef.current
    if (el === null) return
    const onWheel = (event: WheelEvent): void => {
      event.preventDefault()
      const rect = el.getBoundingClientRect()
      const cx = event.clientX - rect.left
      const cy = event.clientY - rect.top
      if (event.shiftKey) {
        setView((prev) => ({ ...prev, x: prev.x - event.deltaY }))
        return
      }
      const factor = Math.exp(-event.deltaY * 0.0016)
      zoomAt(factor, cx, cy)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [zoomAt, props.projectId, loading])

  // 空格 = 临时抓手；Esc = 取消选择/连线。
  useEffect(() => {
    const isTyping = (target: EventTarget | null): boolean => {
      const el = target as HTMLElement | null
      if (el === null) return false
      const tag = el.tagName
      return tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable
    }
    const onKeyDown = (event: KeyboardEvent): void => {
      if (isTyping(event.target)) return
      if (event.code === 'Space') { spaceRef.current = true; event.preventDefault(); return }
      const meta = event.ctrlKey || event.metaKey
      if (meta && event.key.toLowerCase() === 'z') {
        event.preventDefault()
        if (event.shiftKey) redo(); else undo()
        return
      }
      if (meta && event.key.toLowerCase() === 'y') { event.preventDefault(); redo(); return }
      if (meta && event.key.toLowerCase() === 'a') {
        event.preventDefault()
        setSelected(new Set(nodesRef.current.map((node) => node.id)))
        return
      }
      if (meta && event.key.toLowerCase() === 'd') { event.preventDefault(); duplicateSelected(); return }
      if (meta && event.key.toLowerCase() === 'f') { event.preventDefault(); fitAll(); return }
      if (event.key === 'Delete' || event.key === 'Backspace') {
        if (selected.size > 0 || edgePicked !== null) event.preventDefault()
        if (edgePicked !== null) {
          const [from, to] = edgePicked.split('->')
          commit(nodesRef.current.map((node) => (
            node.id === from ? { ...node, edges: node.edges.filter((edge) => edge.to !== to) } : node
          )))
          setEdgePicked(null)
          return
        }
        removeSelected()
        return
      }
      if (event.key === 'Escape') {
        setSelected(new Set<string>())
        setEdgePicked(null)
        setEditing(null)
        setLinking(null)
        return
      }
      if (event.key.startsWith('Arrow') && selected.size > 0) {
        event.preventDefault()
        const step = event.shiftKey ? 24 : 4
        if (event.key === 'ArrowLeft') nudge(-step, 0)
        if (event.key === 'ArrowRight') nudge(step, 0)
        if (event.key === 'ArrowUp') nudge(0, -step)
        if (event.key === 'ArrowDown') nudge(0, step)
      }
    }
    const onKeyUp = (event: KeyboardEvent): void => {
      if (event.code === 'Space') spaceRef.current = false
    }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
    }
  }, [commit, duplicateSelected, edgePicked, fitAll, nudge, redo, removeSelected, selected, undo])

  const onViewportPointerDown = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (event.button === 1 || spaceRef.current || tool === 'hand') {
      panRef.current = { sx: event.clientX, sy: event.clientY, ox: view.x, oy: view.y }
      event.currentTarget.setPointerCapture(event.pointerId)
      return
    }
    if (event.button !== 0) return
    const rect = event.currentTarget.getBoundingClientRect()
    marqueeRef.current = { sx: event.clientX - rect.left, sy: event.clientY - rect.top }
    setMarquee({ x0: marqueeRef.current.sx, y0: marqueeRef.current.sy, x1: marqueeRef.current.sx, y1: marqueeRef.current.sy })
    if (!event.shiftKey) { setSelected(new Set<string>()); setEdgePicked(null) }
  }

  const onViewportPointerMove = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (panRef.current !== null) {
      const pan = panRef.current
      setView((prev) => ({ ...prev, x: pan.ox + (event.clientX - pan.sx), y: pan.oy + (event.clientY - pan.sy) }))
      return
    }
    if (dragRef.current !== null) {
      const drag = dragRef.current
      const dx = (event.clientX - drag.sx) / view.k
      const dy = (event.clientY - drag.sy) / view.k
      setNodes((prev) => prev.map((node) => {
        const origin = drag.origin.get(node.id)
        if (origin === undefined) return node
        return { ...node, x: origin.x + dx, y: origin.y + dy }
      }))
      return
    }
    if (marqueeRef.current !== null) {
      const rect = event.currentTarget.getBoundingClientRect()
      const x1 = event.clientX - rect.left
      const y1 = event.clientY - rect.top
      setMarquee({ x0: marqueeRef.current.sx, y0: marqueeRef.current.sy, x1, y1 })
    }
  }

  const onViewportPointerUp = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (panRef.current !== null) {
      panRef.current = null
      return
    }
    if (dragRef.current !== null) {
      const drag = dragRef.current
      const moved = Math.abs(event.clientX - drag.sx) + Math.abs(event.clientY - drag.sy) > 3
      dragRef.current = null
      if (moved) {
        // 拖拽结束时把「拖之前」压进历史，撤销才能一步回到原位。
        pastRef.current = [...pastRef.current, nodesRef.current.map((node) => {
          const origin = drag.origin.get(node.id)
          return origin === undefined ? node : { ...node, x: origin.x, y: origin.y }
        })].slice(-60)
        futureRef.current = []
        bumpHistory((v) => v + 1)
        setNodes((prev) => [...prev])
      }
      return
    }
    if (marqueeRef.current !== null) {
      const box = marquee
      marqueeRef.current = null
      setMarquee(null)
      if (box === null) return
      const x0 = Math.min(box.x0, box.x1)
      const y0 = Math.min(box.y0, box.y1)
      const x1 = Math.max(box.x0, box.x1)
      const y1 = Math.max(box.y0, box.y1)
      if (x1 - x0 < 4 && y1 - y0 < 4) return
      // 屏幕矩形 → 世界坐标矩形
      const wx0 = (x0 - view.x) / view.k
      const wy0 = (y0 - view.y) / view.k
      const wx1 = (x1 - view.x) / view.k
      const wy1 = (y1 - view.y) / view.k
      const hit = nodesRef.current.filter((node) => (
        node.x < wx1 && node.x + node.w > wx0 && node.y < wy1 && node.y + node.h > wy0
      ))
      setSelected((prev) => {
        const next = new Set(event.shiftKey ? prev : [])
        for (const node of hit) next.add(node.id)
        return next
      })
    }
  }

  const onNodePointerDown = (event: ReactPointerEvent<HTMLDivElement>, node: CanvasNode): void => {
    event.stopPropagation()
    if (event.button !== 0) return
    setEdgePicked(null)
    const next = new Set(selected)
    if (event.shiftKey) {
      if (next.has(node.id)) next.delete(node.id); else next.add(node.id)
    } else if (!next.has(node.id)) {
      next.clear()
      next.add(node.id)
    }
    setSelected(next)
    const origin = new Map<string, { x: number; y: number }>()
    for (const item of nodesRef.current) if (next.has(item.id)) origin.set(item.id, { x: item.x, y: item.y })
    dragRef.current = { sx: event.clientX, sy: event.clientY, origin }
    event.currentTarget.setPointerCapture(event.pointerId)
    // 置顶
    setNodes((prev) => {
      const maxZ = prev.reduce((top, item) => Math.max(top, item.z), 0)
      return prev.map((item) => (item.id === node.id ? { ...item, z: maxZ + 1 } : item))
    })
  }

  const askAgent = useCallback((node: CanvasNode) => {
    if (props.sessionId === null) {
      setError('这个项目还没绑定会话 —— 先在项目上右键「绑定到当前会话」，或在对话里让它绑定')
      return
    }
    if (props.onSend === undefined) {
      setError('当前客户端没有会话通道')
      return
    }
    const kind = KIND[node.kind]?.label ?? node.kind
    props.onSend(props.sessionId, `【画布】${kind}「${titleOfNode(node, shotMap)}」：请基于它继续，我要改的是这一块。`)
    setError(null)
  }, [props, shotMap])

  /* ───────────────────────── 渲染辅助 ───────────────────────── */

  const filtered = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    if (keyword === '') return candidates
    return candidates.filter((candidate) => candidate.title.toLowerCase().includes(keyword))
  }, [candidates, search])

  /** 素材列表里的标题：分镜用「镜号」，不要露 `shots-cdc7f3` 这种 id。 */
  const candidateLabel = useCallback((candidate: CanvasCandidate): string => {
    const shot = candidate.kind === 'shots' ? shotMap.get(candidate.ref_id) : undefined
    if (shot !== undefined && typeof shot.order_index === 'number' && looksLikeId(candidate.title)) {
      return `镜${String(shot.order_index + 1).padStart(2, '0')}`
    }
    return looksLikeId(candidate.title) ? (KIND[candidate.kind]?.label ?? candidate.kind) : candidate.title
  }, [shotMap])

  const groups = useMemo(() => {
    const order = ['characters', 'scenes', 'shots']
    return order
      .map((kind) => ({ kind, items: filtered.filter((candidate) => candidate.kind === kind) }))
      .filter((group) => group.items.length > 0)
  }, [filtered])

  const mapBox = useMemo(() => boundsOf(nodes), [nodes])
  const viewportSize = viewportRef.current === null
    ? { w: 900, h: 600 }
    : { w: viewportRef.current.clientWidth, h: viewportRef.current.clientHeight }

  /* ───────────────────────── 没有项目：先选 ───────────────────────── */

  if (props.projectId === null) {
    return <VfProjectPicker onPicked={(id, title) => { props.onPickProject?.(id, title) }} />
  }

  return (
    <div style={{ display: 'flex', height: '100%', minHeight: 0, background: C.bg, color: C.text }}>
      {/* 左侧竖排工具 */}
      <div style={{
        flex: '0 0 46px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
        padding: '8px 0', borderRight: `1px solid ${C.line}`, background: C.panel,
      }}>
        <ToolButton active={tool === 'select'} label="选择（V）" glyph="➤" onClick={() => setTool('select')} />
        <ToolButton active={tool === 'hand'} label="抓手（H / 按住空格）" glyph="✋" onClick={() => setTool('hand')} />
        <div style={{ width: 22, height: 1, background: C.line, margin: '4px 0' }} />
        <ToolButton active={false} label="适应画布（Ctrl+F）" glyph="⤢" onClick={fitAll} />
        <ToolButton active={showMap} label="小地图" glyph="▭" onClick={() => setShowMap((v) => !v)} />
        <ToolButton
          active={false}
          label="撤销（Ctrl+Z）"
          glyph="↶"
          onClick={undo}
          disabled={pastRef.current.length === 0}
        />
        <ToolButton
          active={false}
          label="重做（Ctrl+Shift+Z）"
          glyph="↷"
          onClick={redo}
          disabled={futureRef.current.length === 0}
        />
      </div>

      {/* 素材库 */}
      <aside style={{
        flex: '0 0 226px', display: 'flex', flexDirection: 'column', minHeight: 0,
        borderRight: `1px solid ${C.line}`, background: C.panel,
      }}>
        <div style={{ padding: '10px 12px 8px', borderBottom: `1px solid ${C.line}` }}>
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
            <span style={{ fontSize: 12, fontWeight: 600, letterSpacing: 0.4 }}>素材库</span>
            <span style={{ marginLeft: 'auto', fontSize: 11, color: C.dimmer }}>{candidates.length} 个待摆放</span>
          </div>
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索角色 / 场景 / 分镜"
            style={{
              width: '100%', boxSizing: 'border-box', fontSize: 12, padding: '5px 8px',
              background: C.panel2, color: C.text, border: `1px solid ${C.line}`, borderRadius: 6, outline: 'none',
            }}
          />
        </div>
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '6px 8px 12px' }}>
          {loading ? (
            <div style={{ fontSize: 12, color: C.dimmer, padding: 8 }}>载入中…</div>
          ) : groups.length === 0 ? (
            <div style={{ fontSize: 12, color: C.dimmer, lineHeight: 1.8, padding: 8 }}>
              {candidates.length === 0
                ? '这个项目的角色 / 场景 / 分镜都已在画布上。'
                : '没有匹配的素材。'}
            </div>
          ) : groups.map((group) => (
            <div key={group.kind} style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 11, color: C.dimmer, padding: '4px 4px', letterSpacing: 0.6 }}>
                {KIND[group.kind]?.label ?? group.kind} · {group.items.length}
              </div>
              {group.items.map((candidate) => {
                const thumb = mediaUrl(candidate.thumb)
                return (
                  <button
                    key={`${candidate.kind}-${candidate.ref_id}`}
                    type="button"
                    onClick={() => { place(candidate) }}
                    title="点击放到画布中央"
                    style={{
                      display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left',
                      padding: '5px 6px', marginBottom: 4, borderRadius: 7, cursor: 'pointer',
                      background: 'transparent', border: '1px solid transparent', color: C.text,
                    }}
                    onMouseEnter={(event) => { event.currentTarget.style.background = C.panel2 }}
                    onMouseLeave={(event) => { event.currentTarget.style.background = 'transparent' }}
                  >
                    <span style={{
                      width: 34, height: 24, flex: '0 0 34px', borderRadius: 4, overflow: 'hidden',
                      background: C.panel2, display: 'grid', placeItems: 'center',
                      color: KIND[candidate.kind]?.color ?? C.dim, fontSize: 12,
                    }}>
                      {thumb === null
                        ? (KIND[candidate.kind]?.glyph ?? '·')
                        : <img src={thumb} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />}
                    </span>
                    <span style={{ flex: 1, minWidth: 0, fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {candidateLabel(candidate)}
                    </span>
                    <span style={{ fontSize: 13, color: C.dimmer }}>＋</span>
                  </button>
                )
              })}
            </div>
          ))}
        </div>
        <div style={{ padding: '8px 10px', borderTop: `1px solid ${C.line}`, display: 'flex', gap: 6 }}>
          <button
            type="button"
            onClick={placeAll}
            disabled={candidates.length === 0}
            style={{ ...miniButton, flex: 1, opacity: candidates.length === 0 ? 0.4 : 1 }}
          >
            全部摆放
          </button>
          <button type="button" onClick={tidy} disabled={nodes.length === 0} style={{ ...miniButton, opacity: nodes.length === 0 ? 0.4 : 1 }}>
            整理
          </button>
        </div>
      </aside>

      {/* 画布 */}
      <div style={{ flex: 1, minWidth: 0, position: 'relative', display: 'flex', flexDirection: 'column' }}>
        <div style={{
          height: 38, flex: '0 0 38px', display: 'flex', alignItems: 'center', gap: 10,
          padding: '0 12px', borderBottom: `1px solid ${C.line}`, background: C.panel, fontSize: 11, color: C.dim,
        }}>
          <span style={{ color: C.text, fontSize: 12, fontWeight: 600 }}>无限画布</span>
          <span>{nodes.length} 个节点 · {edges.length} 条连线</span>
          <span style={{ color: C.dimmer }}>滚轮缩放 · 空格/中键拖动平移 · 拖空白框选</span>
          <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
            {selected.size > 0 && (
              <button type="button" onClick={removeSelected} style={{ ...miniButton, color: '#ff9a9a' }}>
                删除选中（{selected.size}）
              </button>
            )}
            <SaveBadge state={saveState} />
            <button type="button" onClick={() => { void load() }} style={miniButton}>重载</button>
          </div>
        </div>

        {error !== null && (
          <div role="alert" style={{ padding: '6px 12px', fontSize: 12, color: '#ffb03a', background: 'rgba(255,176,58,0.08)' }}>
            {error}
          </div>
        )}

        <div
          ref={viewportRef}
          data-vf-canvas="viewport"
          onPointerDown={onViewportPointerDown}
          onPointerMove={onViewportPointerMove}
          onPointerUp={onViewportPointerUp}
          onPointerLeave={onViewportPointerUp}
          style={{
            flex: 1, minHeight: 0, position: 'relative', overflow: 'hidden',
            cursor: panRef.current !== null ? 'grabbing' : (tool === 'hand' ? 'grab' : 'default'),
            background: 'radial-gradient(circle at 1px 1px, rgba(255,255,255,0.06) 1px, transparent 0) 0 0 / 24px 24px',
            touchAction: 'none',
          }}
        >
          {/* 连线层 */}
          <svg width="100%" height="100%" style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
            <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
              {edges.map((edge) => {
                const from = byId.get(edge.from)
                const to = byId.get(edge.to)
                if (from === undefined || to === undefined) return null
                const key = `${edge.from}->${edge.to}`
                const picked = edgePicked === key
                const x1 = from.x + from.w
                const y1 = from.y + from.h / 2
                const x2 = to.x
                const y2 = to.y + to.h / 2
                const bend = Math.max(40, Math.abs(x2 - x1) * 0.45)
                const d = `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`
                return (
                  <g key={key}>
                    <path
                      d={d}
                      fill="none"
                      stroke="transparent"
                      strokeWidth={14}
                      style={{ pointerEvents: 'stroke', cursor: 'pointer' }}
                      onPointerDown={(event) => { event.stopPropagation(); setEdgePicked(key) }}
                    />
                    <path
                      d={d}
                      fill="none"
                      stroke={picked ? C.accent : 'rgba(140,180,255,0.45)'}
                      strokeWidth={picked ? 2.6 : 1.8}
                      style={{ pointerEvents: 'none' }}
                    />
                  </g>
                )
              })}
              {linking === null ? null : (() => {
                const from = byId.get(linking.from)
                if (from === undefined) return null
                const x1 = from.x + from.w
                const y1 = from.y + from.h / 2
                const x2 = (linking.x - view.x) / view.k
                const y2 = (linking.y - view.y) / view.k
                const bend = Math.max(40, Math.abs(x2 - x1) * 0.4)
                return (
                  <path
                    d={`M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`}
                    fill="none" stroke={C.accent} strokeWidth={2} strokeDasharray="6 5"
                    style={{ pointerEvents: 'none' }}
                  />
                )
              })()}
            </g>
          </svg>

          {/* 节点层 */}
          <div style={{
            position: 'absolute', inset: 0, transformOrigin: '0 0',
            transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})`,
            pointerEvents: 'none',
          }}>
            {nodes.map((node) => {
              const active = selected.has(node.id)
              const thumbPath = (node.meta as { thumb?: unknown }).thumb
              const thumb = mediaUrl(typeof thumbPath === 'string' ? thumbPath : null)
              const isVideo = thumb !== null && /\.(mp4|mov|webm)$/iu.test(thumb)
              const shot = node.kind === 'shots' ? shotMap.get(node.ref_id) : undefined
              const badge = shot !== undefined && typeof shot.order_index === 'number'
                ? `镜${String(shot.order_index + 1).padStart(2, '0')}${typeof shot.duration_seconds === 'number' ? ` · ${Math.round(shot.duration_seconds)}s` : ''}`
                : KIND[node.kind]?.label ?? node.kind
              return (
                <div
                  key={node.id}
                  data-node-id={node.id}
                  onPointerDown={(event) => { onNodePointerDown(event, node) }}
                  onDoubleClick={() => { setEditing({ id: node.id, value: titleOfNode(node, shotMap) }) }}
                  onMouseEnter={() => setHover(node.id)}
                  onMouseLeave={() => setHover((prev) => (prev === node.id ? null : prev))}
                  style={{
                    position: 'absolute', left: node.x, top: node.y, width: node.w, height: node.h,
                    pointerEvents: 'auto', userSelect: 'none', cursor: dragRef.current === null ? 'move' : 'grabbing',
                    background: C.card, borderRadius: 10, overflow: 'hidden',
                    border: `1px solid ${active ? C.accent : C.line}`,
                    boxShadow: active
                      ? '0 0 0 2px rgba(122,193,48,0.25), 0 10px 26px rgba(0,0,0,0.45)'
                      : '0 6px 18px rgba(0,0,0,0.32)',
                    zIndex: node.z,
                  }}
                >
                  <div style={{ width: '100%', height: THUMB_H, background: C.panel2, position: 'relative', overflow: 'hidden' }}>
                    {thumb === null ? (
                      <div style={{ width: '100%', height: '100%', display: 'grid', placeItems: 'center', color: KIND[node.kind]?.color ?? C.dim, fontSize: 26, opacity: 0.55 }}>
                        {KIND[node.kind]?.glyph ?? '·'}
                      </div>
                    ) : isVideo ? (
                      <video src={thumb} muted playsInline style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                    ) : (
                      <img src={thumb} alt="" draggable={false} style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
                    )}
                    <span style={{
                      position: 'absolute', left: 7, top: 7, fontSize: 10, padding: '1px 6px', borderRadius: 5,
                      color: '#fff', background: 'rgba(0,0,0,0.55)', letterSpacing: 0.3,
                    }}>{badge}</span>
                    {(active || hover === node.id) && (
                      <div style={{ position: 'absolute', right: 6, top: 6, display: 'flex', gap: 4 }}>
                        <NodeAction title="交给 agent 处理" glyph="⚡" onClick={() => askAgent(node)} />
                        <button
                          type="button"
                          title="从画布移除"
                          onPointerDown={(event) => { event.stopPropagation() }}
                          onClick={(event) => {
                            event.stopPropagation()
                            commit(nodesRef.current.filter((item) => item.id !== node.id), true)
                          }}
                          style={nodeActionStyle}
                        >✕</button>
                      </div>
                    )}
                  </div>
                  <div style={{ padding: '7px 9px 0', height: NODE_H - THUMB_H - 7 }}>
                    {editing !== null && editing.id === node.id ? (
                      <input
                        autoFocus
                        value={editing.value}
                        onChange={(event) => setEditing({ id: node.id, value: event.target.value })}
                        onBlur={() => rename(node.id, editing.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') rename(node.id, editing.value)
                          if (event.key === 'Escape') setEditing(null)
                        }}
                        style={{
                          width: '100%', boxSizing: 'border-box', fontSize: 12, padding: '2px 5px',
                          background: C.panel2, color: C.text, border: `1px solid ${C.accent}`, borderRadius: 5, outline: 'none',
                        }}
                      />
                    ) : (
                      <div style={{
                        fontSize: 12, fontWeight: 600, lineHeight: 1.35, color: C.text,
                        display: '-webkit-box', WebkitLineClamp: 1, WebkitBoxOrient: 'vertical', overflow: 'hidden',
                      }}>
                        {titleOfNode(node, shotMap)}
                      </div>
                    )}
                    <div style={{ fontSize: 10, color: C.dimmer, marginTop: 2, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {shot?.layer1_overview !== undefined && shot.layer1_overview !== ''
                        ? shot.layer1_overview
                        : (KIND[node.kind]?.label ?? node.kind)}
                    </div>
                  </div>
                  {/* 连线端口 */}
                  <div
                    title="拖到另一个节点上连线"
                    onPointerDown={(event) => {
                      event.stopPropagation()
                      const rect = viewportRef.current?.getBoundingClientRect()
                      if (rect === undefined) return
                      setLinking({ from: node.id, x: event.clientX - rect.left, y: event.clientY - rect.top })
                    }}
                    style={{
                      position: 'absolute', right: -7, top: node.h / 2 - 7, width: 14, height: 14, borderRadius: '50%',
                      background: C.card, border: `2px solid ${hover === node.id || active ? C.accent : 'rgba(150,160,175,0.6)'}`,
                      cursor: 'crosshair',
                    }}
                  />
                </div>
              )
            })}
          </div>

          {/* 框选 */}
          {marquee !== null && (
            <div style={{
              position: 'absolute', pointerEvents: 'none',
              left: Math.min(marquee.x0, marquee.x1), top: Math.min(marquee.y0, marquee.y1),
              width: Math.abs(marquee.x1 - marquee.x0), height: Math.abs(marquee.y1 - marquee.y0),
              background: 'rgba(122,193,48,0.10)', border: `1px solid ${C.accent}`, borderRadius: 3,
            }} />
          )}

          {/* 空状态 */}
          {!loading && nodes.length === 0 && (
            <div style={{
              position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', pointerEvents: 'none',
            }}>
              <div style={{ textAlign: 'center', color: C.dim, fontSize: 13, lineHeight: 2 }}>
                <div style={{ fontSize: 30, opacity: 0.35, marginBottom: 6 }}>▦</div>
                从左边把角色 / 场景 / 分镜点上来<br />
                <span style={{ color: C.dimmer, fontSize: 12 }}>
                  或者点左下角「全部摆放」 · 也可以直接拖节点排布、拖右侧圆点连线
                </span>
              </div>
            </div>
          )}

          {/* 缩放控件 */}
          <div style={{
            position: 'absolute', right: 14, bottom: 14, display: 'flex', alignItems: 'center', gap: 2,
            padding: 3, borderRadius: 8, background: 'rgba(18,18,24,0.92)', border: `1px solid ${C.line}`,
            boxShadow: '0 6px 18px rgba(0,0,0,0.4)',
          }}>
            <button type="button" title="缩小" style={zoomButton} onClick={() => {
              const el = viewportRef.current
              zoomAt(1 / 1.25, (el?.clientWidth ?? 900) / 2, (el?.clientHeight ?? 600) / 2)
            }}>−</button>
            <button
              type="button"
              title="恢复 100%"
              style={{ ...zoomButton, width: 54, fontSize: 11 }}
              onClick={() => {
                const el = viewportRef.current
                const cx = (el?.clientWidth ?? 900) / 2
                const cy = (el?.clientHeight ?? 600) / 2
                zoomAt(1 / view.k, cx, cy)
              }}
            >
              {Math.round(view.k * 100)}%
            </button>
            <button type="button" title="放大" style={zoomButton} onClick={() => {
              const el = viewportRef.current
              zoomAt(1.25, (el?.clientWidth ?? 900) / 2, (el?.clientHeight ?? 600) / 2)
            }}>＋</button>
            <div style={{ width: 1, height: 16, background: C.line, margin: '0 2px' }} />
            <button type="button" title="适应画布（Ctrl+F）" style={zoomButton} onClick={fitAll}>⤢</button>
          </div>

          {/* 小地图 */}
          {showMap && nodes.length > 0 && (
            <Minimap
              box={mapBox}
              nodes={nodes}
              view={view}
              viewport={viewportSize}
              onJump={(worldX, worldY) => {
                setView((prev) => ({
                  ...prev,
                  x: viewportSize.w / 2 - worldX * prev.k,
                  y: viewportSize.h / 2 - worldY * prev.k,
                }))
              }}
            />
          )}
        </div>
      </div>
    </div>
  )
}

/* ───────────────────────── 子组件 / 样式 ───────────────────────── */

/** 把节点标题变成人话（后端兜底的 `shots-4c1608` 换成镜号）。 */
function titleOfNode(node: CanvasNode, shotMap: ReadonlyMap<string, ShotBrief>): string {
  const shot = node.kind === 'shots' ? shotMap.get(node.ref_id) : undefined
  if (shot !== undefined && typeof shot.order_index === 'number' && looksLikeId(node.title)) {
    return `镜${String(shot.order_index + 1).padStart(2, '0')}`
  }
  if (looksLikeId(node.title)) return KIND[node.kind]?.label ?? node.kind
  return node.title
}

/** 保存状态小徽标。 */
function SaveBadge({ state }: { readonly state: 'idle' | 'saving' | 'saved' | 'error' }) {
  const text = state === 'saving' ? '保存中…' : state === 'saved' ? '已保存' : state === 'error' ? '未保存' : '自动保存已开'
  const color = state === 'error' ? '#ff9a9a' : state === 'saved' ? C.accent : C.dimmer
  return (
    <span style={{ fontSize: 11, color, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      <span style={{ width: 6, height: 6, borderRadius: '50%', background: color, display: 'inline-block' }} />
      {text}
    </span>
  )
}

/** 左侧竖排工具按钮。 */
function ToolButton(props: {
  readonly active: boolean
  readonly label: string
  readonly glyph: string
  readonly onClick: () => void
  readonly disabled?: boolean
}) {
  return (
    <button
      type="button"
      title={props.label}
      onClick={props.onClick}
      disabled={props.disabled === true}
      style={{
        width: 34, height: 30, display: 'grid', placeItems: 'center', fontSize: 14, cursor: 'pointer',
        borderRadius: 7, color: props.active ? C.text : C.dim,
        background: props.active ? 'rgba(122,193,48,0.16)' : 'transparent',
        border: `1px solid ${props.active ? 'rgba(122,193,48,0.5)' : 'transparent'}`,
        opacity: props.disabled === true ? 0.35 : 1,
      }}
    >
      {props.glyph}
    </button>
  )
}

/** 节点右上角的小圆钮。 */
function NodeAction(props: { readonly title: string; readonly glyph: string; readonly onClick: () => void }) {
  return (
    <button
      type="button"
      title={props.title}
      onPointerDown={(event) => { event.stopPropagation() }}
      onClick={(event) => { event.stopPropagation(); props.onClick() }}
      style={nodeActionStyle}
    >{props.glyph}</button>
  )
}

/** 小地图：一眼看到整张画布在哪、视口在哪。 */
function Minimap(props: {
  readonly box: { x: number; y: number; w: number; h: number }
  readonly nodes: readonly CanvasNode[]
  readonly view: View
  readonly viewport: { w: number; h: number }
  readonly onJump: (x: number, y: number) => void
}) {
  const W = 168
  const H = 112
  const pad = 8
  const scale = props.box.w <= 0 || props.box.h <= 0
    ? 1
    : Math.min((W - pad * 2) / props.box.w, (H - pad * 2) / props.box.h)
  const ox = pad - props.box.x * scale + (W - pad * 2 - props.box.w * scale) / 2
  const oy = pad - props.box.y * scale + (H - pad * 2 - props.box.h * scale) / 2
  // 视口在世界坐标里的矩形
  const vx = (0 - props.view.x) / props.view.k
  const vy = (0 - props.view.y) / props.view.k
  const vw = props.viewport.w / props.view.k
  const vh = props.viewport.h / props.view.k
  return (
    <div
      onPointerDown={(event) => {
        const rect = event.currentTarget.getBoundingClientRect()
        const worldX = (event.clientX - rect.left - ox) / scale
        const worldY = (event.clientY - rect.top - oy) / scale
        props.onJump(worldX, worldY)
      }}
      style={{
        position: 'absolute', right: 14, bottom: 58, width: W, height: H, borderRadius: 8, overflow: 'hidden',
        background: 'rgba(14,14,19,0.92)', border: `1px solid ${C.line}`, cursor: 'crosshair',
        boxShadow: '0 6px 18px rgba(0,0,0,0.4)',
      }}
    >
      {props.nodes.map((node) => (
        <div
          key={node.id}
          style={{
            position: 'absolute', left: ox + node.x * scale, top: oy + node.y * scale,
            width: Math.max(2, node.w * scale), height: Math.max(2, node.h * scale),
            background: KIND[node.kind]?.color ?? C.dim, opacity: 0.55, borderRadius: 2,
          }}
        />
      ))}
      <div style={{
        position: 'absolute', left: ox + vx * scale, top: oy + vy * scale,
        width: Math.max(6, vw * scale), height: Math.max(6, vh * scale),
        border: `1px solid ${C.accent}`, background: 'rgba(122,193,48,0.08)', borderRadius: 2,
      }} />
    </div>
  )
}

/** 没选项目时的项目选择页（不再是一句死胡同）。 */
export function VfProjectPicker(props: {
  readonly title?: string
  readonly hint?: string
  readonly onPicked: (id: string, title: string) => void
}) {
  const [projects, setProjects] = useState<readonly ProjectBrief[]>([])
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    void (async () => {
      const data = await fetchJson<{ projects?: ProjectBrief[] }>('/api/projects')
      setProjects(data?.projects ?? [])
      setLoading(false)
    })()
  }, [])
  return (
    <div style={{ padding: '28px 32px', color: C.text, height: '100%', overflowY: 'auto', boxSizing: 'border-box' }}>
      <div style={{ fontSize: 17, fontWeight: 650, marginBottom: 6 }}>{props.title ?? '画布 · 先选一个项目'}</div>
      <div style={{ fontSize: 12.5, color: C.dim, marginBottom: 18, lineHeight: 1.8 }}>
        {props.hint ?? '画布按项目存：角色、场景、分镜都属于某一部片。选一个项目进去摆。'}
      </div>
      {loading && <div style={{ fontSize: 12, color: C.dimmer }}>载入项目…</div>}
      {!loading && projects.length === 0 && (
        <div style={{ fontSize: 12, color: C.dimmer, lineHeight: 1.9 }}>
          还没有项目。回到「AI 对话」说一句「做个 12 秒短片」就能立项。
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 560 }}>
        {projects.map((project) => (
          <button
            key={project.id}
            type="button"
            onClick={() => {
              if (project.id !== undefined) props.onPicked(project.id, project.title ?? '项目')
            }}
            style={{
              textAlign: 'left', padding: '11px 14px', borderRadius: 9, cursor: 'pointer',
              background: C.panel, border: `1px solid ${C.line}`, color: C.text, fontSize: 13,
            }}
            onMouseEnter={(event) => { event.currentTarget.style.borderColor = 'rgba(122,193,48,0.5)' }}
            onMouseLeave={(event) => { event.currentTarget.style.borderColor = C.line }}
          >
            {project.title ?? '未命名项目'}
          </button>
        ))}
      </div>
    </div>
  )
}

const miniButton: CSSProperties = {
  fontSize: 11, padding: '4px 9px', cursor: 'pointer', color: '#dbe3ee',
  background: 'rgba(255,255,255,0.05)', border: `1px solid ${C.line}`, borderRadius: 6,
}

const zoomButton: CSSProperties = {
  width: 26, height: 24, display: 'grid', placeItems: 'center', cursor: 'pointer',
  fontSize: 13, color: '#dbe3ee', background: 'transparent', border: 'none', borderRadius: 5,
}

const nodeActionStyle: CSSProperties = {
  width: 22, height: 20, padding: 0, fontSize: 11, lineHeight: '18px', cursor: 'pointer',
  color: '#e6ecf5', background: 'rgba(0,0,0,0.55)', border: '1px solid rgba(255,255,255,0.22)', borderRadius: 5,
}
