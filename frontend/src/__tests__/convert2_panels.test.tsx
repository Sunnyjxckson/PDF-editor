import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import {
  joinOcrLanguages,
  installOcrLanguage,
  getExportFormatUrl,
  createPdfFromFiles,
} from '@/lib/features/convert'
import {
  summarizeDetected,
  toPdfDate,
  listSelection,
  useFormsStore,
  type FormField,
  type DetectedField,
} from '@/lib/features/forms'
import ConvertPanel from '@/components/features/ConvertPanel'
import FormsPanel from '@/components/features/FormsPanel'

const mockFetch = vi.fn()
global.fetch = mockFetch as unknown as typeof fetch

function json(data: unknown, ok = true, status = 200) {
  return { ok, status, json: () => Promise.resolve(data), headers: new Headers(), blob: () => Promise.resolve(new Blob(['x'])) } as unknown as Response
}

beforeEach(() => {
  mockFetch.mockReset()
  useFormsStore.setState({
    docId: null, fields: [], pages: [], loading: false, error: null,
    prepareMode: false, createType: 'text', selectedId: null, version: 0,
  })
})

describe('convert2 client helpers', () => {
  it('joins OCR languages into tesseract multi-language form', () => {
    expect(joinOcrLanguages(['eng', 'spa'])).toBe('eng+spa')
    expect(joinOcrLanguages(['eng', '', 'eng', null])).toBe('eng')
  })

  it('installOcrLanguage posts the code', async () => {
    mockFetch.mockResolvedValueOnce(json({ installed: 'spa', already_installed: false, languages: ['eng', 'spa'] }))
    const r = await installOcrLanguage('spa')
    expect(r.languages).toContain('spa')
    const [url, init] = mockFetch.mock.calls[0]
    expect(url).toBe('http://localhost:8000/api/pdf/ocr/languages/install')
    expect(JSON.parse(init.body)).toEqual({ code: 'spa' })
  })

  it('builds the reflow HTML export URL and only adds layout for html', () => {
    expect(getExportFormatUrl('d', 'html', { layout: 'reflow' })).toBe('http://localhost:8000/api/pdf/d/export/html?layout=reflow')
    expect(getExportFormatUrl('d', 'txt', { layout: 'reflow' })).toBe('http://localhost:8000/api/pdf/d/export/txt')
  })

  it('sends docx_engine=word only when asked', async () => {
    mockFetch.mockResolvedValue(json({ id: 'n', filename: 'a.pdf', page_count: 1, metadata: {} }))
    await createPdfFromFiles([new File(['a'], 'a.docx')], { docxEngine: 'word' })
    await createPdfFromFiles([new File(['a'], 'a.docx')])
    const fd1 = mockFetch.mock.calls[0][1].body as FormData
    const fd2 = mockFetch.mock.calls[1][1].body as FormData
    expect(fd1.get('docx_engine')).toBe('word')
    expect(fd2.get('docx_engine')).toBeNull()
  })
})

describe('ConvertPanel language install, multi-language OCR, reflow export, Word engine', () => {
  function route(opts: { word?: boolean } = {}) {
    let installed = ['eng']
    mockFetch.mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url)
      if (u.endsWith('/ocr/languages/install')) {
        installed = ['eng', 'spa']
        return Promise.resolve(json({ installed: 'spa', already_installed: false, languages: installed }))
      }
      if (u.endsWith('/ocr/languages'))
        return Promise.resolve(json({
          languages: installed,
          installable: installed.includes('spa') ? [{ code: 'fra', name: 'French' }] : [{ code: 'fra', name: 'French' }, { code: 'spa', name: 'Spanish' }],
          names: { eng: 'English', spa: 'Spanish' },
        }))
      if (u.endsWith('/convert/capabilities')) return Promise.resolve(json({ word: !!opts.word, ocr: true, languages: installed }))
      if (u.endsWith('/ocr/detect')) return Promise.resolve(json({ pages: [], scanned_pages: [0], needs_ocr: [0] }))
      if (u.endsWith('/ocr')) return Promise.resolve(json({ job_id: 'j1' }))
      if (u.includes('/ocr/jobs/'))
        return Promise.resolve(json({ status: 'done', done: 1, total: 1, progress: 1, result: { pages_processed: [0], words_added: 3, skipped: [], total: 1 } }))
      if (u.includes('/export/')) return Promise.resolve(json({}))
      if (u.endsWith('/create')) return Promise.resolve(json({ id: 'n', filename: 'w.pdf', page_count: 2, metadata: {} }))
      void init
      return Promise.resolve(json({}, false, 404))
    })
  }

  it('installs a language only on click, then OCRs with eng+spa', async () => {
    route()
    globalThis.URL.createObjectURL = vi.fn(() => 'blob:x')
    render(<ConvertPanel docId="d1" currentPage={0} onDocumentChanged={() => {}} />)
    const pick = await screen.findByLabelText('Language to install')
    await waitFor(() => expect(screen.getByRole('option', { name: 'Spanish (spa)' })).toBeInTheDocument())
    expect(mockFetch.mock.calls.some(([u]) => String(u).endsWith('/install'))).toBe(false)

    fireEvent.change(pick, { target: { value: 'spa' } })
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    expect(await screen.findByText('Installed Spanish (spa).')).toBeInTheDocument()
    const inst = mockFetch.mock.calls.filter(([u]) => String(u).endsWith('/install'))
    expect(inst).toHaveLength(1)
    expect(JSON.parse(inst[0][1].body)).toEqual({ code: 'spa' })
    await waitFor(() => expect(screen.getByLabelText('Second OCR language')).toHaveValue('spa'))

    fireEvent.click(screen.getByRole('button', { name: /Recognize text/ }))
    expect(await screen.findByText(/Recognized 3 words/)).toBeInTheDocument()
    const ocr = mockFetch.mock.calls.find(([u]) => String(u).endsWith('/ocr'))!
    expect(JSON.parse(ocr[1].body).language).toBe('eng+spa')
  })

  it('exports reflowable HTML from the Web page button', async () => {
    route()
    globalThis.URL.createObjectURL = vi.fn(() => 'blob:x')
    globalThis.URL.revokeObjectURL = vi.fn()
    render(<ConvertPanel docId="d1" currentPage={0} onDocumentChanged={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Export Web page' }))
    await waitFor(() => expect(mockFetch.mock.calls.some(([u]) => String(u).includes('/export/html?') && String(u).includes('layout=reflow'))).toBe(true))
  })

  it('offers the Microsoft Word path only when the server has Word and a .docx is queued', async () => {
    route({ word: false })
    const { unmount } = render(<ConvertPanel docId="d1" currentPage={0} onDocumentChanged={() => {}} />)
    await screen.findByText(/scanned page/)
    fireEvent.drop(screen.getByTestId('create-dropzone'), { dataTransfer: { files: [new File(['a'], 'w.docx')] } })
    expect(screen.queryByText(/High fidelity/)).not.toBeInTheDocument()
    unmount()

    route({ word: true })
    render(<ConvertPanel docId="d1" currentPage={0} onDocumentChanged={() => {}} />)
    await screen.findByText(/scanned page/)
    expect(screen.queryByText(/High fidelity/)).not.toBeInTheDocument()
    fireEvent.drop(screen.getByTestId('create-dropzone'), { dataTransfer: { files: [new File(['a'], 'w.docx')] } })
    fireEvent.click(await screen.findByLabelText(/High fidelity \(uses Microsoft Word\)/))
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(mockFetch.mock.calls.some(([u]) => String(u).endsWith('/create'))).toBe(true))
    const create = mockFetch.mock.calls.find(([u]) => String(u).endsWith('/create'))!
    expect((create[1].body as FormData).get('docx_engine')).toBe('word')
  })
})

function field(p: Partial<FormField>): FormField {
  return {
    id: 1, page: 0, name: 'f', type: 'text', rect: [10, 10, 110, 30], pdf_rect: [10, 10, 110, 30],
    required: false, readonly: false, tooltip: '', font_size: 0, multiline: false, max_len: 0,
    options: [], option_labels: [], export_value: null, value: '', ...p,
  }
}

describe('convert2 forms helpers + multi-select list box', () => {
  it('summarizes detection with radio groups, dates and scanned source', () => {
    const c = (p: Partial<DetectedField>): DetectedField => ({ page: 0, type: 'text', name: 'n', label: '', source: 'line', rect: [0, 0, 1, 1], ...p })
    expect(summarizeDetected([])).toBe('No new fields found')
    expect(summarizeDetected([
      c({ name: 'Name' }), c({ name: 'DOB', format: 'date' }), c({ type: 'checkbox', name: 'ok' }),
      c({ type: 'radio', name: 'Gender', export_value: 'M', source: 'scan-box' }), c({ type: 'radio', name: 'Gender', export_value: 'F', source: 'scan-box' }),
    ])).toBe('Created 5 fields (2 text (1 date), 1 checkbox, 1 radio group) from the scanned page')
  })

  it('normalizes ISO dates and reads list selections', () => {
    expect(toPdfDate('1990-7-4')).toBe('07/04/1990')
    expect(toPdfDate('07/04/1990')).toBe('07/04/1990')
    expect(listSelection(field({ type: 'list', value: ['a', 'b'] }))).toEqual(['a', 'b'])
    expect(listSelection(field({ type: 'list', value: 'a' }))).toEqual(['a'])
    expect(listSelection(field({ type: 'list', value: '' }))).toEqual([])
  })

  it('renders a multi-select list box and commits every selected option', async () => {
    const fields = [
      field({ id: 9, name: 'toppings', type: 'list', multi_select: true, options: ['Cheese', 'Ham', 'Olives'], option_labels: ['Cheese', 'Ham', 'Olives'], value: ['Ham'] }),
      field({ id: 10, name: 'dob', type: 'text', format: 'date', value: '' }),
    ]
    mockFetch.mockImplementation((url: string, init?: RequestInit) => {
      if (!init) return Promise.resolve(json({ fields, count: fields.length, is_form: true, pages: [] }))
      return Promise.resolve(json({ status: 'ok', filled: ['toppings'], errors: {} }))
    })
    render(<FormsPanel docId="doc1" currentPage={0} onDocumentChanged={() => {}} />)
    const sel = (await screen.findByLabelText('toppings')) as HTMLSelectElement
    expect(sel.multiple).toBe(true)
    expect(Array.from(sel.selectedOptions).map((o) => o.value)).toEqual(['Ham'])
    for (const o of Array.from(sel.options)) o.selected = o.value !== 'Ham'
    fireEvent.change(sel)
    await waitFor(() => {
      const fill = mockFetch.mock.calls.find(([u]) => String(u).endsWith('/fill'))
      expect(fill && JSON.parse(fill[1].body)).toEqual({ values: { toppings: ['Cheese', 'Olives'] } })
    })

    const dob = screen.getByLabelText('dob')
    expect(dob).toHaveAttribute('placeholder', 'mm/dd/yyyy')
    fireEvent.change(dob, { target: { value: '2001-02-03' } })
    fireEvent.blur(dob)
    await waitFor(() =>
      expect(mockFetch.mock.calls.filter(([u]) => String(u).endsWith('/fill')).map(([, i]) => JSON.parse(i.body))).toContainEqual({
        values: { dob: '02/03/2001' },
      }),
    )
  })
})
