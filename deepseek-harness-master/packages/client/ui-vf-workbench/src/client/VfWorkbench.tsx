/**
 * VideoForge 一屏工作台（面向"拍片子的人"）。
 *
 * 布局：项目（左） │ 9 步流水线（中） │ 产物（右）
 * 纪律：
 *   · 面板不持有业务状态，全部读 sidecar（库是唯一真相）
 *   · 免费步骤点了就排队；花钱步骤先出审批卡（金额来自 estimate），确认才执行
 *   · 每一步都要能看到"正在做什么 / 做完了什么 / 为什么还不能做"——不留黑箱
 */
import * as React from 'react'
import {
  api,
  subscribeJob,
  sidecarBase,
  setSidecarBase,
  type VfCharacter,
  type VfEstimate,
  type VfProject,
  type VfScript,
  type VfScene,
  type VfStage,
  type VfStageStatus,
} from './api.ts'

/* ───────────────────────── 样式（内联，避免额外 CSS 管线）───────────────────────── */
const CSS = `
/* ★ 只占**主区**（侧边栏右侧）：left 由 --vf-left 驱动（实测侧边栏宽度，见 useSidebarWidth）。
   不要写 inset:0 —— 那会盖住 DSH 侧边栏，把侧边栏里的「项目/流水线/产物」导航挡死。 */
.vf-root{position:absolute;top:0;right:0;bottom:0;left:var(--vf-left,260px);z-index:0;
  display:flex;flex-direction:column;overflow:hidden;
  background:#0f1115;color:#e8eaf0;font:13px/1.55 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
/* 右上角要让开 Electron 的原生窗口按钮（最小化/最大化/关闭），否则会被压在下面 */
.vf-top{display:flex;align-items:center;gap:10px;padding:10px 16px 10px 18px;
  padding-right:160px;background:#151922;border-bottom:1px solid #232936}
.vf-title{font-size:14px;font-weight:700;color:#e8eaf0;letter-spacing:.3px}
.vf-sep{color:#39425a;font-size:12px}
.vf-sub{color:#8b93a7;font-size:12px}
.vf-spacer{flex:1}
.vf-btn{background:#232936;color:#dfe4ee;border:1px solid #333b4d;border-radius:6px;
  padding:5px 11px;font-size:12px;cursor:pointer}
.vf-btn:hover{background:#2c3446}
.vf-btn:disabled{opacity:.45;cursor:not-allowed}
.vf-btn-primary{background:#2f6fed;border-color:#2f6fed;color:#fff}
.vf-btn-primary:hover{background:#3b7bf5}
.vf-btn-danger{background:#3a2326;border-color:#5b2f36;color:#ffb4b4}
.vf-body{flex:1;display:grid;grid-template-columns:280px 1fr 380px;min-height:0}
.vf-col{min-height:0;display:flex;flex-direction:column;border-right:1px solid #232936}
.vf-col:last-child{border-right:none}
.vf-col-h{padding:9px 14px;font-size:12px;color:#8b93a7;border-bottom:1px solid #232936;
  display:flex;align-items:center;gap:8px}
.vf-scroll{flex:1;overflow:auto}
.vf-search{margin:10px 12px;padding:6px 9px;background:#1a1f2b;border:1px solid #2b3344;
  border-radius:6px;color:#e8eaf0;font-size:12px;width:calc(100% - 24px)}
.vf-proj{padding:9px 14px;border-bottom:1px solid #1b202b;cursor:pointer}
.vf-proj:hover{background:#171c26}
.vf-proj.on{background:#1d2637;box-shadow:inset 3px 0 0 #2f6fed}
.vf-proj-t{font-size:13px;color:#e8eaf0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vf-proj-m{font-size:11px;color:#7c8499;margin-top:2px}
.vf-steps{padding:10px 12px;display:flex;flex-direction:column;gap:8px}
.vf-step{display:grid;grid-template-columns:34px 1fr auto;gap:10px;align-items:start;
  padding:10px 12px;border:1px solid #262e3d;border-radius:8px;background:#141924}
.vf-step.done{border-color:#1f4d33;background:#121b16}
.vf-step.current{border-color:#2f6fed;background:#131b2b}
.vf-step.gate{border-color:#8a6d1f;background:#1b1912}
.vf-step.waiting{opacity:.62}
.vf-idx{width:26px;height:26px;border-radius:50%;background:#232936;color:#aab3c6;
  display:flex;align-items:center;justify-content:center;font-size:12px}
.vf-step.done .vf-idx{background:#1f6b45;color:#d6ffe8}
.vf-step.current .vf-idx{background:#2f6fed;color:#fff}
.vf-step.gate .vf-idx{background:#8a6d1f;color:#fff5d1}
.vf-step-t{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600}
.vf-tag{font-size:11px;padding:1px 7px;border-radius:10px;background:#232936;color:#9aa5bb;font-weight:400}
.vf-tag.paid{background:#3a2a12;color:#ffc978}
.vf-tag.free{background:#15301f;color:#77e0a2}
.vf-ev{font-size:12px;color:#9aa5bb;margin-top:3px;word-break:break-word}
.vf-ev.warn{color:#ffc978}
.vf-why{font-size:11px;color:#6f7789;margin-top:3px}
.vf-acts{display:flex;flex-direction:column;gap:6px;align-items:flex-end}
.vf-log{margin:0 12px 12px;border:1px solid #262e3d;border-radius:8px;background:#10141d;
  padding:8px 10px;max-height:150px;overflow:auto;font:11px/1.5 Consolas,monospace;color:#8ff0b0}
.vf-log .m{color:#c9d1e0}
.vf-log .e{color:#ff9b9b}
.vf-bar{height:5px;background:#232936;border-radius:3px;overflow:hidden;margin-top:6px}
.vf-bar>i{display:block;height:100%;background:linear-gradient(90deg,#2f6fed,#4ac0ff);width:0;transition:width .3s}
.vf-art{padding:10px 14px;border-bottom:1px solid #1b202b}
.vf-video{width:100%;max-height:200px;background:#000;border-radius:6px;display:block}
.vf-thumbs{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:6px}
.vf-thumb{position:relative;border:1px solid #1e2532;border-radius:6px;overflow:hidden;background:#0d1017}
.vf-thumb img{width:100%;height:66px;object-fit:cover;display:block}
.vf-thumb-v{width:100%;height:66px;object-fit:cover;display:block;background:#000}
.vf-thumb span{display:block;font-size:10px;color:#9aa5bb;padding:2px 4px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.vf-thumb.miss img{display:none}
.vf-thumb.miss{height:66px;display:flex;align-items:center;justify-content:center;color:#6f7789;font-size:10px}
.vf-art-h{font-size:12px;color:#8b93a7;margin-bottom:6px;display:flex;justify-content:space-between}
.vf-art-body{font-size:12px;color:#cdd4e2;white-space:pre-wrap;word-break:break-word;
  max-height:190px;overflow:auto;background:#10141d;border:1px solid #1e2532;border-radius:6px;padding:8px}
.vf-kv{display:flex;justify-content:space-between;font-size:12px;color:#b9c1d1;padding:2px 0}
.vf-kv b{color:#8b93a7;font-weight:400}
.vf-mask{position:fixed;inset:0;background:rgba(4,6,10,.72);display:flex;align-items:center;
  justify-content:center;z-index:2147483001}
.vf-card{width:520px;max-width:92vw;background:#151a24;border:1px solid #2c3446;border-radius:12px;
  padding:18px 20px;box-shadow:0 18px 50px rgba(0,0,0,.55)}
.vf-card h3{margin:0 0 10px;font-size:16px}
.vf-money{font-size:30px;font-weight:700;color:#ffc978;margin:6px 0 2px}
.vf-money small{font-size:13px;color:#8b93a7;font-weight:400;margin-left:6px}
.vf-warn{background:#1d1a12;border:1px solid #5b4a1f;color:#ffd79a;border-radius:8px;
  padding:8px 10px;font-size:12px;margin:10px 0}
.vf-row{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
.vf-empty{padding:24px 16px;color:#6f7789;font-size:12px;text-align:center}
.vf-err{color:#ff9b9b;font-size:12px;padding:8px 14px}
`

/* ───────────────────────── 小工具 ───────────────────────── */

const STAGE_ORDER = ['brief', 'script', 'assets', 'looks', 'shots', 'previz', 'draft', 'final', 'compose']

/** 会花钱的步骤 —— 必须走审批卡（与 sidecar 的 cost 口径一致）。 */
const PAID_STAGES = new Set(['draft', 'final'])

const STATUS_TEXT: Record<string, string> = {
  done: '已完成',
  current: '该做这一步',
  gate: '等你确认',
  waiting: '还没轮到',
  skipped: '你跳过了',
}

/** 九步的中文名：**界面永不出现英文 key**（后端连不上时也按这个兜底）。 */
const STAGE_TITLES: Record<string, string> = {
  brief: '立项',
  script: '剧本',
  assets: '角色 / 场景',
  looks: '定妆照 / 场景图',
  shots: '分镜表',
  previz: '白膜预演',
  draft: '480P 小样',
  final: '1080P 成片',
  compose: '后期成片',
}

/* ───────────────── 让位侧边栏：实测侧边栏宽度 ───────────────── */

/** 量不到时的退路（侧边栏常见宽度）。可被 CSS 变量覆盖：:root{--vf-sidebar-fallback:280px} */
const SIDEBAR_FALLBACK = 260

/**
 * 读侧边栏实际宽度。
 *
 * 为什么不用固定值：DSH 的壳是 CSS Grid（`gridTemplateColumns: "<sidebar>px minmax(...) ..."`），
 * 侧边栏能**拖拽调宽**、能**折叠成 0**、窗口缩放时也会变。硬编码一个数字，
 * 只要用户拖一下分隔条，面板就会盖住侧边栏或留一条缝。
 *
 * 取法（按可靠性排序）：
 *   1. 从我的挂载点向上找 `[data-shell-overlay]` 的父元素（= AppFrame 的 frame），
 *      读它的 computedStyle.gridTemplateColumns 第一条轨道（就是侧边栏列宽）；
 *   2. 退路：Windows 标题栏下 frame 会写 `--dsh-windows-sidebar-width`；
 *   3. 再退：SIDEBAR_FALLBACK 常量。
 */
function readSidebarWidth(): number {
  try {
    const layer = document.querySelector('[data-shell-overlay]')
    const frame = (layer?.parentElement ?? null) as HTMLElement | null
    if (frame) {
      const cols = (getComputedStyle(frame).gridTemplateColumns || '').trim()
      const first = parseFloat(cols.split(/\s+/)[0] ?? '')
      if (Number.isFinite(first)) return Math.max(0, Math.round(first))
    }
    const cssVar = getComputedStyle(document.documentElement)
      .getPropertyValue('--dsh-windows-sidebar-width')
      .trim()
    const n = parseFloat(cssVar)
    if (Number.isFinite(n)) return Math.max(0, Math.round(n))
  } catch {
    /* 文档不在预期结构里（夹具环境）→ 走退路 */
  }
  const fallbackVar = parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue('--vf-sidebar-fallback') || '',
  )
  return Number.isFinite(fallbackVar) ? Math.max(0, Math.round(fallbackVar)) : SIDEBAR_FALLBACK
}

/** 订阅侧边栏宽度变化：拖分隔条改的是 inline style，折叠改的是属性，缩放改的是尺寸。 */
function useSidebarWidth(): number {
  const [w, setW] = React.useState<number>(() =>
    typeof document === 'undefined' ? SIDEBAR_FALLBACK : readSidebarWidth(),
  )
  React.useEffect(() => {
    let raf = 0
    const update = (): void => {
      if (raf) return
      raf = window.requestAnimationFrame(() => {
        raf = 0
        setW(readSidebarWidth())
      })
    }
    update()
    const frame = document.querySelector('[data-shell-overlay]')?.parentElement ?? null
    let ro: ResizeObserver | null = null
    try {
      if (frame && typeof ResizeObserver !== 'undefined') {
        ro = new ResizeObserver(update)
        ro.observe(frame)
      }
    } catch {
      /* 老环境没有 ResizeObserver → 还有 window resize 监听 */
    }
    const mo = new MutationObserver(update)
    if (frame) {
      mo.observe(frame, {
        attributes: true,
        attributeFilter: ['style', 'class', 'data-sidebar-collapsed', 'data-rightbar-collapsed'],
      })
    }
    window.addEventListener('resize', update)
    return () => {
      if (raf) window.cancelAnimationFrame(raf)
      ro?.disconnect()
      mo.disconnect()
      window.removeEventListener('resize', update)
    }
  }, [])
  return w
}

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return '待确认'
  return `¥${v.toFixed(2)}`
}

function fmtTime(s?: string): string {
  if (!s) return ''
  // 兼容后端 'YYYY-MM-DD HH:MM:SS' / ISO
  return s.replace('T', ' ').slice(0, 16)
}

/* ───────────────────────── 主面板 ───────────────────────── */

export function VfWorkbench(): React.ReactElement {
  /** 侧边栏实际宽度（拖拽/折叠/缩放都会跟着变）—— 本面板从它的右侧开始。 */
  const sidebarW = useSidebarWidth()
  const [projects, setProjects] = React.useState<VfProject[]>([])
  const [query, setQuery] = React.useState('')
  const [pid, setPid] = React.useState('')
  const [stageStatus, setStageStatus] = React.useState<VfStageStatus | null>(null)
  const [script, setScript] = React.useState<VfScript | null>(null)
  const [chars, setChars] = React.useState<VfCharacter[]>([])
  const [scenes, setScenes] = React.useState<VfScene[]>([])
  const [shots, setShots] = React.useState<Array<Record<string, unknown>>>([])
  // 产物预览：成片能不能放（404 时降级成一句提示）+ 联络表 url
  const [finalOk, setFinalOk] = React.useState(true)
  const [framesheetUrl, setFramesheetUrl] = React.useState('')
  const [sheetErr, setSheetErr] = React.useState('')
  const [err, setErr] = React.useState('')
  const [notice, setNotice] = React.useState('')

  // 审批卡
  const [approval, setApproval] = React.useState<{ est: VfEstimate; stage: string } | null>(null)
  const [approving, setApproving] = React.useState(false)

  // 当前 job
  const [job, setJob] = React.useState<{ id: string; stage: string; progress: number; status: string } | null>(null)
  const [log, setLog] = React.useState<Array<{ t: string; m: string; cls?: string }>>([])
  const unsubRef = React.useRef<null | (() => void)>(null)

  const pushLog = React.useCallback((m: string, cls?: string) => {
    setLog(prev => [...prev.slice(-120), { t: new Date().toLocaleTimeString(), m, cls }])
  }, [])

  /* 项目列表 */
  const loadProjects = React.useCallback(async () => {
    try {
      setErr('')
      const list = await api.listProjects()
      setProjects(list)
      setPid(cur => cur || (list[0]?.id ?? ''))
    } catch (e) {
      setErr(`连不上 VideoForge 后端（${sidecarBase()}）：${(e as Error).message}`)
    }
  }, [])

  React.useEffect(() => {
    void loadProjects()
  }, [loadProjects])

  /* 选中项目的流水线 + 产物 */
  const loadProject = React.useCallback(async (id: string) => {
    if (!id) return
    try {
      setErr('')
      setFramesheetUrl('')
      setFinalOk(true)
      const st = await api.stageStatus(id)
      setStageStatus(st)
      void api.script(id).then(setScript).catch(() => setScript(null))
      void api.characters(id).then(setChars).catch(() => setChars([]))
      void api.scenes(id).then(setScenes).catch(() => setScenes([]))
      void api.shots(id).then(setShots).catch(() => setShots([]))
    } catch (e) {
      setErr(`读项目失败：${(e as Error).message}`)
    }
  }, [])

  React.useEffect(() => {
    void loadProject(pid)
  }, [pid, loadProject])

  /* 执行一个 job（免费直接跑；花钱先出审批卡） */
  const startJob = React.useCallback(
    async (stage: string, opts?: { redo?: boolean }) => {
      if (!pid) return
      try {
        setErr('')
        setNotice('')
        unsubRef.current?.()
        setLog([])
        pushLog(`提交「${stage}」${opts?.redo ? '（重做）' : ''}…`)
        const r = await api.run(pid, stage, opts?.redo ? { redo: true } : {})
        setJob({ id: r.job_id, stage, progress: 0, status: 'running' })
        if (r.reused) pushLog('这一步已经在跑，复用原来的任务（没有重复提交）', 'm')
        else pushLog(`任务已排队：${r.job_id}`)
        unsubRef.current = subscribeJob(r.job_id, {
          progress: e => {
            setJob(j => (j ? { ...j, progress: Number(e.progress ?? 0), status: String(e.status ?? 'running') } : j))
            if (e.message) pushLog(`[${e.stage ?? stage}] ${e.message}${e.shot_index ? ` · 第 ${e.shot_index} 镜` : ''}`)
          },
          done: e => {
            const ok = e.ok !== false
            pushLog(ok ? '✅ 这一步做完了' : `❌ 没做成：${e.result?.error ?? '未知原因'}`, ok ? '' : 'e')
            setJob(j => (j ? { ...j, progress: 1, status: ok ? 'completed' : 'failed' } : j))
            void loadProject(pid)
          },
          error: m => pushLog(`进度连接：${m}`, 'e'),
        })
      } catch (e) {
        setErr(`提交失败：${(e as Error).message}`)
      }
    },
    [pid, pushLog, loadProject],
  )

  /** 点击某一步：先判断要不要审批。 */
  const onClickStage = React.useCallback(
    async (st: VfStage) => {
      if (!pid) return
      const paid = PAID_STAGES.has(st.key) || st.cost === 'video' || st.cost === 'expensive'
      if (!paid) {
        void startJob(st.key, { redo: st.status === 'done' })
        return
      }
      // 花钱步骤：先问价，出审批卡
      try {
        setErr('')
        setNotice('正在问价…')
        const est = await api.estimate(pid, st.key)
        setApproval({ est, stage: st.key })
        setNotice('')
      } catch (e) {
        setErr(`拿不到这一步的价格：${(e as Error).message}`)
        setNotice('')
      }
    },
    [pid, startJob],
  )

  const confirmApprove = React.useCallback(async () => {
    if (!approval) return
    const stage = approval.stage
    setApproving(true)
    try {
      setApproval(null)
      await startJob(stage)
    } finally {
      setApproving(false)
    }
  }, [approval, startJob])

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return projects
    return projects.filter(p =>
      `${p.title ?? p.name ?? ''} ${p.description ?? ''} ${p.id}`.toLowerCase().includes(q),
    )
  }, [projects, query])

  const stages: VfStage[] = React.useMemo(() => {
    const list = stageStatus?.stages ?? []
    // 后端给的是完整 9 步；缺了就补占位，保证时间轴永远 9 格。
    // ★ 占位也用**中文名**（STAGE_TITLES）—— 后端没连上时界面不该出现 brief/script 这种英文 key。
    return STAGE_ORDER.map(k => list.find(s => s.key === k) ?? ({
      key: k, title: STAGE_TITLES[k] ?? k, tool: '', status: 'waiting',
    } as VfStage))
  }, [stageStatus])

  async function changeBase(): Promise<void> {
    const v = window.prompt('VideoForge 后端地址', sidecarBase())
    if (!v) return
    setSidecarBase(v)
    await loadProjects()
  }

  return (
    <div className="vf-root" style={{ ['--vf-left' as never]: `${sidebarW}px` }}>
      <style>{CSS}</style>

      {/* 顶栏：**不再重复放 VideoForge 标**（那在侧边栏，由品牌插件负责），
          这里只写"这一屏是什么"，避免和侧边栏的 logo/名称叠在一起。 */}
      <div className="vf-top">
        <span className="vf-title">工作台</span>
        <span className="vf-sep">·</span>
        <span className="vf-sub">项目 → 9 步流水线 → 产物</span>
        <span className="vf-spacer" />
        {notice ? <span className="vf-sub">{notice}</span> : null}
        <span className="vf-sub">{sidecarBase()}</span>
        <button className="vf-btn" onClick={() => void changeBase()}>改后端地址</button>
        <button className="vf-btn" onClick={() => void loadProjects()}>刷新</button>
      </div>

      {err ? <div className="vf-err">{err}</div> : null}

      <div className="vf-body">
        {/* 左：项目 */}
        <div className="vf-col">
          <div className="vf-col-h">
            <span>项目</span>
            <span className="vf-spacer" />
            <span>{projects.length} 个</span>
          </div>
          <input
            className="vf-search"
            placeholder="搜索项目（片名 / 简介）"
            value={query}
            onChange={e => setQuery(e.target.value)}
          />
          <div className="vf-scroll">
            {filtered.length === 0 ? (
              <div className="vf-empty">
                {projects.length === 0 ? '还没有项目（或后端没连上）' : '没有匹配的项目'}
              </div>
            ) : (
              filtered.map(p => (
                <div
                  key={p.id}
                  className={`vf-proj${p.id === pid ? ' on' : ''}`}
                  onClick={() => setPid(p.id)}
                >
                  <div className="vf-proj-t">{p.title ?? p.name ?? '(未命名)'}</div>
                  <div className="vf-proj-m">
                    {p.status ?? ''} {p.updated_at ? `· ${fmtTime(p.updated_at)}` : ''}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* 中：9 步流水线 */}
        <div className="vf-col">
          <div className="vf-col-h">
            <span>流水线（9 步）</span>
            <span className="vf-spacer" />
            <span>
              {stageStatus ? `${stageStatus.done_count ?? 0}/${stageStatus.total ?? 9} 步已完成` : '—'}
            </span>
          </div>

          <div className="vf-scroll">
            <div className="vf-steps">
              {stages.map((s, i) => {
                const ev = s.evidence ?? {}
                const paid = PAID_STAGES.has(s.key) || s.cost === 'video' || s.cost === 'expensive'
                // 只有「还没轮到」才不能点。被跳过的步骤**仍然可点**：
                // 跳过只是"这次不做"，用户随时可以反悔回来补做（尤其是花钱步骤）。
                const canClick = !!pid && s.status !== 'waiting'
                const running = job?.stage === s.key && job.status === 'running'
                return (
                  <div key={s.key} className={`vf-step ${s.status}`}>
                    <div className="vf-idx">{s.status === 'done' ? '✓' : i + 1}</div>
                    <div>
                      <div className="vf-step-t">
                        <span>{s.title}</span>
                        <span className={`vf-tag ${paid ? 'paid' : 'free'}`}>{paid ? '花钱' : '免费'}</span>
                        <span className="vf-tag">{STATUS_TEXT[s.status] ?? s.status}</span>
                      </div>
                      {/* 证据：这一步到底做完了什么（不许黑箱） */}
                      <div className={`vf-ev${s.status === 'gate' || s.status === 'waiting' ? ' warn' : ''}`}>
                        {String(ev.detail ?? (s.key === stages[i].key ? '' : '') ?? '') || '（还没有可展示的证据）'}
                      </div>
                      <div className="vf-why">
                        {s.eta ? `大致耗时：${s.eta}` : ''}
                        {s.missing_prereq && s.missing_prereq.length > 0
                          ? ` · 还差前置：${s.missing_prereq.join('、')}`
                          : ''}
                        {paid ? ' · 点了会先出审批卡（不确认不扣费）' : ''}
                      </div>
                      {running ? (
                        <div className="vf-bar">
                          <i style={{ width: `${Math.round((job?.progress ?? 0) * 100)}%` }} />
                        </div>
                      ) : null}
                    </div>
                    <div className="vf-acts">
                      <button
                        className={`vf-btn ${s.status === 'current' ? 'vf-btn-primary' : ''}`}
                        data-vf-stage={s.key}
                        data-vf-action="run"
                        disabled={!canClick || running}
                        onClick={() => void onClickStage(s)}
                        title={canClick ? '执行这一步' : '前置还没做完'}
                      >
                        {running ? '进行中…' : s.status === 'done' ? '重做' : '执行'}
                      </button>
                      {running && job ? (
                        <button
                          className="vf-btn vf-btn-danger"
                          onClick={() => {
                            void api.cancel(job.id).then(r => pushLog(r.message ?? '已请求取消'))
                          }}
                        >
                          取消
                        </button>
                      ) : null}
                    </div>
                  </div>
                )
              })}
            </div>

            {log.length > 0 ? (
              <div className="vf-log">
                {log.map((l, i) => (
                  <div key={i} className={l.cls ?? 'm'}>
                    <span style={{ opacity: 0.55 }}>{l.t} </span>
                    {l.m}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        </div>

        {/* 右：产物 */}
        <div className="vf-col">
          <div className="vf-col-h">
            <span>产物</span>
            <span className="vf-spacer" />
            <span>{pid ? pid.slice(0, 8) : '—'}</span>
          </div>
          <div className="vf-scroll">
            {/* 成片预览：直接吃 sidecar 的 /final（video/mp4），不内联字节 */}
            <div className="vf-art">
              <div className="vf-art-h">
                <span>成片</span>
                <span>
                  {finalOk ? (
                    <a href={api.finalUrl(pid, true)} style={{ color: '#6ea8ff' }}>
                      下载
                    </a>
                  ) : (
                    '还没有成片'
                  )}
                </span>
              </div>
              {finalOk && pid ? (
                <video
                  className="vf-video"
                  controls
                  preload="metadata"
                  src={api.finalUrl(pid)}
                  onError={() => setFinalOk(false)}
                />
              ) : (
                <div className="vf-art-body">还没有成片 —— 先把「后期成片」那一步做掉</div>
              )}
            </div>

            {/* 白膜帧：直接吃 sidecar 的 /api/media（不内联字节） */}
            <div className="vf-art">
              <div className="vf-art-h">
                <span>白膜帧</span>
                <span>
                  {shots.filter(s => String((s as any).previz_path ?? '').trim()).length}/{shots.length} 镜有帧
                </span>
              </div>
              <div className="vf-thumbs">
                {shots.length === 0 ? (
                  <div className="vf-art-body" style={{ gridColumn: '1 / -1' }}>
                    还没有分镜
                  </div>
                ) : (
                  shots.map((s, i) => {
                    const p = String((s as any).previz_path ?? '').trim()
                    const isImg = /\.(png|jpe?g|webp)$/i.test(p)
                    const isVid = /\.(mp4|webm|mov)$/i.test(p)
                    return (
                      <div key={i} className={`vf-thumb${p ? '' : ' miss'}`} title={p || '这一镜还没渲白膜'}>
                        {p && isImg ? <img src={api.mediaUrl(p)} alt={`第 ${i + 1} 镜白膜`} /> : null}
                        {p && isVid ? (
                          <video className="vf-thumb-v" src={api.mediaUrl(p)} muted preload="metadata" />
                        ) : null}
                        {!p || (!isImg && !isVid) ? '没帧' : null}
                        <span>第 {i + 1} 镜</span>
                      </div>
                    )
                  })
                )}
              </div>
            </div>

            {/* 逐镜联络表（可选）：sidecar 拼图后回一个 /api/media url */}
            <div className="vf-art">
              <div className="vf-art-h">
                <span>逐镜联络表</span>
                <span>
                  <button
                    className="vf-btn"
                    data-vf-action="framesheet"
                    style={{ padding: '1px 8px' }}
                    onClick={() => {
                      if (!pid) return
                      setSheetErr('')
                      void api
                        .framesheet(pid)
                        .then(r => setFramesheetUrl(r?.url ? `${sidecarBase()}${r.url}` : ''))
                        .catch(e => setSheetErr(`没生成：${(e as Error).message}`))
                    }}
                  >
                    生成
                  </button>
                </span>
              </div>
              {framesheetUrl ? (
                <img className="vf-video" src={framesheetUrl} alt="逐镜联络表" />
              ) : (
                <div className="vf-art-body">
                  {sheetErr || '把各镜抽帧拼成一张联络表 —— 一眼看全有没有倒置/重复镜头（需要有已选中的片段）'}
                </div>
              )}
            </div>

            {/* 剧本 */}
            <div className="vf-art">
              <div className="vf-art-h">
                <span>剧本</span>
                <span>{script ? '已生成' : '还没有'}</span>
              </div>
              <div className="vf-art-body">
                {script
                  ? String(script.content ?? script.summary ?? script.logline ?? '（剧本没有正文）').slice(0, 4000)
                  : '还没有剧本 —— 先做「剧本」那一步'}
              </div>
            </div>

            {/* 角色 / 场景 */}
            <div className="vf-art">
              <div className="vf-art-h">
                <span>角色 / 场景（定妆照）</span>
                <span>
                  {chars.filter(c => c.reference_image_path).length}/{chars.length} ·{' '}
                  {scenes.filter(s => s.reference_image_path).length}/{scenes.length}
                </span>
              </div>
              <div className="vf-thumbs">
                {[...chars.map(c => ({ k: '角色', n: c.name, p: c.reference_image_path })),
                  ...scenes.map(s => ({ k: '场景', n: s.name, p: s.reference_image_path }))].length === 0 ? (
                  <div className="vf-art-body" style={{ gridColumn: '1 / -1' }}>
                    还没有角色/场景资产
                  </div>
                ) : (
                  [...chars.map(c => ({ k: '角色', n: c.name, p: c.reference_image_path })),
                    ...scenes.map(s => ({ k: '场景', n: s.name, p: s.reference_image_path }))].map((it, i) => (
                    <div key={i} className={`vf-thumb${it.p ? '' : ' miss'}`} title={it.p ?? '缺图'}>
                      {it.p ? <img src={api.mediaUrl(String(it.p))} alt={String(it.n ?? '')} /> : '缺图'}
                      <span>
                        {it.k}：{it.n ?? '?'}
                      </span>
                    </div>
                  ))
                )}
              </div>
            </div>

            {/* 分镜 */}
            <div className="vf-art">
              <div className="vf-art-h">
                <span>分镜</span>
                <span>{shots.length} 个镜头</span>
              </div>
              <div className="vf-art-body">
                {shots.length === 0
                  ? '还没有分镜'
                  : shots
                      .slice(0, 12)
                      .map(
                        (s, i) =>
                          `#${i + 1} ${String((s as any).layer1_overview ?? (s as any).description ?? '')}`.slice(0, 90),
                      )
                      .join('\n')}
              </div>
            </div>

            {/* 成片 */}
            <div className="vf-art">
              <div className="vf-art-h">
                <span>成片 / 进度</span>
                <span>{job ? job.status : '空闲'}</span>
              </div>
              <div className="vf-art-body">
                {[
                  `后端口：${sidecarBase()}`,
                  job ? `当前任务：${job.stage} · ${Math.round(job.progress * 100)}%` : '当前没有在跑的任务',
                  stageStatus?.headline ? `流水线：${stageStatus.headline}` : '',
                  (() => {
                    const c = stages.find(s => s.key === 'compose')
                    return c?.evidence?.detail ? `成片：${String(c.evidence.detail)}` : ''
                  })(),
                ]
                  .filter(Boolean)
                  .join('\n')}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* 审批卡 */}
      {approval ? (
        <div className="vf-mask" onClick={() => setApproval(null)}>
          <div className="vf-card" onClick={e => e.stopPropagation()}>
            <h3>这一步要花钱，先确认</h3>
            <div className="vf-sub">{approval.est.title}</div>
            <div className="vf-money">
              {fmtMoney(approval.est.cost_yuan)}
              <small>{approval.est.cost_known === false ? '（后端说金额待确认）' : '预计'}</small>
            </div>
            <div className="vf-kv">
              <b>耗时</b>
              <span>{approval.est.eta}</span>
            </div>
            <div className="vf-kv">
              <b>怎么算的</b>
              <span style={{ textAlign: 'right', maxWidth: 300 }}>{approval.est.basis ?? ''}</span>
            </div>
            {approval.est.cost_floor_yuan ? (
              <div className="vf-kv">
                <b>至少</b>
                <span>{fmtMoney(approval.est.cost_floor_yuan)}（下界，不是报价）</span>
              </div>
            ) : null}
            {approval.est.cost_known === false ? (
              <div className="vf-warn">
                后端拿不到这条通道的价目表，**金额待确认** —— 不确认不会扣费；确认后按实际账单结算。
              </div>
            ) : null}
            <div className="vf-row">
              <button className="vf-btn" onClick={() => setApproval(null)} disabled={approving}>
                先不花
              </button>
              <button className="vf-btn vf-btn-primary" onClick={() => void confirmApprove()} disabled={approving}>
                {approving ? '提交中…' : '确认，开始做'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}

export default VfWorkbench
