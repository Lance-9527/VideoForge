/**
 * VideoForge model registry: four platform groups (text / image / video / audio)
 * with per-platform keys, model choice, base-URL override, connectivity test and
 * "set current".
 *
 * Degrades by itself: when `GET /api/models/registry` is unavailable (older
 * sidecar), only this block is replaced by one explanatory line — the rest of
 * the settings page keeps working.
 */
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { SIDECAR, categoryCounts, effectiveBaseUrl, secretGroupOf } from './settings-model.ts'
import type {
  ModelRegistry,
  ModelSelectResult,
  ModelTestResult,
  RegistryCategory,
  RegistryProvider,
  SecretGroup,
} from './settings-model.ts'
import { settingsCopy } from './locale/settings.ts'

const colors = {
  text: '#f0f0f3',
  dim: '#9a9aa4',
  line: '#303039',
  accent: '#7ac130',
  warn: '#ffb03a',
  orange: '#ff9f43',
  red: '#ff6b6b',
  panel: '#19191e',
}
const control = { width: '100%', padding: '7px 9px', borderRadius: 7, border: '1px solid #303039', background: '#151519', color: colors.text, font: 'inherit', boxSizing: 'border-box' as const }
const button = { padding: '6px 11px', borderRadius: 8, border: '1px solid #303039', background: '#222228', color: colors.text, cursor: 'pointer', font: 'inherit', fontSize: 12 }

/** Foreign text/image endpoints measured to time out when dialed directly from this machine. */
const FOREIGN_BASE_HOSTS = ['api.openai.com', 'api.anthropic.com', 'generativelanguage.googleapis.com', 'openrouter.ai', 'api.mistral.ai', 'api.cohere.com', 'api.x.ai']

/** Local platforms the backend never asks a key for. */
const KEYLESS_PROVIDERS = new Set(['ollama', 'edge', 'local'])

type Tone = 'green' | 'amber' | 'orange' | 'red' | 'gray'
const toneColor: Record<Tone, string> = { green: colors.accent, amber: colors.warn, orange: colors.orange, red: colors.red, gray: colors.dim }
type LoadState = 'loading' | 'ready' | 'unavailable'

interface TestState {
  readonly running: boolean
  readonly result?: ModelTestResult
  readonly failure?: string
}

interface SelectState {
  readonly running: boolean
  readonly ok?: boolean
  readonly message?: string
}

/** Props handed down by the settings section: key edits stay in its SecretEdits state. */
export interface RegistrySectionProps {
  keyInput: (group: SecretGroup, provider: string) => ReactNode
  onSaveKeys: () => void
  saving: boolean
  settingsReady: boolean
}

function FieldRow({ label, children }: { label: string; children: ReactNode }) {
  return <label style={{ display: 'grid', gridTemplateColumns: 'minmax(86px, 22%) minmax(0, 1fr)', gap: 10, alignItems: 'center', padding: '5px 0' }}>
    <span style={{ color: colors.dim, fontSize: 12 }}>{label}</span>
    <span style={{ minWidth: 0 }}>{children}</span>
  </label>
}

function Badge({ label, tone }: { label: string; tone: Tone }) {
  const color = toneColor[tone]
  return <span style={{ border: '1px solid ' + (tone === 'gray' ? colors.line : color), color, borderRadius: 999, padding: '1px 7px', fontSize: 11, lineHeight: '17px', whiteSpace: 'nowrap' }}>{label}</span>
}

/** Sidecar APIs answer either bare JSON or the legacy `{code,message,data}` envelope. */
function unwrapEnvelope(payload: unknown): unknown {
  if (payload !== null && typeof payload === 'object' && 'code' in payload) {
    const body = payload as { code?: unknown; message?: unknown; data?: unknown }
    if (body.code !== '000000' || body.data === undefined) {
      throw new Error(typeof body.message === 'string' && body.message !== '' ? body.message : '服务返回了错误响应')
    }
    return body.data
  }
  return payload
}

async function sidecarJson<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(SIDECAR + path, init)
  if (!response.ok) throw new Error('HTTP ' + String(response.status))
  return unwrapEnvelope(await response.json()) as T
}

function getRegistry(signal?: AbortSignal): Promise<ModelRegistry> {
  return sidecarJson<ModelRegistry>('/api/models/registry', signal === undefined ? {} : { signal })
}

function postModelAction<T>(path: string, payload: Record<string, unknown>): Promise<T> {
  return sidecarJson<T>(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) })
}

function domId(...parts: string[]): string {
  return parts.map((part) => part.replace(/[^A-Za-z0-9_-]+/g, '-')).join('-')
}

function withoutKey<T>(record: Record<string, T>, key: string): Record<string, T> {
  if (record[key] === undefined) return record
  const next: Record<string, T> = {}
  for (const [entryKey, value] of Object.entries(record)) {
    if (entryKey !== key) next[entryKey] = value
  }
  return next
}

/** The measured direct-access failure only concerns foreign text/image platforms. */
function needsRelayHint(categoryId: string, provider: RegistryProvider): boolean {
  if (categoryId !== 'text' && categoryId !== 'image') return false
  const base = effectiveBaseUrl(provider)
  return FOREIGN_BASE_HOSTS.some((host) => base.includes(host))
}

/** Mirror a successful `select` locally so the card flips before the quiet reload lands. */
function patchSelection(registry: ModelRegistry, categoryId: string, providerId: string, model: string, baseUrl: string): ModelRegistry {
  return {
    ...registry,
    categories: (registry.categories ?? []).map((category) => {
      if (category.id !== categoryId) return category
      return {
        ...category,
        providers: (category.providers ?? []).map((provider) => {
          if (provider.id !== providerId) return { ...provider, active: false }
          const defaultBase = provider.base_url ?? ''
          return {
            ...provider,
            active: true,
            selected_model: model,
            base_url_override: baseUrl,
            ...(baseUrl === '' ? { base_url_effective: defaultBase } : { base_url_effective: baseUrl }),
          }
        }),
      }
    }),
  }
}

export function VfRegistrySection({ keyInput, onSaveKeys, saving, settingsReady }: RegistrySectionProps) {
  const copy = settingsCopy()
  const [registry, setRegistry] = useState<ModelRegistry | null>(null)
  const [loadState, setLoadState] = useState<LoadState>('loading')
  const [active, setActive] = useState('text')
  const [models, setModels] = useState<Record<string, string>>({})
  const [bases, setBases] = useState<Record<string, string>>({})
  const [tests, setTests] = useState<Record<string, TestState>>({})
  const [selections, setSelections] = useState<Record<string, SelectState>>({})
  const categories = registry?.categories ?? []

  const applyRegistry = (data: ModelRegistry): void => {
    const loaded = data.categories ?? []
    setRegistry(data)
    if (loaded.length === 0) {
      setLoadState('unavailable')
      return
    }
    setLoadState('ready')
    setActive((current) => (loaded.some((category) => category.id === current) ? current : loaded[0]?.id ?? current))
  }

  const load = async (signal?: AbortSignal, quiet = false): Promise<void> => {
    try {
      const data = await getRegistry(signal)
      if (signal?.aborted === true) return
      applyRegistry(data)
    } catch {
      if (signal?.aborted === true) return
      // A quiet refresh keeps the directory on screen; only a failed first load hides the block.
      if (!quiet) {
        setRegistry(null)
        setLoadState('unavailable')
      }
    }
  }

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => { controller.abort() }
    // The registry is fetched once per mount; the reload button reuses `load`.
  }, [])

  const keyOf = (categoryId: string, providerId: string): string => categoryId + ':' + providerId
  const modelOf = (key: string, provider: RegistryProvider): string => {
    const saved = provider.selected_model !== undefined && provider.selected_model !== ''
      ? provider.selected_model
      : provider.default_model ?? provider.models?.[0]?.id ?? ''
    return models[key] ?? saved
  }
  const baseOf = (key: string, provider: RegistryProvider): string => bases[key] ?? effectiveBaseUrl(provider)

  const changeModel = (key: string, value: string): void => {
    setModels((current) => ({ ...current, [key]: value }))
    setTests((current) => withoutKey(current, key))
  }
  const changeBase = (key: string, value: string): void => {
    setBases((current) => ({ ...current, [key]: value }))
    setTests((current) => withoutKey(current, key))
  }

  const runTest = async (category: RegistryCategory, provider: RegistryProvider): Promise<void> => {
    const key = keyOf(category.id, provider.id)
    const model = modelOf(key, provider).trim()
    const base = baseOf(key, provider).trim()
    const payload: Record<string, unknown> = { category: category.id, provider: provider.id, model }
    // Unsaved relay addresses are forwarded when the backend understands them; older
    // backends ignore the field and test the saved/effective address instead.
    if (base !== '' && base !== effectiveBaseUrl(provider)) payload.base_url = base
    setTests((current) => ({ ...current, [key]: { running: true } }))
    try {
      const result = await postModelAction<ModelTestResult>('/api/models/test', payload)
      setTests((current) => ({ ...current, [key]: { running: false, result } }))
    } catch (error) {
      setTests((current) => ({ ...current, [key]: { running: false, failure: error instanceof Error ? error.message : String(error) } }))
    }
  }

  const runSelect = async (category: RegistryCategory, provider: RegistryProvider): Promise<void> => {
    if (provider.status === 'reserved') return
    const key = keyOf(category.id, provider.id)
    const model = modelOf(key, provider).trim()
    const base = baseOf(key, provider).trim()
    const payload: Record<string, unknown> = { category: category.id, provider: provider.id, model }
    if (base === '') payload.clear_base_url = true
    else if (base !== effectiveBaseUrl(provider)) payload.base_url = base
    setSelections((current) => ({ ...current, [key]: { running: true } }))
    try {
      await postModelAction<ModelSelectResult>('/api/models/select', payload)
      setSelections((current) => ({ ...current, [key]: { running: false, ok: true, message: copy.registrySelected } }))
      const selectedModel = model !== '' ? model : provider.default_model ?? ''
      setRegistry((current) => (current === null ? current : patchSelection(current, category.id, provider.id, selectedModel, base)))
      setModels((current) => withoutKey(current, key))
      setBases((current) => withoutKey(current, key))
      void load(undefined, true)
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error)
      setSelections((current) => ({ ...current, [key]: { running: false, ok: false, message: copy.registrySelectFailed + '：' + reason } }))
    }
  }

  const resultTone = (result: ModelTestResult): Tone => {
    if (result.status === 'verified') return 'green'
    if (result.status === 'unauthorized') return 'orange'
    if (result.status === 'unverified' || result.status === 'no_key') return 'gray'
    return 'red'
  }

  const resultLabel = (result: ModelTestResult): string => {
    switch (result.status) {
      case 'verified': return copy.registryResultVerified
      case 'unauthorized': return copy.registryResultUnauthorized
      case 'unverified': return copy.registryResultUnverified
      case 'no_key': return copy.registryResultNoKey
      default: return copy.registryResultError
    }
  }

  const clip = (text: string): string => (text.length > 400 ? text.slice(0, 400) + '…' : text)

  const renderTestResult = (result: ModelTestResult): ReactNode => {
    const status = result.status ?? 'error'
    const details: string[] = []
    if (typeof result.latency_ms === 'number') details.push(String(result.latency_ms) + ' ms')
    if (typeof result.status_code === 'number' && result.status_code > 0) details.push('HTTP ' + String(result.status_code))
    const message = result.message ?? ''
    return <span style={{ color: toneColor[resultTone(result)], fontSize: 12, lineHeight: 1.65, display: 'block', marginTop: 6 }}>
      <strong>{resultLabel(result)}</strong>
      {details.length > 0 && <span> · {details.join(' · ')}</span>}
      {result.model !== undefined && result.model !== '' && <span style={{ color: colors.dim }}> · {result.model}</span>}
      {status !== 'verified' && message !== '' && <span style={{ display: 'block', color: colors.dim, fontSize: 11.5, overflowWrap: 'anywhere' }}>{clip(message)}</span>}
      {status !== 'verified' && result.url !== undefined && result.url !== '' && <span style={{ display: 'block', color: colors.dim, fontSize: 11, overflowWrap: 'anywhere' }}>{result.url}</span>}
    </span>
  }

  const providerBadge = (provider: RegistryProvider): { readonly label: string; readonly tone: Tone } => {
    if (provider.status === 'reserved') return { label: copy.registryReserved, tone: 'gray' }
    if (provider.status === 'verified') return { label: copy.registryVerified, tone: 'green' }
    return { label: copy.registryImplemented, tone: 'amber' }
  }

  const renderProvider = (category: RegistryCategory, provider: RegistryProvider): ReactNode => {
    const key = keyOf(category.id, provider.id)
    const test = tests[key]
    const selection = selections[key]
    const reserved = provider.status === 'reserved'
    const badge = providerBadge(provider)
    const keyless = KEYLESS_PROVIDERS.has(provider.id)
    const group = keyless ? null : secretGroupOf(provider)
    const env = keyless ? [] : provider.env ?? []
    const envPresent = provider.env_present ?? []
    const configured = provider.configured === true || envPresent.length > 0
    const modelListId = domId('vf-registry-model', category.id, provider.id)
    return <article key={key} style={{ padding: '12px 0', borderBottom: '1px solid ' + colors.line }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <strong style={{ fontSize: 13 }}>{provider.label ?? provider.id}</strong>
        <Badge label={badge.label} tone={badge.tone} />
        {(group !== null || env.length > 0) && <Badge label={configured ? copy.registryKeySet : copy.registryNoKey} tone={configured ? 'gray' : 'amber'} />}
        {provider.active === true && <span style={{ color: colors.accent, fontSize: 11 }}>{copy.registryActive}</span>}
        {provider.doc !== undefined && provider.doc !== '' && <a href={provider.doc} target="_blank" rel="noreferrer" style={{ color: '#8fb7ff', fontSize: 11 }}>{copy.registryDoc}</a>}
      </div>
      {provider.notes !== undefined && provider.notes !== '' && <p style={{ color: colors.dim, fontSize: 11.5, lineHeight: 1.65, margin: '5px 0 3px' }}>{provider.notes}</p>}
      {reserved && <p style={{ color: colors.dim, fontSize: 11.5, lineHeight: 1.65, margin: '4px 0 2px' }}>{copy.registryReservedHint}</p>}
      <FieldRow label={copy.registryModel}>
        <input style={control} list={modelListId} disabled={reserved} value={modelOf(key, provider)} placeholder={copy.registryModelHint} onChange={(event) => { changeModel(key, event.target.value) }} />
        <datalist id={modelListId}>{(provider.models ?? []).map((model) => <option key={model.id} value={model.id}>{model.label ?? model.id}</option>)}</datalist>
      </FieldRow>
      <FieldRow label={copy.registryBaseUrl}>
        <input style={control} disabled={reserved} value={baseOf(key, provider)} placeholder={copy.registryBaseUrlHint} onChange={(event) => { changeBase(key, event.target.value) }} />
      </FieldRow>
      {needsRelayHint(category.id, provider) && <p style={{ color: colors.dim, fontSize: 11, lineHeight: 1.6, margin: '1px 0 3px' }}>{copy.registryRelayHint}</p>}
      {group !== null && <FieldRow label={copy.registryKey}>
        {keyInput(group, provider.id)}
        {envPresent.length > 0 && <small style={{ display: 'block', color: colors.dim, fontSize: 11, marginTop: 4 }}>{copy.registryKeyEnv}：{envPresent.join(' / ')}</small>}
        {envPresent.length === 0 && !configured && <small style={{ display: 'block', color: colors.dim, fontSize: 11, marginTop: 4 }}>{copy.registryKeyPending}</small>}
      </FieldRow>}
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginTop: 7 }}>
        <button type="button" style={button} disabled={reserved || test?.running === true} onClick={() => { void runTest(category, provider) }}>{test?.running === true ? copy.registryTesting : copy.registryTest}</button>
        <button type="button" style={button} disabled={reserved || selection?.running === true} onClick={() => { void runSelect(category, provider) }}>{selection?.running === true ? copy.registrySelecting : copy.registrySelect}</button>
        {selection?.message !== undefined && <span role="status" style={{ color: selection.ok === true ? colors.accent : colors.red, fontSize: 12 }}>{selection.message}</span>}
      </div>
      {test?.result !== undefined && <div role="status">{renderTestResult(test.result)}</div>}
      {test?.failure !== undefined && <p role="status" style={{ color: colors.red, fontSize: 12, margin: '6px 0 0' }}>{copy.registryTestFailed}：{test.failure}</p>}
    </article>
  }

  if (loadState === 'loading') return <p style={{ color: colors.dim, fontSize: 12, margin: '0 0 18px' }}>{copy.registryLoading}</p>
  // Older sidecars have no registry: one hint line replaces the block, the rest of the page is untouched.
  if (loadState === 'unavailable' || registry === null) return <p role="status" style={{ color: colors.dim, fontSize: 12, margin: '0 0 18px' }}>{copy.registryUnavailable}</p>

  const current = categories.find((category) => category.id === active) ?? categories[0]
  if (current === undefined) return <p role="status" style={{ color: colors.dim, fontSize: 12, margin: '0 0 18px' }}>{copy.registryUnavailable}</p>

  return <section aria-labelledby="vf-registry-title" style={{ marginBottom: 22 }}>
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
      <h3 id="vf-registry-title" style={{ fontSize: 15, margin: '0 0 5px' }}>{copy.registryTitle}</h3>
      <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
        <button type="button" style={button} onClick={() => { void load(undefined, true) }}>{copy.registryReload}</button>
        <button type="button" style={button} disabled={!settingsReady || saving} title={copy.registrySaveHint} onClick={onSaveKeys}>{copy.registrySaveKey}</button>
      </div>
    </div>
    <p style={{ color: colors.dim, fontSize: 12, lineHeight: 1.65, margin: '0 0 8px' }}>{copy.registryNote}</p>
    <div role="tablist" aria-label={copy.registryTitle} style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
      {categories.map((category) => {
        const counts = categoryCounts(category, registry.counts)
        const selected = category.id === current.id
        return <button key={category.id} type="button" role="tab" aria-selected={selected} onClick={() => { setActive(category.id) }} style={{ ...button, borderColor: selected ? colors.accent : colors.line, color: selected ? colors.text : colors.dim, background: selected ? '#222228' : '#151519' }}>
          {category.label ?? category.id}
          <span style={{ marginLeft: 6, color: colors.dim, fontSize: 11 }}>{copy.registryConfigured} {counts.configured} / {copy.registryTotal} {counts.total}</span>
        </button>
      })}
    </div>
    {current.hint !== undefined && current.hint !== '' && <p style={{ color: colors.dim, fontSize: 12, lineHeight: 1.6, margin: '0 0 8px' }}>{current.hint}</p>}
    <div style={{ border: '1px solid ' + colors.line, borderRadius: 10, padding: '2px 14px', background: colors.panel }}>
      {(current.providers ?? []).length === 0
        ? <p style={{ color: colors.dim, fontSize: 12, padding: '10px 0' }}>{copy.registryEmpty}</p>
        : (current.providers ?? []).map((provider) => renderProvider(current, provider))}
    </div>
  </section>
}
