import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import {
  editTextInPlace,
  isUpright,
  overflowChoices,
  rotBoxToCss,
  TextOverflowError,
  type EditablePage,
  type OverflowInfo,
} from '@/lib/features/text_edit'
import TextEditOverlay, { editResultMessage } from '@/components/features/TextEditOverlay'

const mockFetch = vi.fn()
global.fetch = mockFetch

const ok = (data: unknown) => ({ ok: true, json: () => Promise.resolve(data) })
const fail = (status: number, detail: unknown) => ({ ok: false, status, json: () => Promise.resolve({ detail }) })

const style = { font: 'Helvetica', family: 'sans' as const, size: 12, color: '#000000', bold: false, italic: false, flags: 0 }

const INFO: OverflowInfo = {
  code: 'overflow', message: 'The edited text does not fit', needed_height: 160.4,
  available_height: 118.2, requested_size: 11, fit_size: 7.81, fit_scale: 0.71,
}

const PAGE: EditablePage = {
  page: 0, width: 612, height: 792, rotation: 0, fonts: [],
  blocks: [{
    id: 'b0p0', bbox: [72, 90, 300, 130], text: 'Para', paragraph_text: 'Para', style,
    mixed_styles: false, editable: true, align: 'left', line_height: 1.2, angle: 0,
    box: { x: 72, y: 90, w: 228, h: 40, angle: 0 },
    lines: [{ id: 'b0.l0', bbox: [72, 90, 110, 103], text: 'Para', style, spans: [] }],
  }, {
    // 30 degree stamp: its axis-aligned bbox is much bigger than its own box
    id: 'b1p0', bbox: [200, 280, 320, 360], text: 'Stamp', paragraph_text: 'Stamp', style,
    mixed_styles: false, editable: true, align: 'left', line_height: 1.2, angle: 30,
    box: { x: 205, y: 287, w: 120, h: 17, angle: 30 },
    lines: [{ id: 'b1.l0', bbox: [200, 280, 320, 360], text: 'Stamp', style, editable: true, angle: 30,
      box: { x: 205, y: 287, w: 120, h: 17, angle: 30 }, spans: [] }],
  }, {
    // vertical label (text runs up the page)
    id: 'b2p0', bbox: [400, 300, 414, 400], text: 'Side', paragraph_text: 'Side', style,
    mixed_styles: false, editable: true, align: 'left', line_height: 1.2, angle: 270,
    box: { x: 400, y: 400, w: 100, h: 14, angle: 270 },
    lines: [],
  }],
}

beforeEach(() => mockFetch.mockReset())

describe('textedit2 helpers', () => {
  it('detects upright angles and builds rotated CSS boxes', () => {
    expect(isUpright(0)).toBe(true)
    expect(isUpright(360)).toBe(true)
    expect(isUpright(null)).toBe(true)
    expect(isUpright(90)).toBe(false)
    expect(isUpright(30)).toBe(false)
    const css = rotBoxToCss({ x: 10, y: 20, w: 100, h: 12, angle: 90 }, 2)
    expect(css).toMatchObject({ left: 20, top: 40, width: 200, height: 24, transform: 'rotate(90deg)', transformOrigin: '0 0' })
    expect(rotBoxToCss({ x: 10, y: 20, w: 100, h: 12, angle: 0 }, 1, 3)).toMatchObject({
      left: 7, top: 17, width: 106, height: 18, transform: undefined,
    })
    // padding is applied along the rotated axes: at 90deg "outwards" is +x / -y
    const p = rotBoxToCss({ x: 10, y: 20, w: 100, h: 12, angle: 90 }, 1, 3)
    expect(p.left).toBeCloseTo(13)
    expect(p.top).toBeCloseTo(17)
  })

  it('labels the overflow choices with the resulting size', () => {
    const c = overflowChoices(INFO)
    expect(c.shrink).toBe('Shrink to fit (7.8 pt)')
    expect(c.allow).toBe('Allow overlap')
    expect(c.cancel).toBe('Cancel')
    expect(c.summary).toMatch(/160 pt.*118 pt/)
    expect(overflowChoices({ ...INFO, fit_size: null }).shrink).toBeNull()
  })

  it('turns a 422 overflow body into a TextOverflowError carrying the numbers', async () => {
    mockFetch.mockResolvedValueOnce(fail(422, INFO))
    const target = { kind: 'block' as const, id: 'b0p0', bbox: [1, 2, 3, 4] }
    const err = await editTextInPlace('d', { page: 0, target, text: 'x' }).catch((e) => e)
    expect(err).toBeInstanceOf(TextOverflowError)
    expect(err.info.fit_size).toBe(7.81)
    // other 422s stay plain errors
    mockFetch.mockResolvedValueOnce(fail(422, 'Lines run in different directions'))
    const e2 = await editTextInPlace('d', { page: 0, target, text: 'x' }).catch((e) => e)
    expect(e2).not.toBeInstanceOf(TextOverflowError)
    expect(e2.message).toMatch(/different directions/)
  })

  it('reports pushes / overlaps after an edit', () => {
    expect(editResultMessage({ pushed: 2 })).toMatch(/moved the following text down/i)
    expect(editResultMessage({ overlap: true, overflow: true })).toMatch(/overlaps/)
    expect(editResultMessage({ overflow: false })).toBeNull()
  })
})

async function openParagraph() {
  mockFetch.mockResolvedValueOnce(ok(PAGE))
  const onChanged = vi.fn()
  const onMessage = vi.fn()
  render(<TextEditOverlay docId="doc1" currentPage={0} pxPerPt={1} onDocumentChanged={onChanged} onMessage={onMessage} />)
  fireEvent.click(await screen.findByTestId('text-target-b0p0'))
  const ta = (await screen.findByLabelText('Edit text')) as HTMLTextAreaElement
  fireEvent.change(ta, { target: { value: 'Para that is now far too long' } })
  mockFetch.mockResolvedValueOnce(fail(422, INFO))
  await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })
  await screen.findByTestId('text-overflow-dialog')
  return { ta, onChanged, onMessage }
}

const editCalls = () => mockFetch.mock.calls.filter(([u]) => String(u).endsWith('/text-edit/edit'))

describe('TextEditOverlay overflow dialog', () => {
  it('offers shrink (with the size), allow overlap and cancel; shrink resends with overflow=shrink', async () => {
    const { onChanged } = await openParagraph()
    expect(screen.getByText('Shrink to fit (7.8 pt)')).toBeTruthy()
    expect(screen.getByText('Allow overlap')).toBeTruthy()
    expect(screen.getByText('Cancel')).toBeTruthy()
    expect(onChanged).not.toHaveBeenCalled() // nothing was written yet
    mockFetch.mockResolvedValueOnce(ok({ status: 'ok', kind: 'block', font_size: 7.81, overflow: false }))
    mockFetch.mockResolvedValueOnce(ok(PAGE))
    await act(async () => { fireEvent.click(screen.getByText('Shrink to fit (7.8 pt)')) })
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    const body = JSON.parse(editCalls()[1][1].body)
    expect(body).toMatchObject({ text: 'Para that is now far too long', overflow: 'shrink' })
    expect(JSON.parse(editCalls()[0][1].body).overflow).toBeUndefined()
    await waitFor(() => expect(screen.queryByTestId('text-overflow-dialog')).toBeNull())
  })

  it('allow overlap resends with overflow=allow and tells the user it overlaps', async () => {
    const { onChanged, onMessage } = await openParagraph()
    mockFetch.mockResolvedValueOnce(ok({ status: 'ok', kind: 'block', overflow: true, overlap: true }))
    mockFetch.mockResolvedValueOnce(ok(PAGE))
    await act(async () => { fireEvent.click(screen.getByText('Allow overlap')) })
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(JSON.parse(editCalls()[1][1].body).overflow).toBe('allow')
    expect(onMessage).toHaveBeenCalledWith(expect.stringMatching(/overlaps/), 'info')
  })

  it('cancel writes nothing and returns to the editor with the draft', async () => {
    const { ta, onChanged } = await openParagraph()
    fireEvent.click(screen.getByText('Cancel'))
    expect(screen.queryByTestId('text-overflow-dialog')).toBeNull()
    expect((screen.getByLabelText('Edit text') as HTMLTextAreaElement).value).toBe('Para that is now far too long')
    expect(editCalls()).toHaveLength(1)
    expect(onChanged).not.toHaveBeenCalled()
    // blur while the dialog is open does not resend; neither after cancel without changes... until Enter
    mockFetch.mockResolvedValueOnce(fail(422, INFO))
    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })
    await screen.findByTestId('text-overflow-dialog')
    fireEvent.blur(ta)
    expect(editCalls()).toHaveLength(2)
  })
})

describe('TextEditOverlay rotated text', () => {
  it('draws rotated hover targets on the text box, upright ones unchanged', async () => {
    mockFetch.mockResolvedValueOnce(ok(PAGE))
    render(<TextEditOverlay docId="doc1" currentPage={0} pxPerPt={2} onDocumentChanged={() => {}} />)
    const stamp = await screen.findByTestId('text-target-b1p0')
    expect(stamp.style.transform).toBe('rotate(30deg)')
    expect(stamp.style.width).toBe(`${120 * 2 + 2}px`)
    const upright = screen.getByTestId('text-target-b0p0')
    expect(upright.style.transform).toBe('')
    expect(upright.style.left).toBe('143px')
  })

  it('opens a rotated editor for slanted and vertical text, at the text box angle', async () => {
    mockFetch.mockResolvedValueOnce(ok(PAGE))
    render(<TextEditOverlay docId="doc1" currentPage={0} pxPerPt={1} onDocumentChanged={() => {}} />)
    fireEvent.click(await screen.findByTestId('text-target-b1p0'))
    let rot = await screen.findByTestId('text-edit-rotated')
    expect(rot.style.transform).toBe('rotate(30deg)')
    expect(rot.style.transformOrigin).toBe('0 0')
    const ta = screen.getByLabelText('Edit text') as HTMLTextAreaElement
    expect(rot.contains(ta)).toBe(true)
    expect(ta.style.width).toBe(`${120 + 6}px`) // along the text, not the bbox width
    fireEvent.keyDown(ta, { key: 'Escape' })

    fireEvent.click(await screen.findByTestId('text-target-b2p0'))
    rot = await screen.findByTestId('text-edit-rotated')
    expect(rot.style.transform).toBe('rotate(270deg)')
    fireEvent.keyDown(screen.getByLabelText('Edit text'), { key: 'Escape' })

    // upright text keeps the plain (unrotated) editor
    fireEvent.click(await screen.findByTestId('text-target-b0p0'))
    await screen.findByLabelText('Edit text')
    expect(screen.queryByTestId('text-edit-rotated')).toBeNull()
  })
})
