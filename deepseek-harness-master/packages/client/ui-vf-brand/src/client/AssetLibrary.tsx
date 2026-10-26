/**
 * VideoForge 资产库：跨项目复用 + 工作区级资产设置。
 *
 * 为什么单独成文件：Panel.tsx 已经承载资产/后期两大块，资产库自带弹窗与状态，
 * 塞进去会让那个文件更难读；这里出问题也能整段摘掉。
 *
 * 用的都是 sidecar **已有**的接口（没有新增后端能力，纯客户端补齐）：
 *   GET  /api/library/characters?exclude_project=<pid>
 *   GET  /api/library/scenes?exclude_project=<pid>
 *   POST /api/projects/<pid>/characters/import   { ids: [...] }
 *   POST /api/projects/<pid>/scenes/import       { ids: [...] }
 *
 * 设置项只有「真的会生效」的：范围筛选与只看有图，都是本地立刻起作用；
 * 不放假开关（点了没反应那种）。
 */
import { useCallback, useEffect, useState } from 'react'

const SIDECAR = 'http://127.0.0.1:8765'

const C = {
  line: 'rgba(255,255,255,0.10)',
  card: 'rgba(255,255,255,0.03)',
  text: '#f0f0f3',
  dim: '#b8b8c0',
  dimmer: '#7a7a85',
  accent: '#7ac130',
} as const

/** 资产库里的一条角色/场景。 */
interface LibraryAsset {
  readonly id?: string
  readonly name?: string
  readonly project_id?: string
  readonly project_title?: string
  readonly image_path?: string | null
  readonly portrait_path?: string | null
  readonly ref_image_path?: string | null
  readonly description?: string
}

/** 工作区资产设置（存 localStorage，纯客户端即时生效）。 */
interface AssetSettings {
  /** 资产库范围：all=全部项目；excludeCurrent=排除当前项目（默认，避免重复导入自己） */
  readonly scope: 'all' | 'excludeCurrent'
  /** 只看已经有图的资产（没有图的资产导过来也还得再生成一次）。 */
  readonly withImageOnly: boolean
}

const SETTINGS_KEY = 'vf.assetLibrary'

function readSettings(): AssetSettings {
  try {
    const raw = window.localStorage.getItem(SETTINGS_KEY)
    if (raw !== null) {
      const parsed = JSON.parse(raw) as Partial<AssetSettings>
      return {
        scope: parsed.scope === 'all' ? 'all' : 'excludeCurrent',
        withImageOnly: parsed.withImageOnly === true,
      }
    }
  } catch { /* 坏了就用默认值 */ }
  return { scope: 'excludeCurrent', withImageOnly: false }
}

function writeSettings(next: AssetSettings): void {
  try { window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(next)) } catch { /* 忽略：只影响下次 */ }
}

/** 取资产缩略图 url（没有图就返回 null）。 */
function imageUrl(asset: LibraryAsset): string | null {
  const path = asset.image_path ?? asset.portrait_path ?? asset.ref_image_path ?? null
  if (typeof path !== 'string' || path === '') return null
  return `${SIDECAR}/api/media?path=${encodeURIComponent(path)}`
}

const overlayStyle = {
  position: 'fixed' as const, inset: 0, zIndex: 9_000,
  background: 'rgba(0,0,0,0.55)', display: 'flex',
  alignItems: 'center', justifyContent: 'center',
}

const dialogStyle = {
  width: 'min(860px, 92vw)', maxHeight: '82vh', display: 'flex', flexDirection: 'column' as const,
  background: '#18181c', border: `1px solid ${C.line}`, borderRadius: 12,
  boxShadow: '0 24px 60px rgba(0,0,0,0.55)',
}

/**
 * 资产库入口 + 弹窗。
 * @param props.projectId - 当前项目 id；导入目标是它。
 * @param props.onImported - 导入成功后的回调（面板可据此刷新资产列表）。
 */
export function AssetLibrary({ projectId, onImported }: {
  projectId: string
  onImported?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState<'characters' | 'scenes'>('characters')
  const [items, setItems] = useState<readonly LibraryAsset[]>([])
  const [selected, setSelected] = useState<readonly string[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [settings, setSettings] = useState<AssetSettings>(() => readSettings())
  const [showSettings, setShowSettings] = useState(false)

  const load = useCallback(async (nextKind: 'characters' | 'scenes', nextSettings: AssetSettings) => {
    setLoading(true)
    setError('')
    try {
      const query = nextSettings.scope === 'excludeCurrent'
        ? `?exclude_project=${encodeURIComponent(projectId)}`
        : ''
      const response = await fetch(`${SIDECAR}/api/library/${nextKind}${query}`)
      const body = await response.json() as { data?: Record<string, LibraryAsset[]> }
      const list = body.data?.[nextKind] ?? []
      setItems(Array.isArray(list) ? list : [])
    } catch (cause) {
      setError(`读取资产库失败：${String(cause)}`)
      setItems([])
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    if (!open) return
    setSelected([])
    void load(kind, settings)
  }, [open, kind, settings, load])

  const visible = settings.withImageOnly ? items.filter((item) => imageUrl(item) !== null) : items

  const toggle = (id: string): void => {
    setSelected((current) => current.includes(id) ? current.filter((x) => x !== id) : [...current, id])
  }

  const doImport = async (): Promise<void> => {
    if (selected.length === 0) return
    setBusy(true)
    setError('')
    try {
      const response = await fetch(`${SIDECAR}/api/projects/${encodeURIComponent(projectId)}/${kind}/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: selected }),
      })
      const body = await response.json() as { code?: string; message?: string }
      if (!response.ok || (body.code !== undefined && body.code !== '000000')) {
        throw new Error(body.message ?? `HTTP ${response.status}`)
      }
      setSelected([])
      setOpen(false)
      onImported?.()
    } catch (cause) {
      setError(`导入失败：${String(cause)}`)
    } finally {
      setBusy(false)
    }
  }

  const saveSettings = (next: AssetSettings): void => {
    setSettings(next)
    writeSettings(next)
  }

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <button
          type="button"
          onClick={() => { setOpen(true) }}
          style={{
            padding: '6px 12px', borderRadius: 8, fontSize: 12.5, cursor: 'pointer', font: 'inherit',
            border: `1px solid ${C.line}`, background: C.card, color: C.text,
          }}
        >
          📚 从资产库导入（跨项目复用）
        </button>
        <button
          type="button"
          onClick={() => { setShowSettings((v) => !v) }}
          style={{
            padding: '6px 12px', borderRadius: 8, fontSize: 12.5, cursor: 'pointer', font: 'inherit',
            border: `1px solid ${C.line}`, background: 'transparent', color: C.dim,
          }}
        >
          ⚙ 工作区资产设置
        </button>
        <span style={{ fontSize: 11.5, color: C.dimmer }}>
          当前：{settings.scope === 'all' ? '全部项目' : '排除本项目'}
          {settings.withImageOnly ? ' · 只看有图的' : ''}
        </span>
      </div>

      {showSettings && (
        <div style={{ marginBottom: 14, padding: '10px 12px', border: `1px solid ${C.line}`, borderRadius: 10, background: C.card }}>
          <div style={{ fontSize: 12, color: C.dim, marginBottom: 8 }}>
            工作区资产设置（立刻生效，只影响这个工作区的资产库视图）
          </div>
          <label style={{ display: 'block', fontSize: 12.5, marginBottom: 8, color: C.text }}>
            资产库范围：
            <select
              value={settings.scope}
              onChange={(event) => { saveSettings({ ...settings, scope: event.target.value === 'all' ? 'all' : 'excludeCurrent' }) }}
              style={{ marginLeft: 8, padding: '3px 6px', borderRadius: 6, border: `1px solid ${C.line}`, background: '#111114', color: C.text, font: 'inherit', fontSize: 12 }}
            >
              <option value="excludeCurrent">排除本项目（推荐，防止把自己再导一遍）</option>
              <option value="all">全部项目</option>
            </select>
          </label>
          <label style={{ display: 'block', fontSize: 12.5, color: C.text }}>
            <input
              type="checkbox"
              checked={settings.withImageOnly}
              onChange={(event) => { saveSettings({ ...settings, withImageOnly: event.target.checked }) }}
            />{' '}
            只看已经有图（定妆照/场景图）的资产
          </label>
        </div>
      )}

      {open && (
        <div style={overlayStyle} onClick={() => { setOpen(false) }}>
          <div style={dialogStyle} onClick={(event) => { event.stopPropagation() }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '14px 18px', borderBottom: `1px solid ${C.line}` }}>
              <div style={{ fontSize: 14, fontWeight: 650, color: C.text }}>资产库</div>
              <div style={{ display: 'flex', gap: 6 }}>
                {(['characters', 'scenes'] as const).map((key) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => { setKind(key) }}
                    style={{
                      padding: '3px 10px', borderRadius: 999, fontSize: 12, cursor: 'pointer', font: 'inherit',
                      border: `1px solid ${kind === key ? 'rgba(122,193,48,0.5)' : C.line}`,
                      background: kind === key ? 'rgba(122,193,48,0.14)' : 'transparent',
                      color: kind === key ? C.text : C.dim,
                    }}
                  >
                    {key === 'characters' ? '角色' : '场景'}
                  </button>
                ))}
              </div>
              <span style={{ marginLeft: 'auto', fontSize: 12, color: C.dimmer }}>
                共 {visible.length} 个 · 已选 {selected.length}
              </span>
              <button
                type="button"
                onClick={() => { setOpen(false) }}
                style={{ border: 'none', background: 'transparent', color: C.dimmer, fontSize: 16, cursor: 'pointer' }}
              >
                ✕
              </button>
            </div>

            <div style={{ padding: '12px 18px', overflowY: 'auto', flex: 1 }}>
              {loading && <div style={{ fontSize: 13, color: C.dim }}>读取中…</div>}
              {error !== '' && <div style={{ fontSize: 13, color: '#ff6b6b', marginBottom: 10 }}>{error}</div>}
              {!loading && visible.length === 0 && (
                <div style={{ fontSize: 13, color: C.dim, lineHeight: 1.8 }}>
                  资产库里还没有可复用的{kind === 'characters' ? '角色' : '场景'}。
                  <br />先在别的项目里建好角色/场景（或生成定妆照），它们就会出现在这里。
                </div>
              )}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: 10 }}>
                {visible.map((item) => {
                  const id = String(item.id ?? '')
                  const url = imageUrl(item)
                  const picked = selected.includes(id)
                  return (
                    <button
                      key={id}
                      type="button"
                      onClick={() => { if (id !== '') toggle(id) }}
                      style={{
                        padding: 0, borderRadius: 10, overflow: 'hidden', textAlign: 'left', cursor: 'pointer', font: 'inherit',
                        border: `2px solid ${picked ? 'rgba(122,193,48,0.75)' : C.line}`,
                        background: C.card,
                      }}
                    >
                      {url !== null
                        ? <img src={url} alt={item.name ?? ''} style={{ width: '100%', display: 'block', aspectRatio: '1 / 1', objectFit: 'cover' }} />
                        : <div style={{ width: '100%', aspectRatio: '1 / 1', display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.dimmer, fontSize: 12 }}>还没有图</div>}
                      <div style={{ padding: '6px 8px' }}>
                        <div style={{ fontSize: 12.5, color: C.text, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {picked ? '✓ ' : ''}{item.name ?? '未命名'}
                        </div>
                        <div style={{ fontSize: 11, color: C.dimmer, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {item.project_title ?? '其它项目'}
                        </div>
                      </div>
                    </button>
                  )
                })}
              </div>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 18px', borderTop: `1px solid ${C.line}` }}>
              <span style={{ fontSize: 12, color: C.dimmer }}>
                导入是「复制」：目标项目会得到独立副本，改这边不影响原项目。
              </span>
              <span style={{ marginLeft: 'auto' }} />
              <button
                type="button"
                onClick={() => { setOpen(false) }}
                style={{ padding: '6px 14px', borderRadius: 8, fontSize: 12.5, cursor: 'pointer', font: 'inherit', border: `1px solid ${C.line}`, background: 'transparent', color: C.dim }}
              >
                取消
              </button>
              <button
                type="button"
                disabled={busy || selected.length === 0}
                onClick={() => { void doImport() }}
                style={{
                  padding: '6px 14px', borderRadius: 8, fontSize: 12.5, cursor: busy || selected.length === 0 ? 'not-allowed' : 'pointer', font: 'inherit',
                  border: '1px solid rgba(122,193,48,0.5)',
                  background: busy || selected.length === 0 ? 'rgba(122,193,48,0.06)' : 'rgba(122,193,48,0.18)',
                  color: busy || selected.length === 0 ? C.dimmer : C.text,
                }}
              >
                {busy ? '导入中…' : `导入到本项目（${selected.length}）`}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
