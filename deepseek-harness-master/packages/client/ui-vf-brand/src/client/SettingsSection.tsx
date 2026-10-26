/** VideoForge settings use the installed backend catalogs and explicit partial updates. */
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { SIDECAR, emptySecretEdits, providerOptions, settingsPatch, ttsModelId } from './settings-model.ts'
import type { ModelProvider, SecretEdits, SecretGroup, VfSettings, VideoCatalog } from './settings-model.ts'
import { VfRegistrySection } from './RegistrySection.tsx'
import { settingsCopy } from './locale/settings.ts'

const colors = { text: '#f0f0f3', dim: '#9a9aa4', line: '#303039', accent: '#7ac130', warn: '#ffb03a' }
const control = { width: '100%', padding: '7px 9px', borderRadius: 7, border: '1px solid #303039', background: '#151519', color: colors.text, font: 'inherit', boxSizing: 'border-box' as const }
const button = { padding: '8px 13px', borderRadius: 8, border: '1px solid #303039', background: '#222228', color: colors.text, cursor: 'pointer', font: 'inherit' }

function Row({ label, hint, children }: { label: string; hint?: string | undefined; children: ReactNode }) {
  return <label style={{ display: 'grid', gridTemplateColumns: 'minmax(110px, 30%) minmax(0, 1fr)', gap: 12, alignItems: 'center', padding: '9px 0', borderBottom: '1px solid #303039' }}>
    <span>{label}{hint && <small style={{ display: 'block', color: colors.dim, lineHeight: 1.5, marginTop: 3 }}>{hint}</small>}</span>
    <span style={{ minWidth: 0 }}>{children}</span>
  </label>
}

function Group({ title, note, children }: { title: string; note: string; children: ReactNode }) {
  return <section style={{ marginBottom: 22 }}>
    <h3 style={{ fontSize: 15, margin: '0 0 5px' }}>{title}</h3>
    <p style={{ color: colors.dim, fontSize: 12, lineHeight: 1.6, margin: '0 0 8px' }}>{note}</p>
    <div style={{ border: '1px solid #303039', borderRadius: 10, padding: '2px 14px', background: '#19191e' }}>{children}</div>
  </section>
}

interface ProviderData { providers?: ModelProvider[] }
interface CatalogData { catalog?: Record<string, VideoCatalog> }

async function request<T>(path: string, signal?: AbortSignal, payload?: VfSettings): Promise<T> {
  const response = await fetch(SIDECAR + path, {
    ...(signal === undefined ? {} : { signal }),
    ...(payload === undefined ? {} : { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) }),
  })
  if (!response.ok) throw new Error('HTTP ' + String(response.status))
  const body = await response.json() as { code?: string; message?: string; data?: T }
  if (body.code !== '000000' || body.data === undefined) throw new Error(body.message ?? 'Invalid settings response')
  return body.data
}

const scalarFields = ['llm_provider', 'llm_model', 'llm_base_url', 'llm_effort', 'enable_llm_local', 'ollama_url', 'ollama_base_url', 'ollama_model', 'default_model_provider', 'default_model_name', 'default_aspect_ratio', 'default_resolution', 'default_video_resolution', 'allow_video_spend', 'default_video_budget_yuan', 'max_shot_seconds', 'voiceover_mode', 'voice_rate', 'voice_volume', 'image_provider', 'image_model', 'lipsync_mode', 'dialogue_backfill', 'output_dir', 'cache_dir', 'ffmpeg_path', 'public_base_url', 'public_sync_dir', 'tunnel_named', 'tunnel_hostname'] as const

function draftOf(settings: VfSettings): VfSettings {
  return { ...Object.fromEntries(scalarFields.filter((key) => settings[key] !== undefined).map((key) => [key, settings[key]])), tts_models: { ...(settings.tts_models ?? {}) } }
}

/** Model settings preserve untouched provider keys, models and relay configuration. */
export function VfModelsSection() {
  const copy = settingsCopy()
  const [server, setServer] = useState<VfSettings | null>(null)
  const [draft, setDraft] = useState<VfSettings>({})
  const [secrets, setSecrets] = useState<SecretEdits>(emptySecretEdits)
  const [textProviders, setTextProviders] = useState<ModelProvider[]>([])
  const [videoProviders, setVideoProviders] = useState<ModelProvider[]>([])
  const [imageProviders, setImageProviders] = useState<ModelProvider[]>([])
  const [ttsProviders, setTtsProviders] = useState<ModelProvider[]>([])
  const [catalog, setCatalog] = useState<Record<string, VideoCatalog>>({})
  const [status, setStatus] = useState('')
  const [catalogWarning, setCatalogWarning] = useState(false)
  const [saving, setSaving] = useState(false)

  const loadSettings = async (signal?: AbortSignal): Promise<void> => {
    const data = await request<VfSettings>('/api/settings', signal)
    if (signal?.aborted) return
    setServer(data)
    setDraft(draftOf(data))
    setSecrets(emptySecretEdits())
  }

  useEffect(() => {
    const controller = new AbortController()
    void loadSettings(controller.signal).catch(() => {
      if (!controller.signal.aborted) setStatus(copy.loadFailed)
    })
    const queries = [
      request<ProviderData>('/api/llm/providers', controller.signal).then((data) => { if (!controller.signal.aborted) setTextProviders(data.providers ?? []) }),
      request<ProviderData>('/api/providers', controller.signal).then((data) => { if (!controller.signal.aborted) setVideoProviders(data.providers ?? []) }),
      request<ProviderData>('/api/image/providers', controller.signal).then((data) => { if (!controller.signal.aborted) setImageProviders(data.providers ?? []) }),
      request<ProviderData>('/api/tts/models', controller.signal).then((data) => { if (!controller.signal.aborted) setTtsProviders(data.providers ?? []) }),
      request<CatalogData>('/api/model-catalog?kind=video', controller.signal).then((data) => { if (!controller.signal.aborted) setCatalog(data.catalog ?? {}) }),
    ]
    void Promise.allSettled(queries).then((results) => {
      if (!controller.signal.aborted) setCatalogWarning(results.some((result) => result.status === 'rejected'))
    })
    return () => controller.abort()
  }, [])

  const patch = (values: Partial<VfSettings>): void => { setDraft((current) => ({ ...current, ...values })) }
  const textProvider = String(draft.llm_provider ?? '')
  const videoProvider = String(draft.default_model_provider ?? '')
  const imageProvider = String(draft.image_provider ?? '')
  const textInfo = textProviders.find((provider) => provider.name === textProvider)
  const videoInfo = videoProviders.find((provider) => provider.name === videoProvider)
  const videoModels = catalog[videoProvider]?.models ?? []
  const videoModel = videoModels.find((model) => model.id === draft.default_model_name)
  const resolutions = [...new Set([...(videoInfo?.supported_resolutions ?? []), ...Object.keys(videoModel?.caps ?? {}), String(draft.default_video_resolution ?? draft.default_resolution ?? '480p')])]
  const aspects = [...new Set([...(videoInfo?.supported_aspect_ratios ?? ['16:9', '9:16', '1:1']), String(draft.default_aspect_ratio ?? '16:9')])]

  const input = (key: keyof VfSettings, placeholder = ''): ReactNode => <input style={control} value={String(draft[key] ?? '')} placeholder={placeholder} onChange={(event) => { patch({ [key]: event.target.value }) }} />
  const numberInput = (key: keyof VfSettings, fallback: number, min: number, max?: number, step = 1): ReactNode => <input style={control} type="number" min={min} max={max} step={step} value={Number(draft[key] ?? fallback)} onChange={(event) => {
    const next = Number(event.target.value)
    if (Number.isFinite(next) && next >= min && (max === undefined || next <= max)) patch({ [key]: next })
  }} />
  const choice = (key: keyof VfSettings, options: readonly string[], fallback: string): ReactNode => <select style={control} value={String(draft[key] ?? fallback)} onChange={(event) => { patch({ [key]: event.target.value }) }}>{options.map((option) => <option key={option} value={option}>{option}</option>)}</select>
  const checkbox = (key: keyof VfSettings, fallback = false): ReactNode => <input type="checkbox" checked={Boolean(draft[key] ?? fallback)} onChange={(event) => { patch({ [key]: event.target.checked }) }} />
  const keyInput = (group: SecretGroup, provider: string): ReactNode => <input style={control} type="password" autoComplete="new-password" value={secrets[group][provider] ?? ''} placeholder={server?.[group]?.[provider] ? copy.keyKeep : copy.keyNew} onChange={(event) => {
    const value = event.target.value
    setSecrets((current) => ({ ...current, [group]: { ...current[group], [provider]: value } }))
  }} />
  const providerSelect = (providers: readonly ModelProvider[], selected: string, onChange: (provider: string) => void): ReactNode => <select style={control} value={selected} onChange={(event) => onChange(event.target.value)}>
    <option value="">{copy.default}</option>
    {providerOptions(providers, selected).map((provider) => <option key={provider.name} value={provider.name}>{provider.display_name ?? provider.name}</option>)}
  </select>

  const save = async (): Promise<void> => {
    if (server === null) return
    const payload = settingsPatch(server, draft, secrets)
    if (Object.keys(payload).length === 0) { setStatus(copy.noChanges); return }
    setSaving(true)
    setStatus(copy.saving)
    try {
      const saved = await request<VfSettings>('/api/settings', undefined, payload)
      setServer(saved)
      setDraft(draftOf(saved))
      setSecrets(emptySecretEdits())
      setStatus(copy.saved)
    } catch (error) {
      setStatus(copy.saveFailed + ': ' + (error instanceof Error ? error.message : String(error)))
    } finally { setSaving(false) }
  }

  return <div style={{ fontSize: 13, color: colors.text }}>
    <p style={{ color: colors.dim, lineHeight: 1.7, marginTop: 0 }}>{copy.intro}</p>
    {catalogWarning && <p role="status" style={{ color: colors.warn }}>{copy.catalogFailed}</p>}
    {server === null && <p role="status">{status || copy.loading}</p>}
    <fieldset disabled={server === null || saving} style={{ margin: 0, padding: 0, border: 0, minWidth: 0 }}>
      <VfRegistrySection keyInput={keyInput} onSaveKeys={() => { void save() }} saving={saving} settingsReady={server !== null} />
      <Group title={copy.text} note={copy.textNote}>
        <Row label={copy.provider}>{providerSelect(textProviders, textProvider, (name) => {
          const provider = textProviders.find((item) => item.name === name)
          patch({ llm_provider: name, llm_model: provider?.default_model ?? '', llm_base_url: '', enable_llm_local: name === 'ollama' })
        })}</Row>
        <Row label={copy.model} hint={copy.custom}>
          <input style={control} list="vf-text-models" value={String(draft.llm_model ?? '')} onChange={(event) => { patch({ llm_model: event.target.value }) }} />
          <datalist id="vf-text-models">{(textInfo?.available_models ?? []).map((model) => <option key={model} value={model} />)}</datalist>
        </Row>
        <Row label={copy.endpoint}>
          {input('llm_base_url')}
          <button type="button" style={{ ...button, marginTop: 6, fontSize: 11 }} onClick={() => { patch({ llm_base_url: '' }) }}>{copy.defaultEndpoint}</button>
        </Row>
        {textProvider && <Row label={copy.key}>{keyInput('llm_api_keys', textProvider)}</Row>}
        <Row label={copy.effort}><select style={control} value={String(draft.llm_effort ?? '')} onChange={(event) => { patch({ llm_effort: event.target.value }) }}><option value="">{copy.default}</option>{['low', 'medium', 'high', 'xhigh', 'max'].map((value) => <option key={value} value={value}>{value}</option>)}</select></Row>
        <Row label={copy.local}>{checkbox('enable_llm_local')}</Row>
        {(draft.enable_llm_local || textProvider === 'ollama') && <>
          <Row label={copy.ollamaUrl}>{input('ollama_base_url', 'http://localhost:11434')}</Row>
          <Row label={copy.ollamaModel}>{input('ollama_model', 'qwen2.5:7b')}</Row>
        </>}
      </Group>

      <Group title={copy.video} note={copy.videoNote}>
        <Row label={copy.provider}>{providerSelect(videoProviders, videoProvider, (name) => {
          const provider = videoProviders.find((item) => item.name === name)
          patch({ default_model_provider: name, default_model_name: provider?.default_model || catalog[name]?.models?.[0]?.id || String(draft.default_model_name ?? '') })
        })}</Row>
        <Row label={copy.version}><select style={control} value={videoModels.some((model) => model.id === draft.default_model_name) ? String(draft.default_model_name) : ''} onChange={(event) => { if (event.target.value) patch({ default_model_name: event.target.value }) }}>
          <option value="">{copy.customVersion}</option>{videoModels.map((model) => <option key={model.id} value={model.id}>{model.label ?? model.id}</option>)}
        </select></Row>
        <Row label={copy.model}>{input('default_model_name')}</Row>
        {videoProvider && <Row label={copy.key}>{keyInput('api_keys', videoProvider)}</Row>}
        {videoInfo?.base_url && <Row label={copy.endpoint}><output style={{ overflowWrap: 'anywhere' }}>{videoInfo.base_url}</output></Row>}
        <Row label={copy.resolution}>{choice('default_video_resolution', resolutions, String(draft.default_resolution ?? '480p'))}</Row>
        <Row label={copy.aspect}>{choice('default_aspect_ratio', aspects, '16:9')}</Row>
        <Row label={copy.spend}>{checkbox('allow_video_spend')}</Row>
        <Row label={copy.budget}>{numberInput('default_video_budget_yuan', 0, 0, undefined, 0.1)}</Row>
        <Row label={copy.maxSeconds} hint={copy.maxSecondsHint}>{numberInput('max_shot_seconds', 0, 0, 60)}</Row>
        <Row label={copy.lip}><select style={control} value={String(draft.lipsync_mode ?? 'off')} onChange={(event) => { patch({ lipsync_mode: event.target.value }) }}><option value="off">{copy.off}</option><option value="cloud">{copy.cloud}</option></select></Row>
        <Row label={copy.backfill}>{checkbox('dialogue_backfill', true)}</Row>
        {(videoModel?.best_for || catalog[videoProvider]?.note || videoInfo?.description) && <p style={{ color: colors.dim, lineHeight: 1.7 }}>{videoModel?.best_for || catalog[videoProvider]?.note || videoInfo?.description}</p>}
      </Group>

      <Group title={copy.voice} note={copy.voiceNote}>
        <Row label={copy.voiceMode}><select style={control} value={String(draft.voiceover_mode ?? 'auto')} onChange={(event) => { patch({ voiceover_mode: event.target.value }) }}><option value="auto">{copy.auto}</option><option value="off">{copy.off}</option><option value="on">{copy.on}</option></select></Row>
        {providerOptions(ttsProviders, '').map((provider) => <div key={provider.name}>
          <Row label={provider.display_name ?? provider.name} hint={provider.supports_model === false ? (provider.model_note || copy.fixed) : undefined}>
            {provider.supports_model === false ? <small style={{ color: colors.dim }}>{provider.model_note || copy.fixed}</small> : <>
              <input style={control} list={'vf-tts-' + provider.name} value={String((draft.tts_models ?? {})[provider.name] ?? provider.selected_model ?? provider.default_model ?? '')} onChange={(event) => { patch({ tts_models: { ...(draft.tts_models ?? {}), [provider.name]: event.target.value } }) }} />
              <datalist id={'vf-tts-' + provider.name}>{(provider.models ?? []).map((model) => <option key={ttsModelId(model)} value={ttsModelId(model)}>{model.label}</option>)}</datalist>
            </>}
          </Row>
          {provider.name !== 'edge' && <Row label={(provider.display_name ?? provider.name) + ' ' + copy.key}>{keyInput('tts_api_keys', provider.name)}</Row>}
        </div>)}
        <Row label={copy.rate}>{numberInput('voice_rate', 1, 0.5, 2, 0.05)}</Row>
        <Row label={copy.volume}>{numberInput('voice_volume', 1, 0, 2, 0.05)}</Row>
      </Group>

      <Group title={copy.image} note={copy.imageNote}>
        <Row label={copy.provider}>{providerSelect(imageProviders, imageProvider, (name) => { const provider = imageProviders.find((item) => item.name === name); patch({ image_provider: name, image_model: provider?.default_model ?? '' }) })}</Row>
        <Row label={copy.model}>{input('image_model', imageProviders.find((provider) => provider.name === imageProvider)?.default_model ?? '')}</Row>
        {imageProvider && <Row label={copy.key}>{keyInput('llm_api_keys', imageProvider)}</Row>}
      </Group>

      <Group title={copy.paths} note={copy.pathsNote}>
        <Row label={copy.output}>{input('output_dir')}</Row><Row label={copy.cache}>{input('cache_dir')}</Row><Row label={copy.ffmpeg}>{input('ffmpeg_path', 'ffmpeg')}</Row>
      </Group>
      <Group title={copy.public} note={copy.publicNote}>
        <Row label={copy.publicUrl}>{input('public_base_url')}</Row><Row label={copy.publicDir}>{input('public_sync_dir')}</Row><Row label={copy.tunnel}>{input('tunnel_named')}</Row><Row label={copy.hostname}>{input('tunnel_hostname')}</Row>
      </Group>
      <Group title={copy.keys} note={copy.keysNote}>
        <details><summary style={{ cursor: 'pointer', padding: '9px 0' }}>{copy.stored}</summary>
          {(['llm_api_keys', 'api_keys', 'tts_api_keys'] as const).flatMap((group) => Object.keys(server?.[group] ?? {}).map((provider) => <Row key={group + ':' + provider} label={group + ' · ' + provider}>{keyInput(group, provider)}</Row>))}
        </details>
      </Group>
    </fieldset>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', position: 'sticky', bottom: 0, padding: '12px 0', background: '#202025', flexWrap: 'wrap' }}>
        <button type="button" disabled={server === null || saving} style={{ ...button, borderColor: colors.accent, color: colors.accent }} onClick={() => { void save() }}>{saving ? copy.saving : copy.save}</button>
        <button type="button" disabled={saving} style={button} onClick={() => { void loadSettings().then(() => setStatus('')).catch(() => setStatus(copy.loadFailed)) }}>{copy.reload}</button>
        <span role="status" style={{ color: status === copy.saved ? colors.accent : colors.warn, fontSize: 12 }}>{status}</span>
      </div>
  </div>
}
