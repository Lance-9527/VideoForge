/** Settings form data and partial updates for the VideoForge sidecar. */
export interface VfSettings {
  [key: string]: unknown
  llm_provider?: string | null
  llm_model?: string | null
  llm_base_url?: string | null
  llm_effort?: string | null
  enable_llm_local?: boolean | null
  ollama_url?: string | null
  ollama_base_url?: string | null
  ollama_model?: string | null
  default_model_provider?: string | null
  default_model_name?: string | null
  default_aspect_ratio?: string | null
  default_resolution?: string | null
  default_video_resolution?: string | null
  allow_video_spend?: boolean | null
  default_video_budget_yuan?: number | null
  max_shot_seconds?: number | null
  api_keys?: Record<string, string> | null
  llm_api_keys?: Record<string, string> | null
  tts_api_keys?: Record<string, string> | null
  tts_models?: Record<string, string> | null
  voiceover_mode?: string | null
  voice_rate?: number | null
  voice_volume?: number | null
  image_provider?: string | null
  image_model?: string | null
  lipsync_mode?: string | null
  dialogue_backfill?: boolean | null
  output_dir?: string | null
  cache_dir?: string | null
  ffmpeg_path?: string | null
  public_base_url?: string | null
  public_sync_dir?: string | null
  tunnel_named?: string | null
  tunnel_hostname?: string | null
}

/** Provider metadata comes from the installed backend, including relay adapters. */
export interface ModelProvider {
  name: string
  display_name?: string
  description?: string
  base_url?: string
  default_model?: string
  available_models?: string[]
  supported_resolutions?: string[]
  supported_aspect_ratios?: string[]
  min_duration?: number
  max_duration?: number
  supports_model?: boolean
  model_note?: string
  selected_model?: string
  models?: { id?: string; value?: string; label?: string; note?: string }[]
  usable?: boolean
  note?: string
}

/** Backend model catalog for the selected video provider. */
export interface VideoCatalog {
  display?: string
  note?: string
  models?: { id: string; label?: string; caps?: Record<string, number[]>; best_for?: string }[]
}

export type SecretGroup = 'llm_api_keys' | 'api_keys' | 'tts_api_keys'
export type SecretEdits = Record<SecretGroup, Record<string, string>>

/** Start empty: stored keys never become form values. */
export function emptySecretEdits(): SecretEdits {
  return { llm_api_keys: {}, api_keys: {}, tts_api_keys: {} }
}

/** Preserve a saved/custom provider even when it is absent from today's catalog. */
export function providerOptions(providers: readonly ModelProvider[], current: string): ModelProvider[] {
  return current && !providers.some((provider) => provider.name === current)
    ? [...providers, { name: current, display_name: current }]
    : [...providers]
}

/** Send changed entries only; the sidecar merges provider maps with current storage. */
export function settingsPatch(server: VfSettings, draft: VfSettings, secrets: SecretEdits): VfSettings {
  const patch: VfSettings = {}
  const resettable = new Set(['llm_base_url', 'llm_effort', 'llm_model', 'default_model_provider', 'default_model_name', 'image_provider', 'image_model'])
  for (const [key, value] of Object.entries(draft)) {
    if (value === undefined || value === null || (value === '' && !resettable.has(key))) continue
    if (key === 'tts_models') {
      const changes = Object.fromEntries(Object.entries(draft.tts_models ?? {}).filter(([provider, model]) => model.trim() !== '' && model !== server.tts_models?.[provider]))
      if (Object.keys(changes).length > 0) patch.tts_models = changes
      continue
    }
    if (JSON.stringify(value) !== JSON.stringify(server[key])) patch[key] = value
  }
  if (patch.default_video_resolution !== undefined) patch.default_resolution = patch.default_video_resolution
  if (patch.ollama_base_url !== undefined) patch.ollama_url = patch.ollama_base_url
  for (const group of ['llm_api_keys', 'api_keys', 'tts_api_keys'] as const) {
    const edits = Object.fromEntries(Object.entries(secrets[group]).map(([key, value]) => [key, value.trim()]).filter(([, value]) => value !== ''))
    if (Object.keys(edits).length > 0) patch[group] = edits
  }
  return patch
}

/** Model ids differ between legacy TTS catalogs (id) and the relay catalog (value). */
export function ttsModelId(model: { id?: string; value?: string }): string {
  return model.id ?? model.value ?? ''
}

/** Base URL of the local VideoForge sidecar: settings, model registry and provider catalogs. */
export const SIDECAR = 'http://127.0.0.1:8765'

/** One model entry in a registry provider (`models[]`). */
export interface RegistryModel {
  id: string
  label?: string
}

/**
 * One platform in the VideoForge model registry.
 *
 * `status` is the backend capability verdict: `implemented` (adapter exists, not
 * verified with a real key), `reserved` (catalog/protocol notes only, no adapter)
 * and `verified` (probed with a real key). `configured` only means a key is
 * present and must not be rendered as "usable".
 */
export interface RegistryProvider {
  id: string
  label?: string
  protocol?: string
  base_url?: string
  base_url_override?: string
  base_url_effective?: string
  default_model?: string
  models?: RegistryModel[]
  key_field?: string
  key_bucket?: string
  doc?: string
  status?: string
  env?: string[]
  notes?: string
  supports?: Record<string, boolean>
  configured?: boolean
  env_present?: string[]
  selected_model?: string
  active?: boolean
}

/** One of the four model categories: text / image / video / audio. */
export interface RegistryCategory {
  id: string
  label?: string
  hint?: string
  providers?: RegistryProvider[]
}

/** Configured and total provider counters for one category. */
export interface RegistryCounts {
  total?: number
  configured?: number
}

/** Payload of `GET /api/models/registry`. */
export interface ModelRegistry {
  version?: number
  counts?: Record<string, RegistryCounts>
  categories?: RegistryCategory[]
}

/** Payload of `POST /api/models/test`. */
export interface ModelTestResult {
  ok?: boolean
  status?: string
  status_code?: number
  latency_ms?: number
  url?: string
  model?: string
  message?: string
  evidence?: string
}

/** Payload of `POST /api/models/select`. */
export interface ModelSelectResult {
  category?: string
  provider?: string
  model?: string
  selected?: unknown
}

const secretGroupNames = ['llm_api_keys', 'api_keys', 'tts_api_keys'] as const

/** Key bucket a registry provider writes to; null when the platform needs no key. */
export function secretGroupOf(provider: Pick<RegistryProvider, 'key_field' | 'key_bucket'>): SecretGroup | null {
  const field = provider.key_bucket ?? provider.key_field
  return field !== undefined && (secretGroupNames as readonly string[]).includes(field) ? field as SecretGroup : null
}

/** Effective API base URL: a saved override wins, otherwise the provider default. */
export function effectiveBaseUrl(provider: Pick<RegistryProvider, 'base_url' | 'base_url_override' | 'base_url_effective'>): string {
  return provider.base_url_effective ?? provider.base_url_override ?? provider.base_url ?? ''
}

/** Configured / total counters, falling back to per-provider flags when counts are absent. */
export function categoryCounts(category: RegistryCategory, counts?: Record<string, RegistryCounts>): { configured: number; total: number } {
  const providers = category.providers ?? []
  const entry = counts?.[category.id]
  return {
    configured: entry?.configured ?? providers.filter((provider) => provider.configured === true).length,
    total: entry?.total ?? providers.length,
  }
}
