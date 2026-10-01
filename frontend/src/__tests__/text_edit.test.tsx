import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import {
  buildEditPayload,
  cssFontFamily,
  deleteText,
  editTextInPlace,
  getEditableText,
  initialDraft,
  moveText,
  ptRectToPx,
  pxDeltaToPt,
  type Draft,
  type EditablePage,
} from '@/lib/features/text_edit'
import TextEditOverlay from '@/components/features/TextEditOverlay'

const mockFetch = vi.fn()
global.fetch = mockFetch

const ok = (data: unknown) => ({ ok: true, json: () => Promise.resolve(data) })
const fail = (status: number, detail: string) => ({ ok: false, status, json: () => Promise.resolve({ detail }) })

const style = { font: 'Times-Roman', family: 'serif' as const, size: 12, color: '#112233', bold: false, italic: false, flags: 4 }

const PAGE: EditablePage = {
  page: 0, width: 612, height: 792, rotation: 0, fonts: [],
  blocks: [{
    id: 'b0p0', bbox: [72, 90, 300, 130], text: 'Hello\nworld', paragraph_text: 'Hello world',
    style, mixed_styles: false, editable: true, align: 'left', line_height: 1.2, angle: 0,
    lines: [
      { id: 'b0.l0', bbox: [72, 90, 110, 103], text: 'Hello', style,
        spans: [{ id: 'b0.l0.s0', bbox: [72, 90, 110, 103], text: 'Hello', ...style }] },
      { id: 'b0.l1', bbox: [72, 104, 112, 117], text: 'world', style,
        spans: [{ id: 'b0.l1.s0', bbox: [72, 104, 112, 117], text: 'world', ...style }] },
    ],
  }, {
    id: 'b1p0', bbox: [300, 300, 320, 400], text: 'Tilted', paragraph_text: 'Tilted', style,
    mixed_styles: false, editable: false, reason: 'Text at a non-right angle cannot be edited in place',
    align: 'left', line_height: 1.2, angle: null, lines: [],
  }],
}

beforeEach(() => mockFetch.mockReset())

describe('text_edit pure helpers', () => {
  it('converts PDF points to rendered pixels and back', () => {
    expect(ptRectToPx([72, 90, 300, 130], 2)).toEqual({ left: 144, top: 180, width: 456, height: 80 })
    expect(pxDeltaToPt(300, -150, 3)).toEqual({ dx: 100, dy: -50 })
    expect(pxDeltaToPt(10, 10, 0)).toEqual({ dx: 0, dy: 0 })
  })

  it('maps font families to CSS stacks', () => {
    expect(cssFontFamily('original', 'serif')).toContain('Times')
    expect(cssFontFamily('mono', 'serif')).toContain('Courier')
    expect(cssFontFamily('sans', 'serif')).toContain('Helvetica')
  })

  it('uses paragraph_text for blocks and sends only changed fields', () => {
    const orig = initialDraft('block', PAGE.blocks[0])
    expect(orig.text).toBe('Hello world')
    const target = { kind: 'block' as const, id: 'b0p0', bbox: PAGE.blocks[0].bbox }
    expect(buildEditPayload(0, target, orig, { ...orig })).toBeNull()
    const d: Draft = { ...orig, text: 'Hello there', bold: true, color: '#FF0000', align: 'center' }
    expect(buildEditPayload(0, target, orig, d)).toEqual({
      page: 0, target, text: 'Hello there', style: { bold: true, color: '#FF0000' }, align: 'center',
    })
    // align is ignored for lines; size change only
    const lineOrig = initialDraft('line', PAGE.blocks[0].lines[0])
    const lt = { kind: 'line' as const, id: 'b0.l0', bbox: [72, 90, 110, 103] }
    expect(buildEditPayload(2, lt, lineOrig, { ...lineOrig, size: 14, align: 'right' }))
      .toEqual({ page: 2, target: lt, style: { size: 14 } })
  })
})

describe('text_edit API client', () => {
  it('fetches the editable page', async () => {
    mockFetch.mockResolvedValueOnce(ok(PAGE))
    const p = await getEditableText('doc1', 3)
    expect(mockFetch).toHaveBeenCalledWith('http://localhost:8000/api/pdf/doc1/text-edit/page/3')
    expect(p.blocks[0].id).toBe('b0p0')
  })

  it('posts edit / move / delete as JSON and surfaces backend detail', async () => {
    mockFetch.mockResolvedValueOnce(ok({ status: 'ok', kind: 'line' }))
    const target = { kind: 'line' as const, id: 'b0.l0', bbox: [1, 2, 3, 4] }
    await editTextInPlace('d', { page: 0, target, text: 'x' })
    let [url, init] = mockFetch.mock.calls[0]
    expect(url).toBe('http://localhost:8000/api/pdf/d/text-edit/edit')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ page: 0, target, text: 'x' })

    mockFetch.mockResolvedValueOnce(ok({ status: 'ok', moved: 1 }))
    await moveText('d', { page: 0, target, dx: 5, dy: -2 })
    ;[url, init] = mockFetch.mock.calls[1]
    expect(url).toBe('http://localhost:8000/api/pdf/d/text-edit/move')
    expect(JSON.parse(init.body)).toMatchObject({ dx: 5, dy: -2 })

    mockFetch.mockResolvedValueOnce(ok({ status: 'ok' }))
    await deleteText('d', { page: 0, target })
    expect(mockFetch.mock.calls[2][0]).toBe('http://localhost:8000/api/pdf/d/text-edit/delete')

    mockFetch.mockResolvedValueOnce(fail(409, 'Text on this page changed; reload and try again'))
    await expect(editTextInPlace('d', { page: 0, target, text: 'y' })).rejects.toThrow(/changed; reload/)
  })
})

describe('TextEditOverlay', () => {
  it('draws scaled hover boxes and edits a paragraph in place', async () => {
    mockFetch.mockResolvedValueOnce(ok(PAGE))
    const onChanged = vi.fn()
    render(<div style={{ position: 'relative' }}>
      <TextEditOverlay docId="doc1" currentPage={0} pxPerPt={2} onDocumentChanged={onChanged} />
    </div>)

    const box = await screen.findByTestId('text-target-b0p0')
    // bbox [72,90,300,130] at 2 px/pt, with a 1px outline margin
    expect(box.style.left).toBe('143px')
    expect(box.style.top).toBe('179px')
    expect(box.style.width).toBe('458px')

    fireEvent.click(box)
    const ta = (await screen.findByLabelText('Edit text')) as HTMLTextAreaElement
    expect(ta.value).toBe('Hello world')
    expect(ta.style.fontSize).toBe('24px') // 12pt * 2px/pt
    expect(ta.style.color).toBe('rgb(17, 34, 51)')

    fireEvent.change(ta, { target: { value: 'Hello brave new world' } })
    fireEvent.click(screen.getByLabelText('Bold'))
    mockFetch.mockResolvedValueOnce(ok({ status: 'ok', kind: 'block', overflow: false }))
    mockFetch.mockResolvedValueOnce(ok(PAGE)) // reload after change
    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    const call = mockFetch.mock.calls.find(([u]) => String(u).endsWith('/text-edit/edit'))!
    expect(JSON.parse(call[1].body)).toEqual({
      page: 0, target: { kind: 'block', id: 'b0p0', bbox: [72, 90, 300, 130] },
      text: 'Hello brave new world', style: { bold: true },
    })
    await waitFor(() => expect(screen.queryByLabelText('Edit text')).toBeNull())
  })

  it('Shift+Enter inserts a newline in paragraph mode, Esc cancels without a request', async () => {
    mockFetch.mockResolvedValueOnce(ok(PAGE))
    const onChanged = vi.fn()
    render(<TextEditOverlay docId="doc1" currentPage={0} pxPerPt={1} onDocumentChanged={onChanged} />)
    fireEvent.click(await screen.findByTestId('text-target-b0p0'))
    const ta = await screen.findByLabelText('Edit text')
    fireEvent.keyDown(ta, { key: 'Enter', shiftKey: true })
    fireEvent.keyDown(ta, { key: 'Escape' })
    expect(screen.queryByLabelText('Edit text')).toBeNull()
    expect(mockFetch).toHaveBeenCalledTimes(1) // only the initial load
    expect(onChanged).not.toHaveBeenCalled()
  })

  it('switches to line granularity and deletes a line', async () => {
    mockFetch.mockResolvedValueOnce(ok(PAGE))
    const onChanged = vi.fn()
    render(<TextEditOverlay docId="doc1" currentPage={0} pxPerPt={1} onDocumentChanged={onChanged} />)
    await screen.findByTestId('text-target-b0p0')
    fireEvent.click(screen.getByText('Line'))
    fireEvent.click(await screen.findByTestId('text-target-b0.l1'))
    mockFetch.mockResolvedValueOnce(ok({ status: 'ok' }))
    mockFetch.mockResolvedValueOnce(ok(PAGE))
    await act(async () => { fireEvent.click(screen.getByLabelText('Delete text')) })
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    const call = mockFetch.mock.calls.find(([u]) => String(u).endsWith('/text-edit/delete'))!
    expect(JSON.parse(call[1].body)).toEqual({ page: 0, target: { kind: 'line', id: 'b0.l1', bbox: [72, 104, 112, 117] } })
  })

  it('does not open an editor on non-editable text and reports why', async () => {
    mockFetch.mockResolvedValueOnce(ok(PAGE))
    const onMessage = vi.fn()
    render(<TextEditOverlay docId="doc1" currentPage={0} pxPerPt={1} onDocumentChanged={() => {}} onMessage={onMessage} />)
    fireEvent.click(await screen.findByTestId('text-target-b1p0'))
    expect(screen.queryByLabelText('Edit text')).toBeNull()
    expect(onMessage).toHaveBeenCalledWith(expect.stringMatching(/non-right angle/), 'info')
  })

  it('renders nothing when inactive', () => {
    const { container } = render(<TextEditOverlay docId="doc1" currentPage={0} pxPerPt={1} active={false} onDocumentChanged={() => {}} />)
    expect(container.innerHTML).toBe('')
    expect(mockFetch).not.toHaveBeenCalled()
  })
})
