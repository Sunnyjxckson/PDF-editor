import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import {
  clientToPdfPoint,
  rectFromPoints,
  rectToPercentStyle,
  rectsIntersect,
  matchesToAreas,
  wordsToAreas,
  permissionsFrom,
  markRedactions,
  applyRedactions,
  searchRedactions,
  deleteRedactMark,
  protectDocument,
  unlockDocument,
  useRedactStore,
  type RedactMatch,
  type RedactWord,
} from '@/lib/features/redact'
import RedactConfirmDialog from '@/components/features/RedactConfirmDialog'
import RedactOverlay from '@/components/features/RedactOverlay'

// jsdom lacks PointerEvent; without this clientX/button are dropped.
if (typeof window.PointerEvent === 'undefined') {
  class PointerEventPolyfill extends MouseEvent {
    pointerId: number
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init)
      this.pointerId = init.pointerId ?? 1
    }
  }
  // @ts-expect-error assigning polyfill
  window.PointerEvent = PointerEventPolyfill
}

const mockFetch = vi.fn()
global.fetch = mockFetch

const ok = (data: unknown) => ({ ok: true, json: () => Promise.resolve(data), blob: () => Promise.resolve(new Blob(['%PDF'])) })
const bad = (status: number, detail: string) => ({ ok: false, status, json: () => Promise.resolve({ detail }) })

const initialRedactState = useRedactStore.getState()

beforeEach(() => {
  mockFetch.mockReset()
  useRedactStore.setState(initialRedactState, true)
})

describe('redact geometry', () => {
  it('maps client coords to PDF points regardless of rendered size', () => {
    // 612x792pt page rendered at 306x396 CSS px (zoom 0.5), offset 100,50
    const box = { left: 100, top: 50, width: 306, height: 396 }
    expect(clientToPdfPoint(100, 50, box, 612, 792)).toEqual([0, 0])
    expect(clientToPdfPoint(253, 248, box, 612, 792)).toEqual([306, 396])
    expect(clientToPdfPoint(406, 446, box, 612, 792)).toEqual([612, 792])
    // clamps outside the page
    expect(clientToPdfPoint(0, 1000, box, 612, 792)).toEqual([0, 792])
  })

  it('normalises rects and converts to percent styles', () => {
    expect(rectFromPoints([50, 80], [10, 20])).toEqual([10, 20, 50, 80])
    expect(rectToPercentStyle([61.2, 79.2, 306, 396], 612, 792)).toEqual({
      left: '10%', top: '10%', width: '40%', height: '40%',
    })
  })

  it('intersects rects', () => {
    expect(rectsIntersect([0, 0, 10, 10], [5, 5, 15, 15])).toBe(true)
    expect(rectsIntersect([0, 0, 10, 10], [10, 0, 20, 10])).toBe(false)
  })

  it('flattens only selected matches into areas', () => {
    const ms: RedactMatch[] = [
      { id: 'a', page: 0, text: 'x', kind: 'ssn', rects: [[1, 2, 3, 4], [5, 6, 7, 8]], context: '' },
      { id: 'b', page: 2, text: 'y', kind: 'email', rects: [[9, 9, 10, 10]], context: '' },
    ]
    expect(matchesToAreas(ms, new Set(['a']))).toEqual([
      { page: 0, rect: [1, 2, 3, 4] },
      { page: 0, rect: [5, 6, 7, 8] },
    ])
  })

  it('unions words per line', () => {
    const ws: RedactWord[] = [
      { text: 'a', rect: [10, 10, 20, 20], block: 0, line: 0, word: 0 },
      { text: 'b', rect: [25, 9, 40, 21], block: 0, line: 0, word: 1 },
      { text: 'c', rect: [10, 30, 20, 40], block: 0, line: 1, word: 0 },
    ]
    expect(wordsToAreas(ws, 3)).toEqual([
      { page: 3, rect: [10, 9, 40, 21] },
      { page: 3, rect: [10, 30, 20, 40] },
    ])
  })

  it('maps permission flags', () => {
    expect(permissionsFrom({ print: true, copy: false, modify: false, annotate: true, fill_forms: false, assemble: false }))
      .toEqual(['print', 'annotate'])
  })
})

describe('redact API client', () => {
  it('marks areas with style and label', async () => {
    mockFetch.mockResolvedValueOnce(ok({ status: 'ok', count: 1, marked: [] }))
    await markRedactions('d1', [{ page: 0, rect: [1, 2, 3, 4] }], { fill_color: '#ff0000', overlay_text: 'REDACTED' }, 'lbl')
    const [url, init] = mockFetch.mock.calls[0]
    expect(url).toMatch(/\/api\/pdf\/d1\/redact\/mark$/)
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({
      areas: [{ page: 0, rect: [1, 2, 3, 4] }], fill_color: '#ff0000', overlay_text: 'REDACTED', label: 'lbl',
    })
  })

  it('applies, searches, deletes, unlocks', async () => {
    mockFetch.mockResolvedValue(ok({ status: 'ok', matches: [], count: 0, truncated: false }))
    await applyRedactions('d1', { images: 'remove' })
    expect(mockFetch.mock.calls[0][0]).toMatch(/\/d1\/redact\/apply$/)
    expect(JSON.parse(mockFetch.mock.calls[0][1].body)).toEqual({ images: 'remove' })
    await searchRedactions('d1', { presets: ['ssn'] })
    expect(mockFetch.mock.calls[1][0]).toMatch(/\/d1\/redact\/search$/)
    await deleteRedactMark('d1', 2, 77)
    expect(mockFetch.mock.calls[2][0]).toMatch(/\/d1\/redact\/marks\/2\/77$/)
    expect(mockFetch.mock.calls[2][1].method).toBe('DELETE')
    await unlockDocument('d1', 'pw')
    expect(JSON.parse(mockFetch.mock.calls[3][1].body)).toEqual({ password: 'pw' })
  })

  it('surfaces server error detail', async () => {
    mockFetch.mockResolvedValueOnce(bad(403, 'Incorrect password'))
    await expect(unlockDocument('d1', 'x')).rejects.toThrow('Incorrect password')
  })

  it('protect returns a Blob for download, JSON when applied to the document', async () => {
    mockFetch.mockResolvedValueOnce(ok({}))
    const b = await protectDocument('d1', { owner_password: 'o', permissions: ['print'] })
    expect(b).toBeInstanceOf(Blob)
    mockFetch.mockResolvedValueOnce(ok({ status: 'ok' }))
    const j = await protectDocument('d1', { owner_password: 'o', permissions: [], apply_to_document: true })
    expect(j).toEqual({ status: 'ok' })
  })
})

describe('RedactConfirmDialog', () => {
  it('requires explicit acknowledgement before applying', () => {
    const onConfirm = vi.fn()
    render(<RedactConfirmDialog open markCount={3} pageCount={2} onConfirm={onConfirm} onCancel={() => {}} />)
    expect(screen.getByText(/Permanently redact 3 areas/)).toBeInTheDocument()
    const apply = screen.getByRole('button', { name: 'Apply redactions' })
    expect(apply).toBeDisabled()
    fireEvent.click(apply)
    expect(onConfirm).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('checkbox'))
    expect(apply).not.toBeDisabled()
    fireEvent.click(apply)
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('renders nothing when closed', () => {
    const { container } = render(<RedactConfirmDialog open={false} markCount={1} pageCount={1} onConfirm={() => {}} onCancel={() => {}} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('RedactOverlay', () => {
  it('converts a drag into a PDF-point area and marks it', async () => {
    mockFetch.mockImplementation((url: string, init?: RequestInit) => {
      if (url.endsWith('/redact/marks') && (!init || init.method === 'GET')) return Promise.resolve(ok({ marks: [], count: 0 }))
      return Promise.resolve(ok({ status: 'ok', count: 1, marked: [] }))
    })
    const changed = vi.fn()
    render(<RedactOverlay docId="d1" currentPage={1} pageWidth={612} pageHeight={792} onDocumentChanged={changed} />)
    const overlay = screen.getByTestId('redact-overlay')
    // Rendered at half size: 306x396 px at (0,0).
    overlay.getBoundingClientRect = () => ({ left: 0, top: 0, width: 306, height: 396, right: 306, bottom: 396, x: 0, y: 0, toJSON: () => ({}) })
    act(() => { useRedactStore.setState({ active: true, tool: 'area' }) })
    expect(overlay.style.pointerEvents).toBe('auto')

    await act(async () => {
      fireEvent.pointerDown(overlay, { clientX: 10, clientY: 20, button: 0 })
      fireEvent.pointerMove(overlay, { clientX: 60, clientY: 45 })
      fireEvent.pointerUp(overlay, { clientX: 60, clientY: 45 })
    })
    const markCall = mockFetch.mock.calls.find(([u]) => String(u).endsWith('/redact/mark'))
    expect(markCall).toBeTruthy()
    expect(JSON.parse(markCall![1].body).areas).toEqual([{ page: 1, rect: [20, 40, 120, 90] }])
    expect(changed).toHaveBeenCalled()
  })

  it('draws marks for the current page only, as % boxes', () => {
    mockFetch.mockResolvedValue(ok({ marks: [], count: 0 }))
    useRedactStore.setState({
      marks: [
        { page: 0, xref: 5, rect: [61.2, 79.2, 306, 396], label: 'a', overlay_text: '', fill_color: '#000000' },
        { page: 1, xref: 6, rect: [0, 0, 10, 10], label: 'b', overlay_text: '', fill_color: '#000000' },
      ],
      refreshMarks: async () => {},
    })
    render(<RedactOverlay docId="d1" currentPage={0} pageWidth={612} pageHeight={792} onDocumentChanged={() => {}} />)
    const boxes = screen.getAllByTitle(/^a$|^b$/)
    expect(boxes).toHaveLength(1)
    expect(boxes[0].style.left).toBe('10%')
    expect(boxes[0].style.width).toBe('40%')
    // inactive overlay must not swallow clicks meant for the page
    expect(screen.getByTestId('redact-overlay').style.pointerEvents).toBe('none')
  })
})
