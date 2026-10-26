// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { VfPanel } from '../src/client/Panel.tsx'
import { VfRail } from '../src/client/Rail.tsx'
import { setVfView, vfViewSnapshot } from '../src/client/view.ts'

vi.mock('../src/client/AssetLibrary.tsx', () => ({ AssetLibrary: () => null }))

const sessions = {
  list: {
    getSnapshot: () => ({
      ids: ['session-newest', 'session-project', 'session-duplicate'],
      byId: {
        'session-newest': { title: 'New empty conversation' },
        'session-project': { title: 'Repeated title' },
        'session-duplicate': { title: 'Repeated title' },
      },
    }),
    subscribe: () => () => {},
  },
}

beforeEach(() => {
  setVfView({ section: 'chat', projectId: null, projectTitle: null, returnSessionId: null })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function mockProjects(projects: readonly { id: string, title: string, settings?: { dsh_session_id: string } }[]) {
  const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ data: projects }) }))
  vi.stubGlobal('fetch', fetch)
  return fetch
}

describe('project conversation navigation', () => {
  it('keeps the explicit project binding when switching assets and post, despite a newer empty session', async () => {
    mockProjects([{ id: 'project-a', title: 'Project A', settings: { dsh_session_id: 'session-project' } }])
    render(<VfRail sessions={sessions} actions={{ openSession: vi.fn(), startSession: vi.fn() }} />)
    fireEvent.click(await screen.findByText('Project A'))
    expect(vfViewSnapshot().returnSessionId).toBe('session-project')
    fireEvent.click(screen.getByText('后期'))
    expect(vfViewSnapshot()).toMatchObject({ section: 'post', projectId: 'project-a', returnSessionId: 'session-project' })
    fireEvent.click(screen.getByText('资产'))
    expect(vfViewSnapshot()).toMatchObject({ section: 'assets', projectId: 'project-a', returnSessionId: 'session-project' })
  })

  it('does not infer an unbound project from duplicate session titles or recency', async () => {
    mockProjects([{ id: 'unbound', title: 'Repeated title' }])
    render(<VfRail sessions={sessions} actions={{ openSession: vi.fn(), startSession: vi.fn() }} />)
    const matches = await waitFor(() => {
      const rows = screen.getAllByText('Repeated title')
      expect(rows).toHaveLength(3)
      return rows
    })
    fireEvent.click(matches[matches.length - 1]!)
    expect(vfViewSnapshot()).toMatchObject({ projectId: 'unbound', returnSessionId: null })
  })

  it('does not choose a session claimed by two projects', async () => {
    mockProjects([
      { id: 'project-a', title: 'Project A', settings: { dsh_session_id: 'session-project' } },
      { id: 'project-b', title: 'Project B', settings: { dsh_session_id: 'session-project' } },
    ])
    render(<VfRail sessions={sessions} actions={{ openSession: vi.fn(), startSession: vi.fn() }} />)
    fireEvent.click(await screen.findByText('Project A'))
    expect(vfViewSnapshot().returnSessionId).toBeNull()
  })

  it('rejects a manual binding to another project session before writing the sidecar', async () => {
    const fetch = mockProjects([
      { id: 'project-a', title: 'Project A' },
      { id: 'project-b', title: 'Project B', settings: { dsh_session_id: 'session-project' } },
    ])
    render(<VfRail sessions={sessions} actions={{ openSession: vi.fn(), startSession: vi.fn(), currentSessionId: () => 'session-project' }} />)
    fireEvent.contextMenu(await screen.findByText('Project A'), { clientX: 30, clientY: 40 })
    fireEvent.click(screen.getByText('绑定到当前会话'))
    expect(screen.getByText(/当前会话已绑定项目/u)).toBeTruthy()
    expect(fetch.mock.calls).toHaveLength(1)
  })

  it('places the complete return control outside the caption and reports an unresolved binding', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ data: {} }) })))
    setVfView({ section: 'assets', projectId: 'project-a', projectTitle: 'Project A', returnSessionId: null })
    const onBack = vi.fn(() => 'Explicit binding required')
    render(<VfPanel onBack={onBack} />)
    const button = screen.getByRole('button', { name: '← AI 对话（回到对应会话）' })
    expect(button.closest('div[style*="position: fixed"]')?.getAttribute('style')).toContain('top: var(--dsh-frame-chrome-top, 0px)')
    expect(Reflect.get(button.style, 'WebkitAppRegion')).toBe('no-drag')
    fireEvent.click(button)
    expect(onBack).toHaveBeenCalledOnce()
    expect(screen.getByRole('alert').textContent).toBe('Explicit binding required')
    expect(vfViewSnapshot().section).toBe('assets')
    await act(async () => {})
  })
})
