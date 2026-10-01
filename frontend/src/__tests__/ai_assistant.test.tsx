import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import {
  parseSSEChunk, splitCitations, streamAIChat, applyReviewedRedactions, loadProfile, saveProfile,
  clearProfile, useAIStore, type AIEvent, type RedactionReviewItem,
} from '@/lib/features/ai'
import { useEditorStore } from '@/lib/store'
import ChatPanel from '@/components/ChatPanel'
import AIPanel from '@/components/AIPanel'
import AIRedactionReview from '@/components/features/AIRedactionReview'
import AICitationOverlay from '@/components/features/AICitationOverlay'
import AIMessageContent from '@/components/features/AIMessageContent'

const DOC = '11111111-2222-3333-4444-555555555555'
const mockFetch = vi.fn()
global.fetch = mockFetch as unknown as typeof fetch

function sseBody(events: Array<AIEvent | '[DONE]'>, split = 13) {
  const text = events.map((e) => `data: ${typeof e === 'string' ? e : JSON.stringify(e)}\n\n`).join('')
  const enc = new TextEncoder()
  const chunks: Uint8Array[] = []
  for (let i = 0; i < text.length; i += split) chunks.push(enc.encode(text.slice(i, i + split)))
  return {
    getReader() {
      let i = 0
      return { read: async () => (i < chunks.length ? { done: false, value: chunks[i++] } : { done: true, value: undefined }) }
    },
  }
}

const sseResponse = (events: Array<AIEvent | '[DONE]'>) => ({ ok: true, status: 200, body: sseBody(events), json: async () => ({}) })
const ok = (data: unknown) => ({ ok: true, status: 200, json: async () => data })

const CONFIG = {
  sdk_installed: true, api_key_set: true, key_source: 'env', ai_available: true, model: 'claude-sonnet-5-5',
  models: [{ id: 'claude-sonnet-5-5', label: 'Claude Sonnet 5.5' }, { id: 'claude-opus-5-5', label: 'Claude Opus 5.5' }],
}

const DONE = {
  type: 'done', run_id: 'r1', response: 'Revenue is now 9000 [p. 2 "total revenue"].', changed: true,
  changes: [{ tool: 'edit_paragraph', summary: 'edited a paragraph on page 2', undoable: true }], undo_steps: 1,
  citations: [{ page: 2, quote: 'total revenue', rects: [[90, 100, 200, 112]], valid: true, marker: '[p. 2 "total revenue"]' }],
  page_count_changed: false, new_page_count: 4, stopped: false,
} as const

beforeEach(() => {
  mockFetch.mockReset()
  useEditorStore.getState().reset()
  useAIStore.setState({ queued: null, highlight: null, references: [] })
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
  try { window.localStorage.clear() } catch { /* ignore */ }
})

describe('SSE + citation helpers', () => {
  it('parses complete events and keeps the partial remainder', () => {
    const [evs, rest] = parseSSEChunk('data: {"type":"text","delta":"Hi"}\n\ndata: [DONE]\n\ndata: {"type":"te')
    expect(evs).toEqual([{ type: 'text', delta: 'Hi' }, '[DONE]'])
    expect(rest).toBe('data: {"type":"te')
  })

  it('splits [p. N] and [p. N "quote"] citations', () => {
    const segs = splitCitations('A [p. 3] b [p. 12 "exact words"] c [page 4]')
    expect(segs.filter((s) => s.kind === 'citation')).toEqual([
      { kind: 'citation', page: 3, quote: null, marker: '[p. 3]' },
      { kind: 'citation', page: 12, quote: 'exact words', marker: '[p. 12 "exact words"]' },
    ])
    expect(segs[segs.length - 1]).toEqual({ kind: 'text', text: ' c [page 4]' })
  })

  it('renders model text without injecting HTML', () => {
    const { container } = render(<AIMessageContent text={'**bold** <img src=x onerror=alert(1)>\n- item [p. 1]'} />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('strong')?.textContent).toBe('bold')
    expect(screen.getByTestId('ai-citation').textContent).toBe('p. 1')
  })
})

describe('streamAIChat', () => {
  it('delivers events in order across chunk boundaries and posts the request', async () => {
    mockFetch.mockResolvedValueOnce(sseResponse([
      { type: 'start', run_id: 'r1', model: 'm' }, { type: 'text', delta: 'Hel' }, { type: 'text', delta: 'lo' },
      DONE as unknown as AIEvent, '[DONE]',
    ]))
    const seen: AIEvent[] = []
    const h = streamAIChat({ doc_id: DOC, message: 'hi', current_page: 0 }, (e) => seen.push(e))
    await h.done
    expect(seen.map((e) => e.type)).toEqual(['start', 'text', 'text', 'done'])
    const [url, init] = mockFetch.mock.calls[0]
    expect(url).toMatch(/\/api\/ai\/chat\/stream$/)
    expect(JSON.parse(init.body)).toMatchObject({ doc_id: DOC, message: 'hi', current_page: 0, allow_break_signature: false })
  })

  it('reports an error event on HTTP failure and when the stream ends without done', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 400, json: async () => ({ detail: 'bad' }) })
    const a: AIEvent[] = []
    await streamAIChat({ doc_id: DOC, message: 'x', current_page: 0 }, (e) => a.push(e)).done
    expect(a).toEqual([{ type: 'error', code: 'http', message: 'bad' }])
    mockFetch.mockResolvedValueOnce(sseResponse([{ type: 'text', delta: 'x' }]))
    const b: AIEvent[] = []
    await streamAIChat({ doc_id: DOC, message: 'x', current_page: 0 }, (e) => b.push(e)).done
    expect(b[b.length - 1]).toMatchObject({ type: 'error', code: 'incomplete' })
  })
})

describe('ChatPanel', () => {
  function open() {
    useEditorStore.setState({ docId: DOC, chatOpen: true, totalPages: 4, currentPage: 0 })
  }

  it('streams text and tool progress, citations jump + highlight, undo calls the API', async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url.endsWith('/api/ai/config')) return ok(CONFIG)
      if (url.endsWith('/api/ai/chat/stream')) return sseResponse([
        { type: 'start', run_id: 'r1', model: 'claude-sonnet-5-5' },
        { type: 'text', delta: 'Working on it. ' },
        { type: 'tool_start', id: 't1', name: 'edit_paragraph', input: {}, label: 'edit paragraph on p. 2' },
        { type: 'tool_result', id: 't1', name: 'edit_paragraph', ok: true, summary: 'edited a paragraph on page 2', changed: true },
        DONE as unknown as AIEvent, '[DONE]',
      ])
      if (url.includes('/info')) return ok({ id: DOC, page_count: 4, metadata: {}, pages: [] })
      if (url.endsWith('/undo')) return ok({ status: 'ok' })
      return ok({})
    })
    open()
    render(<ChatPanel />)
    await waitFor(() => expect(screen.getByLabelText('Model')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'make revenue 9000' } })
    await act(async () => { fireEvent.click(screen.getByLabelText('Send')) })
    await waitFor(() => expect(screen.getByTestId('ai-changes')).toBeInTheDocument())
    expect(screen.getByTestId('ai-tools').textContent).toContain('edited a paragraph on page 2')
    expect(screen.getByText(/Revenue is now 9000/)).toBeInTheDocument()

    await act(async () => { fireEvent.click(screen.getByTestId('ai-citation')) })
    expect(useEditorStore.getState().currentPage).toBe(1)
    expect(useAIStore.getState().highlight).toEqual({ page: 2, rects: [[90, 100, 200, 112]], quote: 'total revenue' })

    await act(async () => { fireEvent.click(screen.getByText('Undo')) })
    expect(mockFetch.mock.calls.some(([u, i]) => String(u).endsWith(`/api/pdf/${DOC}/undo`) && i?.method === 'POST')).toBe(true)
    await waitFor(() => expect(screen.getByText('Undone')).toBeInTheDocument())
  })

  it('shows the key setup state when no key is configured and saves the key', async () => {
    let configured = false
    mockFetch.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/api/ai/config')) return ok({ ...CONFIG, api_key_set: configured, ai_available: configured })
      if (url.endsWith('/api/ai/key')) {
        expect(JSON.parse(String(init?.body))).toEqual({ api_key: 'sk-ant-abc', persist: false })
        configured = true
        return ok({ status: 'ok', api_key_set: true, ai_available: true })
      }
      return ok({})
    })
    open()
    render(<ChatPanel />)
    await waitFor(() => expect(screen.getByTestId('ai-setup')).toBeInTheDocument())
    expect(screen.getByLabelText('Message')).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value: 'sk-ant-abc' } })
    await act(async () => { fireEvent.click(screen.getByText('Save key')) })
    await waitFor(() => expect(screen.queryByTestId('ai-setup')).toBeNull())
    expect(screen.getByLabelText('Message')).not.toBeDisabled()
  })

  it('Stop aborts the stream and tells the server', async () => {
    let release: () => void = () => {}
    mockFetch.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/api/ai/config')) return ok(CONFIG)
      if (url.endsWith('/api/ai/chat/stop')) return ok({ status: 'ok' })
      if (url.endsWith('/api/ai/chat/stream')) {
        const enc = new TextEncoder()
        let sent = false
        return {
          ok: true, status: 200, json: async () => ({}),
          body: { getReader: () => ({ read: () => {
            if (!sent) { sent = true; return Promise.resolve({ done: false, value: enc.encode('data: {"type":"start","run_id":"r9","model":"m"}\n\n') }) }
            return new Promise((_, reject) => {
              release = () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
              init?.signal?.addEventListener('abort', () => release())
            })
          } }) },
        }
      }
      return ok({})
    })
    open()
    render(<ChatPanel />)
    await waitFor(() => expect(screen.getByLabelText('Model')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'long task' } })
    await act(async () => { fireEvent.click(screen.getByLabelText('Send')) })
    await waitFor(() => expect(screen.getByLabelText('Stop')).toBeInTheDocument())
    await act(async () => { fireEvent.click(screen.getByLabelText('Stop')) })
    await waitFor(() => expect(screen.getByLabelText('Send')).toBeInTheDocument())
    expect(mockFetch.mock.calls.some(([u, i]) => String(u).endsWith('/api/ai/chat/stop') && JSON.parse(String(i?.body)).run_id === 'r9')).toBe(true)
  })

  it('sends a queued AIPanel action with the profile only for profile actions', async () => {
    saveProfile({ full_name: 'Ada Lovelace' })
    const bodies: Record<string, unknown>[] = []
    mockFetch.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/api/ai/config')) return ok(CONFIG)
      if (url.endsWith('/api/ai/chat/stream')) {
        bodies.push(JSON.parse(String(init?.body)))
        return sseResponse([{ ...DONE, changed: false, changes: [], undo_steps: 0 } as unknown as AIEvent])
      }
      return ok({})
    })
    useEditorStore.setState({ docId: DOC, totalPages: 4, aiPanelOpen: true })
    render(<><AIPanel /><ChatPanel /></>)
    expect(screen.getByText('Explain selection').closest('button')).toBeDisabled()
    await act(async () => { fireEvent.click(screen.getByText('Fill form from my profile')) })
    await waitFor(() => expect(bodies.length).toBe(1))
    expect(bodies[0]).toMatchObject({ doc_id: DOC, action: 'autofill_profile', profile: { full_name: 'Ada Lovelace' } })
    expect(useEditorStore.getState().chatOpen).toBe(true)
  })
})

describe('smart redaction review + overlay + profile', () => {
  const items: RedactionReviewItem[] = [
    { id: '0:1:2', page: 1, page_index: 0, text: 'John Smith', rects: [[10, 10, 50, 20]], category: 'name', reason: 'person' },
    { id: '0:3:4', page: 1, page_index: 0, text: '123-45-6789', rects: [[60, 10, 120, 20]], category: 'ssn', reason: '' },
  ]

  it('applies only the selected items with 0-based page indices', async () => {
    mockFetch.mockResolvedValueOnce(ok({ status: 'ok', applied: 1 }))
    const onApplied = vi.fn()
    render(<AIRedactionReview docId={DOC} items={items} onApplied={onApplied} />)
    fireEvent.click(screen.getByLabelText('Redact 123-45-6789'))
    await act(async () => { fireEvent.click(screen.getByText('Redact 1 selected')) })
    const [url, init] = mockFetch.mock.calls[0]
    expect(url).toMatch(/\/api\/ai\/redactions\/apply$/)
    expect(JSON.parse(init.body)).toEqual({ doc_id: DOC, areas: [{ page: 0, rect: [10, 10, 50, 20] }], allow_break_signature: false })
    expect(onApplied).toHaveBeenCalledWith(1)
  })

  it('applyReviewedRedactions surfaces server errors', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 409, json: async () => ({ detail: 'signed' }) })
    await expect(applyReviewedRedactions(DOC, items)).rejects.toThrow('signed')
  })

  it('citation overlay draws the highlight only on its page', () => {
    useAIStore.setState({ highlight: { page: 2, rects: [[61.2, 79.2, 306, 158.4]], quote: 'q' } })
    const { rerender } = render(<AICitationOverlay currentPage={0} pageWidth={612} pageHeight={792} />)
    expect(screen.queryByTestId('ai-citation-overlay')).toBeNull()
    rerender(<AICitationOverlay currentPage={1} pageWidth={612} pageHeight={792} />)
    const box = screen.getByTestId('ai-citation-overlay').firstElementChild as HTMLElement
    expect(box.style.left).toBe('10%')
    expect(box.style.top).toBe('10%')
    expect(box.style.width).toBe('40%')
  })

  it('profile is stored only in localStorage', () => {
    saveProfile({ full_name: 'Ada', email: '  ', phone: '555' })
    expect(loadProfile()).toEqual({ full_name: 'Ada', phone: '555' })
    clearProfile()
    expect(loadProfile()).toEqual({})
    expect(mockFetch).not.toHaveBeenCalled()
  })
})
