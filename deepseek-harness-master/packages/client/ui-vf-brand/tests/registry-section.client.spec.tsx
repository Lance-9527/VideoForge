// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { VfRegistrySection } from '../src/client/RegistrySection.tsx'
import { VfModelsSection } from '../src/client/SettingsSection.tsx'

const registryPayload = {
  version: 1,
  counts: {
    text: { total: 3, configured: 1 },
    image: { total: 2, configured: 0 },
    video: { total: 2, configured: 0 },
    audio: { total: 1, configured: 0 },
  },
  categories: [
    {
      id: 'text', label: '文本模型', hint: '写剧本、分镜…',
      providers: [
        {
          id: 'anthropic', label: 'Anthropic Claude', protocol: 'anthropic',
          base_url: 'https://api.anthropic.com', base_url_override: '', base_url_effective: 'https://api.anthropic.com',
          default_model: 'claude-sonnet-4-5', models: [{ id: 'claude-sonnet-4-5', label: 'Claude Sonnet 4.5' }],
          key_field: 'llm_api_keys', key_bucket: 'llm_api_keys', doc: 'https://docs.anthropic.com',
          status: 'implemented', env: ['ANTHROPIC_API_KEY'], notes: '必须用 x-api-key 头',
          configured: true, env_present: [], selected_model: '', active: false,
        },
        {
          id: 'openai', label: 'OpenAI', protocol: 'openai',
          base_url: 'https://api.openai.com', base_url_override: '', base_url_effective: 'https://api.openai.com',
          default_model: 'gpt-4o-mini', models: [{ id: 'gpt-4o-mini', label: 'GPT-4o mini' }],
          key_field: 'llm_api_keys', key_bucket: 'llm_api_keys',
          status: 'implemented', env: ['OPENAI_API_KEY'], configured: false, env_present: [], selected_model: '', active: false,
        },
        {
          id: 'future', label: 'Future Relay', protocol: 'openai',
          base_url: 'https://future.invalid', default_model: 'future-1', models: [{ id: 'future-1', label: 'Future 1' }],
          key_field: 'llm_api_keys', key_bucket: 'llm_api_keys',
          status: 'reserved', configured: false, env_present: [], selected_model: '', active: false,
        },
      ],
    },
    { id: 'image', label: '图像模型', hint: '角色定妆照、场景图', providers: [] },
    { id: 'video', label: '视频模型', providers: [] },
    { id: 'audio', label: '音频模型', providers: [] },
  ],
}

interface JsonResponse {
  readonly ok: boolean
  readonly json: () => Promise<unknown>
}

function bare(data: unknown): JsonResponse {
  return { ok: true, json: async () => data }
}

function envelope(data: unknown): JsonResponse {
  return { ok: true, json: async () => ({ code: '000000', data }) }
}

type Handler = (init?: RequestInit) => JsonResponse | Promise<JsonResponse>

function mockFetch(routes: Record<string, Handler>) {
  const fetchMock = vi.fn(async (input: unknown, init?: RequestInit): Promise<JsonResponse> => {
    const path = String(input).replace('http://127.0.0.1:8765', '')
    const handler = routes[path]
    if (handler === undefined) throw new Error('unexpected fetch: ' + String(input))
    return handler(init)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function registrySection(): ReturnType<typeof render> {
  return render(<VfRegistrySection keyInput={() => <input type="password" aria-label="API Key" />} onSaveKeys={() => {}} saving={false} settingsReady />)
}

/** A registry reply as the backend would return it right after `openai` was selected. */
function registryAfterSelect(): typeof registryPayload {
  const clone = structuredClone(registryPayload)
  const text = clone.categories[0]
  if (text === undefined) return clone
  for (const provider of text.providers) {
    const selected = provider.id === 'openai'
    provider.active = selected
    provider.selected_model = selected ? 'gpt-4o-mini' : ''
  }
  return clone
}

function cardOf(label: string): HTMLElement {
  const card = screen.getByText(label).closest('article')
  if (card === null) throw new Error('provider card not found: ' + label)
  return card
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('VideoForge model registry section', () => {
  it('renders four categories with counts, badges and the measured relay hint', async () => {
    mockFetch({ '/api/models/registry': () => bare(registryPayload) })
    registrySection()
    expect(await screen.findByRole('tab', { name: /文本模型/ })).toBeTruthy()
    expect(screen.getAllByRole('tab')).toHaveLength(4)
    expect(screen.getByText(/已配置 1 \/ 共 3/)).toBeTruthy()
    expect(screen.getAllByText('已实现未验证').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('预留 · 未实现调用')).toBeTruthy()
    expect(screen.getAllByText('未配置 Key').length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText(/实测：api\.openai\.com 在本机直连超时/).length).toBeGreaterThan(0)
    expect((screen.getByDisplayValue('https://api.openai.com') as HTMLInputElement).value).toBe('https://api.openai.com')
    fireEvent.click(screen.getByRole('tab', { name: /图像模型/ }))
    expect(await screen.findByText('这一类暂时没有平台。')).toBeTruthy()
  })

  it('keeps reserved platforms read-only for "set current"', async () => {
    mockFetch({ '/api/models/registry': () => bare(registryPayload) })
    registrySection()
    expect(await screen.findByText('Future Relay')).toBeTruthy()
    const reserved = within(cardOf('Future Relay')).getByRole('button', { name: '设为当前' })
    expect(reserved.hasAttribute('disabled')).toBe(true)
    expect(within(cardOf('Future Relay')).getByRole('button', { name: '测试连接' }).hasAttribute('disabled')).toBe(true)
  })

  it('shows the no_key test branch in gray with the backend reason', async () => {
    const fetchMock = mockFetch({
      '/api/models/registry': () => bare(registryPayload),
      '/api/models/test': () => envelope({ ok: false, status: 'no_key', message: '还没填这个平台的 API Key（设置里填完再点测试）' }),
    })
    registrySection()
    expect(await screen.findByText('OpenAI')).toBeTruthy()
    fireEvent.click(within(cardOf('OpenAI')).getByRole('button', { name: '测试连接' }))
    expect(await screen.findByText('没有 Key')).toBeTruthy()
    expect(screen.getByText(/还没填这个平台的 API Key/)).toBeTruthy()
    const call = fetchMock.mock.calls.find((entry) => String(entry[0]).endsWith('/api/models/test'))
    expect(call).toBeTruthy()
    const body = JSON.parse(String((call?.[1] as RequestInit | undefined)?.body)) as Record<string, unknown>
    expect(body).toMatchObject({ category: 'text', provider: 'openai', model: 'gpt-4o-mini' })
  })

  it('sends the edited base URL to /api/models/select and flips the card to current', async () => {
    let selected = false
    const fetchMock = mockFetch({
      '/api/models/registry': () => bare(selected ? registryAfterSelect() : registryPayload),
      '/api/models/select': () => {
        selected = true
        return envelope({ category: 'text', provider: 'openai', model: 'gpt-4o-mini' })
      },
    })
    registrySection()
    expect(await screen.findByText('OpenAI')).toBeTruthy()
    const card = cardOf('OpenAI')
    fireEvent.change(within(card).getByPlaceholderText(/留空=用官方地址/), { target: { value: 'https://relay.invalid/v1' } })
    fireEvent.click(within(card).getByRole('button', { name: '设为当前' }))
    expect(await within(card).findByText('已设为当前')).toBeTruthy()
    expect(await screen.findByText('当前使用')).toBeTruthy()
    await waitFor(() => {
      expect(fetchMock.mock.calls.some((entry) => String(entry[0]).endsWith('/api/models/select'))).toBe(true)
    })
    const call = fetchMock.mock.calls.find((entry) => String(entry[0]).endsWith('/api/models/select'))
    const body = JSON.parse(String((call?.[1] as RequestInit | undefined)?.body)) as Record<string, unknown>
    expect(body).toMatchObject({ category: 'text', provider: 'openai', model: 'gpt-4o-mini', base_url: 'https://relay.invalid/v1' })
  })

  it('degrades to one hint line when the registry API is missing, leaving legacy settings intact', async () => {
    mockFetch({
      '/api/settings': () => envelope({ llm_provider: 'anthropic', llm_model: 'claude-sonnet-4-5', tts_models: {} }),
      '/api/llm/providers': () => envelope({ providers: [] }),
      '/api/providers': () => envelope({ providers: [] }),
      '/api/image/providers': () => envelope({ providers: [] }),
      '/api/tts/models': () => envelope({ providers: [] }),
      '/api/model-catalog?kind=video': () => envelope({ catalog: {} }),
      '/api/models/registry': () => { throw new Error('404') },
    })
    render(<VfModelsSection />)
    expect(await screen.findByText(/模型目录接口不可用/)).toBeTruthy()
    expect(screen.getByText('本地路径')).toBeTruthy()
    expect(screen.getByText('输出目录')).toBeTruthy()
    expect(screen.getByText('保存设置')).toBeTruthy()
  })
})
