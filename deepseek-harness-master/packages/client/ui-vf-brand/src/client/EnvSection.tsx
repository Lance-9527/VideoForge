/** VideoForge 运行环境面板（2026-10-25，客户刚需）。
 *
 * 为什么要有这一页：
 *   安装版后端是 PyInstaller **冻结包** —— 没有 pip，且打包时裁掉了一批重依赖
 *   （cv2 / pandas / numba / cryptography / llvmlite …）。客户自蒸馏的 skill 只要
 *   import 这些就报错，而用户看不到任何可操作的信息。
 *
 *   这一页把「缺什么 / 怎么补 / 有没有装上」摊开：
 *     ① 解释器与是否冻结（决定能不能用 pip）；
 *     ② 可写扩展目录 `skillenv`（python/bin/wheels）与注入状态；
 *     ③ 常用包逐个体检（缺的给补法）；
 *     ④ 离线装 wheel：把 .whl 放进 wheels 目录，点一下装 / 或填绝对路径装单个；
 *     ⑤ 每个包可「验证导入」—— 真 import 一次，确认 ABI 与当前解释器匹配。
 *
 * 数据全部来自 sidecar 的 /api/env*，前端不复制任何判定逻辑。
 *
 * @module
 */
import { useCallback, useEffect, useState } from 'react'
import type { CSSProperties } from 'react'

/** Python 视频服务地址（与其余壳件一致）。 */
const SIDECAR = 'http://127.0.0.1:8765'

/** 与主面板同一套暗色 token。 */
const C = {
  bg: '#0d0d10',
  card: '#141419',
  line: '#26262e',
  text: '#f0f0f3',
  dim: '#9a9aa4',
  dimmer: '#6c6c76',
  accent: '#7ac130',
  warn: '#ffb03a',
  danger: '#ff6b6b',
} as const

interface PackageRow {
  readonly module: string
  readonly pip: string
  readonly use: string
  readonly available: boolean
  readonly fix: string
}

interface EnvReport {
  readonly python: {
    readonly version: string
    readonly implementation: string
    readonly executable: string
    readonly frozen: boolean
    readonly pip_available: boolean
  }
  readonly skillenv: {
    readonly path: string
    readonly active: boolean
    readonly writable: Record<string, boolean>
    readonly python_path_in_sys_path: boolean
    readonly bin_in_path: boolean
  }
  readonly packages: PackageRow[]
  readonly missing: string[]
  readonly install_hint: string
}

interface ProbeResult {
  readonly ok: boolean
  readonly module: string
  readonly version?: string
  readonly error?: string
  readonly elapsed_ms?: number
}

const shellStyle: CSSProperties = {
  position: 'absolute', inset: 0, background: C.bg, color: C.text,
  display: 'flex', flexDirection: 'column', overflow: 'hidden', zIndex: 5,
}

/** 嵌在「更多 → 运行环境」弹窗里时的外框：不铺满主区、限高、按弹窗宽度自适应。 */
const embeddedShellStyle: CSSProperties = {
  display: 'flex', flexDirection: 'column', maxHeight: '62vh', width: '100%', minWidth: 0, color: C.text,
}

const cardStyle: CSSProperties = {
  background: C.card, border: `1px solid ${C.line}`, borderRadius: 10, padding: '12px 14px',
}

const btnStyle: CSSProperties = {
  padding: '6px 12px', borderRadius: 8, font: 'inherit', fontSize: 13, cursor: 'pointer',
  border: `1px solid ${C.line}`, background: 'transparent', color: C.text,
}

function Pill({ ok, children }: { ok: boolean, children: string }) {
  const color = ok ? C.accent : C.warn
  return (
    <span style={{
      fontSize: 11, padding: '2px 8px', borderRadius: 999, color,
      border: `1px solid color-mix(in srgb, ${color} 45%, transparent)`,
      background: `color-mix(in srgb, ${color} 12%, transparent)`, whiteSpace: 'nowrap',
    }}>{children}</span>
  )
}

async function callJson(path: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(`${SIDECAR}${path}`, init)
  let body: { code?: string, message?: string, data?: unknown } = {}
  try { body = await response.json() as typeof body } catch { /* ignore */ }
  if (!response.ok || (body.code !== undefined && body.code !== '000000')) {
    throw new Error(body.message || `HTTP ${response.status}`)
  }
  return body.data
}

export function EnvSection({ embedded = false }: { readonly embedded?: boolean } = {}) {
  const [report, setReport] = useState<EnvReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [wheelPath, setWheelPath] = useState('')
  const [probes, setProbes] = useState<Record<string, ProbeResult>>({})

  const load = useCallback(async () => {
    try {
      setError(null)
      setReport(await callJson('/api/env') as EnvReport)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => { void load() }, [load])

  async function run(action: () => Promise<string>) {
    setBusy(true); setNotice(null)
    try { setNotice(await action()) } catch (exc) {
      setNotice(`失败：${exc instanceof Error ? exc.message : String(exc)}`)
    } finally {
      setBusy(false)
      void load()
    }
  }

  const installFromWheelsDir = () => run(async () => {
    const data = await callJson('/api/env/install-wheels-dir', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    }) as { installed?: string[], files?: number, wheels?: string[] }
    const names = (data.installed ?? []).join('、')
    if ((data.wheels ?? []).length === 0) return 'wheels 目录里还没有 .whl —— 先把文件放进去再点。'
    return `已装 ${names || '(无新包)'}，共 ${data.files ?? 0} 个文件。`
  })

  const installOne = () => run(async () => {
    const path = wheelPath.trim()
    if (!path) return '先填一个 .whl 的绝对路径。'
    const data = await callJson('/api/env/install-wheel', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }),
    }) as { installed?: string[] }
    setWheelPath('')
    return `已装 ${(data.installed ?? []).join('、') || path}`
  })

  const probe = async (module: string) => {
    try {
      const data = await callJson('/api/env/probe-import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ module }),
      }) as ProbeResult
      setProbes((prev) => ({ ...prev, [module]: data }))
    } catch (exc) {
      setProbes((prev) => ({
        ...prev,
        [module]: { ok: false, module, error: exc instanceof Error ? exc.message : String(exc) },
      }))
    }
  }

  const missing = report?.missing ?? []
  const ready = (report?.packages ?? []).filter((row) => row.available).length

  return (
    <div style={embedded ? embeddedShellStyle : shellStyle}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 12,
        padding: embedded ? '2px 0 10px' : '14px 22px',
        borderBottom: embedded ? 'none' : `1px solid ${C.line}`,
      }}>
        {!embedded && <div style={{ fontWeight: 650, fontSize: 15 }}>运行环境</div>}
        <span style={{ fontSize: 12, color: C.dimmer }}>skill 缺依赖时先看这里</span>
        <button type="button" style={{ ...btnStyle, marginLeft: 'auto' }} disabled={busy} onClick={() => void load()}>
          {busy ? '处理中…' : '刷新'}
        </button>
      </div>

      <div style={{
        padding: embedded ? '2px 0 6px' : '16px 22px 60px',
        overflowY: 'auto',
        overflowX: embedded ? 'auto' : 'hidden',
        height: embedded ? 'auto' : 'calc(100% - 58px)',
      }}>
        {error !== null && <div role="alert" style={{ color: C.warn, fontSize: 13, marginBottom: 12 }}>{error}</div>}
        {report === null && error === null && <div style={{ color: C.dim, fontSize: 13 }}>读取中…</div>}

        {report !== null && (
          <>
            <div style={{ ...cardStyle, marginBottom: 14 }}>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center', fontSize: 13 }}>
                <span style={{ color: C.dim }}>Python</span><b>{report.python.version}</b>
                <span style={{ color: C.dimmer }}>({report.python.implementation})</span>
                <Pill ok={!report.python.frozen}>{report.python.frozen ? '冻结包 · 无 pip' : '源码环境 · 可用 pip'}</Pill>
                <Pill ok={report.python.pip_available}>{report.python.pip_available ? 'pip 可用' : 'pip 不可用'}</Pill>
              </div>
              <div style={{ marginTop: 10, fontSize: 12, color: C.dim, lineHeight: 1.7 }}>
                扩展目录：<code style={{ color: C.text }}>{report.skillenv.path}</code>
                <br />
                注入状态：
                <Pill ok={report.skillenv.python_path_in_sys_path}>python 已入 sys.path</Pill>{' '}
                <Pill ok={report.skillenv.bin_in_path}>bin 已入 PATH</Pill>{' '}
                <Pill ok={report.skillenv.active}>已激活</Pill>
                <br />
                可写：{Object.entries(report.skillenv.writable).map(([key, value]) => `${key}=${value ? '是' : '否'}`).join(' · ')}
              </div>
            </div>

            <div style={{ ...cardStyle, marginBottom: 14 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                <b style={{ fontSize: 13 }}>依赖体检</b>
                <Pill ok={missing.length === 0}>{missing.length === 0 ? '全部就绪' : `缺 ${missing.length} 个`}</Pill>
                <span style={{ fontSize: 12, color: C.dimmer }}>已就绪 {ready}/{report.packages.length}</span>
              </div>
              <div style={{ fontSize: 12, color: C.dimmer, marginBottom: 10 }}>
                冻结包里没有 pip：把对应 <code>.whl</code> 放进 <code>{report.skillenv.path}\wheels</code>，再点下面的「安装 wheels 目录」。
                {report.install_hint !== '' && <><br />{report.install_hint}</>}
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {report.packages.map((row) => {
                  const probeState = probes[row.module]
                  return (
                    <div key={row.module} style={{
                      display: 'flex', alignItems: 'center', gap: 10, fontSize: 12,
                      padding: '6px 8px', borderRadius: 8, background: row.available ? 'transparent' : 'rgba(255,176,58,0.06)',
                    }}>
                      <span style={{ width: 118, color: C.text, fontFamily: 'ui-monospace, monospace' }}>{row.module}</span>
                      <span style={{ width: 170, color: C.dimmer }}>{row.use}</span>
                      <Pill ok={row.available}>{row.available ? '已就绪' : '缺失'}</Pill>
                      {!row.available && <span style={{ color: C.dimmer }}>pip: {row.pip}</span>}
                      <button type="button" style={{ ...btnStyle, marginLeft: 'auto', padding: '2px 8px', fontSize: 11 }}
                        onClick={() => void probe(row.module)}>验证导入</button>
                      {probeState !== undefined && (
                        <span style={{ color: probeState.ok ? C.accent : C.danger, maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {probeState.ok ? `导入成功${probeState.version ? ` v${probeState.version}` : ''}` : `失败：${probeState.error ?? ''}`}
                        </span>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>

            <div style={{ ...cardStyle, marginBottom: 14 }}>
              <b style={{ fontSize: 13 }}>安装依赖（离线 wheel）</b>
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 10 }}>
                <button type="button" style={btnStyle} disabled={busy} onClick={() => void installFromWheelsDir()}>
                  安装 wheels 目录里的全部 .whl
                </button>
              </div>
              <div style={{ display: 'flex', gap: 10, marginTop: 10 }}>
                <input
                  value={wheelPath}
                  onChange={(event) => { setWheelPath(event.target.value) }}
                  placeholder="或填单个 .whl 的绝对路径，例如 D:\下载\opencv_python_headless-….whl"
                  style={{
                    flex: 1, padding: '7px 10px', borderRadius: 8, fontSize: 13, color: C.text,
                    background: '#0b0b0e', border: `1px solid ${C.line}`, font: 'inherit',
                  }}
                />
                <button type="button" style={btnStyle} disabled={busy} onClick={() => void installOne()}>安装这个 wheel</button>
              </div>
              {notice !== null && <div style={{ marginTop: 10, fontSize: 12, color: C.warn }}>{notice}</div>}
              <div style={{ marginTop: 10, fontSize: 12, color: C.dimmer, lineHeight: 1.7 }}>
                ★ 只解压、不联网、不执行包内代码。带编译扩展的包必须与当前解释器 ABI 匹配；
                装完点上面的「验证导入」，能真 import 才算装上。
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
