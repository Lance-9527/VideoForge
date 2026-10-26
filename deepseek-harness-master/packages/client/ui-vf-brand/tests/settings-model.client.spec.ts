import { describe, expect, it } from 'vitest'
import { categoryCounts, effectiveBaseUrl, emptySecretEdits, providerOptions, secretGroupOf, settingsPatch, ttsModelId } from '../src/client/settings-model.ts'
import type { VfSettings } from '../src/client/settings-model.ts'

describe('VideoForge settings patches', () => {
  const saved: VfSettings = {
    llm_provider: 'openai', llm_model: 'relay-model', llm_base_url: 'https://relay.invalid/v1',
    default_model_provider: 'seedance_t6', default_model_name: 'seedance-2-0-official',
    api_keys: { seedance_t6: 'existing-relay', kling: 'existing-kling' },
    llm_api_keys: { openai: 'existing-text' },
    tts_api_keys: { agentai: 'existing-speech' },
    tts_models: { agentai: 'speech-2.8', minimax: 'speech-2.8-hd' },
    public_sync_dir: 'D:/webroot', output_dir: 'D:/VideoForge/outputs',
    allow_video_spend: true, default_video_budget_yuan: 60,
  }

  it('does not resubmit unchanged settings or blank replacement keys', () => {
    const secrets = emptySecretEdits()
    secrets.api_keys.seedance_t6 = '  '
    expect(settingsPatch(saved, { ...saved }, secrets)).toEqual({})
  })

  it('preserves other providers while replacing one key', () => {
    const secrets = emptySecretEdits()
    secrets.api_keys.kling = 'new-kling'
    const patch = settingsPatch(saved, { output_dir: 'D:/new-output' }, secrets)
    expect(patch.api_keys).toEqual({ kling: 'new-kling' })
    expect({ ...saved.api_keys, ...patch.api_keys }).toEqual({ seedance_t6: 'existing-relay', kling: 'new-kling' })
    expect(patch.output_dir).toBe('D:/new-output')
    expect(patch).not.toHaveProperty('llm_api_keys')
    expect(saved.api_keys?.kling).toBe('existing-kling')
  })

  it('keeps independent TTS models, false and a zero budget', () => {
    const patch = settingsPatch(saved, { tts_models: { ...saved.tts_models, minimax: 'speech-2.8-turbo' }, allow_video_spend: false, default_video_budget_yuan: 0 }, emptySecretEdits())
    expect(patch.tts_models).toEqual({ minimax: 'speech-2.8-turbo' })
    expect({ ...saved.tts_models, ...patch.tts_models }).toEqual({ agentai: 'speech-2.8', minimax: 'speech-2.8-turbo' })
    expect(patch.allow_video_spend).toBe(false)
    expect(patch.default_video_budget_yuan).toBe(0)
  })

  it('resets endpoint choices explicitly while blank paths remain untouched', () => {
    expect(settingsPatch(saved, { llm_base_url: '', output_dir: '' }, emptySecretEdits())).toEqual({ llm_base_url: '' })
  })

  it('updates legacy resolution and Ollama aliases together', () => {
    expect(settingsPatch(saved, { default_video_resolution: '720p', ollama_base_url: 'http://localhost:11435' }, emptySecretEdits())).toEqual({ default_video_resolution: '720p', default_resolution: '720p', ollama_base_url: 'http://localhost:11435', ollama_url: 'http://localhost:11435' })
  })

  it('keeps saved relays and custom providers absent from a catalog', () => {
    expect(providerOptions([{ name: 'kling' }], 'seedance_t6').map((provider) => provider.name)).toEqual(['kling', 'seedance_t6'])
  })

  it('reads both legacy and relay TTS model identifiers', () => {
    expect(ttsModelId({ id: 'speech-2.8-hd' })).toBe('speech-2.8-hd')
    expect(ttsModelId({ value: 'speech-2.8' })).toBe('speech-2.8')
  })
})

describe('VideoForge model registry helpers', () => {
  it('maps registry key fields to the three secret buckets', () => {
    expect(secretGroupOf({ key_field: 'llm_api_keys' })).toBe('llm_api_keys')
    expect(secretGroupOf({ key_bucket: 'api_keys' })).toBe('api_keys')
    expect(secretGroupOf({ key_field: 'other', key_bucket: 'tts_api_keys' })).toBe('tts_api_keys')
    expect(secretGroupOf({ key_field: 'something_else' })).toBeNull()
    expect(secretGroupOf({})).toBeNull()
  })

  it('prefers a saved base URL override over the provider default', () => {
    expect(effectiveBaseUrl({ base_url: 'https://api.openai.com', base_url_override: 'https://relay.invalid/v1', base_url_effective: 'https://relay.invalid/v1' })).toBe('https://relay.invalid/v1')
    expect(effectiveBaseUrl({ base_url: 'https://api.openai.com', base_url_override: '', base_url_effective: 'https://api.openai.com' })).toBe('https://api.openai.com')
    expect(effectiveBaseUrl({ base_url: 'https://relay.invalid/v1' })).toBe('https://relay.invalid/v1')
    expect(effectiveBaseUrl({})).toBe('')
  })

  it('uses backend counts first and falls back to provider flags', () => {
    const category = { id: 'text', providers: [{ id: 'a', configured: true }, { id: 'b', configured: false }] }
    expect(categoryCounts(category, { text: { configured: 1, total: 14 } })).toEqual({ configured: 1, total: 14 })
    expect(categoryCounts(category)).toEqual({ configured: 1, total: 2 })
    expect(categoryCounts({ id: 'image' }, { image: { total: 3 } })).toEqual({ configured: 0, total: 3 })
  })
})
