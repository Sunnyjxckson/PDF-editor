import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import {
  runOcr,
  startOcr,
  exportDocument,
  getExportFormatUrl,
  filenameFromDisposition,
  createPdfFromFiles,
  compressDocument,
  formatBytes,
} from '@/lib/features/convert'
import ConvertPanel from '@/components/features/ConvertPanel'

const mockFetch = vi.fn()
global.fetch = mockFetch

function json(data: unknown, ok = true, status = 200) {
  return { ok, status, json: () => Promise.resolve(data), headers: new Headers() } as unknown as Response
}

const DOC = 'doc-1'

beforeEach(() => {
  mockFetch.mockReset()
})

describe('convert API client', () => {
  it('startOcr posts options and returns the job id', async () => {
    mockFetch.mockResolvedValueOnce(json({ job_id: 'j1' }))
    const id = await startOcr(DOC, { language: 'eng', mode: 'editable', pages: [2] })
    expect(id).toBe('j1')
    const [url, opts] = mockFetch.mock.calls[0]
    expect(url).toBe('http://localhost:8000/api/pdf/doc-1/ocr')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ language: 'eng', mode: 'editable', pages: [2] })
  })

  it('runOcr polls until done and reports progress', async () => {
    mockFetch
      .mockResolvedValueOnce(json({ job_id: 'j1' }))
      .mockResolvedValueOnce(json({ status: 'running', done: 1, total: 2, progress: 0.5 }))
      .mockResolvedValueOnce(json({ status: 'done', done: 2, total: 2, progress: 1, result: { pages_processed: [0, 1], words_added: 40, skipped: [], total: 2 } }))
    const seen: number[] = []
    const res = await runOcr(DOC, {}, (j) => seen.push(j.progress), 0)
    expect(res.words_added).toBe(40)
    expect(seen).toEqual([0.5, 1])
    expect(mockFetch.mock.calls[1][0]).toBe('http://localhost:8000/api/pdf/ocr/jobs/j1')
  })

  it('runOcr rejects with the server error', async () => {
    mockFetch
      .mockResolvedValueOnce(json({ job_id: 'j1' }))
      .mockResolvedValueOnce(json({ status: 'error', error: 'Invalid page(s): [9]', done: 0, total: 0, progress: 0 }))
    await expect(runOcr(DOC, {}, undefined, 0)).rejects.toThrow('Invalid page(s): [9]')
  })

  it('startOcr surfaces the backend detail on 400', async () => {
    mockFetch.mockResolvedValueOnce(json({ detail: 'Tesseract language(s) not installed: deu' }, false, 400))
    await expect(startOcr(DOC, { language: 'deu' })).rejects.toThrow('not installed: deu')
  })

  it('builds export URLs and parses the download filename', async () => {
    expect(getExportFormatUrl(DOC, 'png', { dpi: 300, filename: 'My Doc' })).toBe(
      'http://localhost:8000/api/pdf/doc-1/export/png?dpi=300&filename=My+Doc',
    )
    expect(filenameFromDisposition('attachment; filename="report.docx"', 'x')).toBe('report.docx')
    expect(filenameFromDisposition(null, 'fallback.md')).toBe('fallback.md')

    const blob = new Blob(['hello'])
    mockFetch.mockResolvedValueOnce({
      ok: true,
      blob: () => Promise.resolve(blob),
      headers: new Headers({ 'Content-Disposition': 'attachment; filename="a.txt"' }),
    })
    const out = await exportDocument(DOC, 'txt')
    expect(out.filename).toBe('a.txt')
    expect(out.blob).toBe(blob)
  })

  it('createPdfFromFiles sends every file in order plus page size', async () => {
    mockFetch.mockResolvedValueOnce(json({ id: 'new', filename: 'a.pdf', page_count: 2, metadata: {} }))
    const a = new File(['a'], 'a.png', { type: 'image/png' })
    const b = new File(['b'], 'b.md', { type: 'text/markdown' })
    const doc = await createPdfFromFiles([a, b], { pageSize: 'a4' })
    expect(doc.id).toBe('new')
    const fd = mockFetch.mock.calls[0][1].body as FormData
    expect(fd.getAll('files')).toEqual([a, b])
    expect(fd.get('page_size')).toBe('a4')
    await expect(createPdfFromFiles([])).rejects.toThrow('No files selected')
  })

  it('compressDocument sends preset and dry_run', async () => {
    mockFetch.mockResolvedValueOnce(json({ applied: false, dry_run: true }))
    await compressDocument(DOC, 'smallest', { dryRun: true })
    expect(JSON.parse(mockFetch.mock.calls[0][1].body)).toEqual({ preset: 'smallest', dry_run: true })
  })

  it('formatBytes', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.00 MB')
  })
})

describe('ConvertPanel', () => {
  function routeFetch(overrides: Record<string, unknown> = {}) {
    mockFetch.mockImplementation((url: string) => {
      if (url.endsWith('/ocr/languages')) return Promise.resolve(json({ languages: ['eng', 'deu'] }))
      if (url.endsWith('/ocr/detect'))
        return Promise.resolve(json({ pages: [], scanned_pages: [0, 2], needs_ocr: [0] }))
      if (url.endsWith('/compress'))
        return Promise.resolve(
          json(
            overrides.compress ?? {
              preset: 'balanced', before_bytes: 2_000_000, after_bytes: 500_000, optimized_bytes: 500_000,
              saved_bytes: 1_500_000, saved_percent: 75, applied: true, dry_run: false,
              images_total: 3, images_rewritten: 2, fonts_subset: true, target_dpi: 150, quality: 75,
            },
          ),
        )
      if (url.endsWith('/ocr')) return Promise.resolve(json({ job_id: 'j1' }))
      if (url.includes('/ocr/jobs/'))
        return Promise.resolve(json({ status: 'done', done: 1, total: 1, progress: 1, result: { pages_processed: [0], words_added: 12, skipped: [], total: 1 } }))
      return Promise.resolve(json({}, false, 404))
    })
  }

  it('shows scan detection and installed languages', async () => {
    routeFetch()
    render(<ConvertPanel docId={DOC} currentPage={0} onDocumentChanged={() => {}} />)
    expect(await screen.findByText('2 scanned pages, 1 without a text layer.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('option', { name: 'deu' })).toBeInTheDocument())
  })

  it('runs OCR for the current page and notifies the editor', async () => {
    routeFetch()
    const changed = vi.fn()
    render(<ConvertPanel docId={DOC} currentPage={4} onDocumentChanged={changed} />)
    fireEvent.change(screen.getByLabelText('OCR pages'), { target: { value: 'current' } })
    fireEvent.click(screen.getByRole('button', { name: /Recognize text/ }))
    expect(await screen.findByText(/Recognized 12 words on 1 page\./)).toBeInTheDocument()
    expect(changed).toHaveBeenCalledTimes(1)
    const ocrCall = mockFetch.mock.calls.find(([u]) => String(u).endsWith('/ocr'))!
    expect(JSON.parse(ocrCall[1].body)).toMatchObject({ pages: [4], force: true, language: 'eng' })
  })

  it('compresses and shows before/after sizes', async () => {
    routeFetch()
    const changed = vi.fn()
    render(<ConvertPanel docId={DOC} currentPage={0} onDocumentChanged={changed} />)
    await screen.findByText(/scanned pages/)
    fireEvent.click(screen.getByRole('button', { name: 'Compress' }))
    const box = await screen.findByTestId('compress-result')
    expect(box).toHaveTextContent('Before1.91 MB')
    expect(box).toHaveTextContent('After488.3 KB')
    expect(box).toHaveTextContent('75%')
    expect(changed).toHaveBeenCalledTimes(1)
  })

  it('adds dropped files and lets you reorder them', async () => {
    routeFetch()
    render(<ConvertPanel docId={DOC} currentPage={0} onDocumentChanged={() => {}} />)
    await screen.findByText(/scanned pages/)
    const a = new File(['a'], 'first.png', { type: 'image/png' })
    const b = new File(['b'], 'second.txt', { type: 'text/plain' })
    fireEvent.drop(screen.getByTestId('create-dropzone'), { dataTransfer: { files: [a, b] } })
    const list = screen.getByRole('list', { name: 'Files to combine' })
    expect(list).toHaveTextContent(/first\.png.*second\.txt/)
    fireEvent.click(screen.getByRole('button', { name: 'Move second.txt up' }))
    expect(list).toHaveTextContent(/second\.txt.*first\.png/)
  })
})
