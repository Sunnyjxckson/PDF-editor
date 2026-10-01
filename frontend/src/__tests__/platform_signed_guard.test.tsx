import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import {
  API_BASE,
  ALLOW_BREAK_SIGNATURE_HEADER,
  apiFetch,
  clearSignatureConsent,
  hasSignatureConsent,
  installSignedDocFetchGuard,
  replaceText,
  setSignedDocConfirmHandler,
} from '@/lib/api'
import SignedDocGuard from '@/components/SignedDocGuard'

const DOC = '0b6f2a52-1c2d-4e5f-8a9b-0123456789ab'
const EDIT_URL = `${API_BASE}/api/pdf/${DOC}/organize/rotate`

function conflict() {
  return new Response(
    JSON.stringify({
      code: 'signed_document',
      detail: 'This PDF is digitally signed by Alice Signer. Editing it will invalidate the signature.',
      doc_id: DOC,
      signers: ['Alice Signer'],
    }),
    { status: 409, headers: { 'content-type': 'application/json' } },
  )
}
const ok = (data: unknown = { status: 'ok' }) =>
  new Response(JSON.stringify(data), { status: 200, headers: { 'content-type': 'application/json' } })

function headerOf(init: RequestInit | undefined): string | null {
  return new Headers(init?.headers).get(ALLOW_BREAK_SIGNATURE_HEADER)
}

let mockFetch: ReturnType<typeof vi.fn>
const realFetch = globalThis.fetch

beforeEach(() => {
  clearSignatureConsent()
  setSignedDocConfirmHandler(null)
  mockFetch = vi.fn()
  globalThis.fetch = mockFetch as unknown as typeof fetch
})

afterEach(() => {
  globalThis.fetch = realFetch
})

describe('apiFetch signed-document flow', () => {
  it('asks once, retries with the override header on Continue, then remembers consent', async () => {
    const handler = vi.fn().mockResolvedValue('continue')
    setSignedDocConfirmHandler(handler)
    mockFetch.mockResolvedValueOnce(conflict()).mockResolvedValueOnce(ok()).mockResolvedValueOnce(ok())

    const body = JSON.stringify({ pages: [0], angle: 90 })
    const res = await apiFetch(EDIT_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body })
    expect(res.status).toBe(200)
    expect(handler).toHaveBeenCalledTimes(1)
    expect(handler.mock.calls[0][0]).toMatchObject({ docId: DOC, signers: ['Alice Signer'] })
    expect(mockFetch).toHaveBeenCalledTimes(2)
    expect(headerOf(mockFetch.mock.calls[0][1])).toBeNull()
    const retryInit = mockFetch.mock.calls[1][1] as RequestInit
    expect(headerOf(retryInit)).toBe('1')
    expect(new Headers(retryInit.headers).get('Content-Type')).toBe('application/json')
    expect(retryInit.body).toBe(body)
    expect(hasSignatureConsent(DOC)).toBe(true)

    // Next edit on the same doc goes straight through with the header.
    await apiFetch(EDIT_URL, { method: 'POST', body })
    expect(mockFetch).toHaveBeenCalledTimes(3)
    expect(headerOf(mockFetch.mock.calls[2][1])).toBe('1')
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('Cancel returns the 409 untouched and does not retry', async () => {
    setSignedDocConfirmHandler(vi.fn().mockResolvedValue('cancel'))
    mockFetch.mockResolvedValueOnce(conflict())
    const res = await apiFetch(EDIT_URL, { method: 'POST', body: '{}' })
    expect(res.status).toBe(409)
    expect(mockFetch).toHaveBeenCalledTimes(1)
    expect(hasSignatureConsent(DOC)).toBe(false)
  })

  it('Save a copy first downloads the signed original before retrying', async () => {
    setSignedDocConfirmHandler(vi.fn().mockResolvedValue('copy'))
    mockFetch
      .mockResolvedValueOnce(conflict())
      .mockResolvedValueOnce(new Response(new Blob(['%PDF-1.7']), { status: 200 }))
      .mockResolvedValueOnce(ok())
    const res = await apiFetch(EDIT_URL, { method: 'POST', body: '{}' })
    expect(res.status).toBe(200)
    expect(String(mockFetch.mock.calls[1][0])).toBe(`${API_BASE}/api/pdf/${DOC}/export?flatten=false`)
    expect(headerOf(mockFetch.mock.calls[2][1])).toBe('1')
  })

  it('ignores 409s that are not signed_document and non-mutating requests', async () => {
    const handler = vi.fn().mockResolvedValue('continue')
    setSignedDocConfirmHandler(handler)
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'busy' }), { status: 409 }))
    expect((await apiFetch(EDIT_URL, { method: 'POST' })).status).toBe(409)
    mockFetch.mockResolvedValueOnce(conflict())
    expect((await apiFetch(`${API_BASE}/api/pdf/${DOC}/text`)).status).toBe(409)
    expect(handler).not.toHaveBeenCalled()
  })

  it('api.ts helpers go through the guard (replaceText)', async () => {
    setSignedDocConfirmHandler(vi.fn().mockResolvedValue('continue'))
    mockFetch.mockResolvedValueOnce(conflict()).mockResolvedValueOnce(ok({ replaced: 1 }))
    await expect(replaceText(DOC, 'a', 'b')).resolves.toEqual({ replaced: 1 })
    expect(headerOf(mockFetch.mock.calls[1][1])).toBe('1')
  })

  it('installSignedDocFetchGuard routes plain fetch() from feature clients through the guard', async () => {
    setSignedDocConfirmHandler(vi.fn().mockResolvedValue('continue'))
    mockFetch.mockResolvedValueOnce(conflict()).mockResolvedValueOnce(ok())
    const uninstall = installSignedDocFetchGuard()
    try {
      const res = await fetch(EDIT_URL, { method: 'POST', body: '{}' })
      expect(res.status).toBe(200)
      expect(mockFetch).toHaveBeenCalledTimes(2)
      expect(headerOf(mockFetch.mock.calls[1][1])).toBe('1')
    } finally {
      uninstall()
    }
    expect(globalThis.fetch).toBe(mockFetch)
  })
})

describe('<SignedDocGuard />', () => {
  it('shows who signed and retries on Continue', async () => {
    mockFetch.mockResolvedValueOnce(conflict()).mockResolvedValueOnce(ok())
    const { unmount } = render(<SignedDocGuard />)
    let result: Promise<Response> | undefined
    act(() => {
      result = fetch(EDIT_URL, { method: 'POST', body: '{}' })
    })
    await screen.findByRole('dialog')
    expect(screen.getByText('Alice Signer')).toBeInTheDocument()
    expect(screen.getByText(/Editing will invalidate the/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save a copy first' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    const res = await result!
    expect(res.status).toBe(200)
    expect(headerOf(mockFetch.mock.calls[1][1])).toBe('1')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    unmount()
  })

  it('Cancel closes the dialog and leaves the 409', async () => {
    mockFetch.mockResolvedValueOnce(conflict())
    const { unmount } = render(<SignedDocGuard />)
    let result: Promise<Response> | undefined
    act(() => {
      result = fetch(EDIT_URL, { method: 'POST', body: '{}' })
    })
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect((await result!).status).toBe(409)
    expect(mockFetch).toHaveBeenCalledTimes(1)
    unmount()
  })
})
