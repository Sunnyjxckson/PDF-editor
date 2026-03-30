import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  uploadPDF,
  getDocumentInfo,
  getPageUrl,
  findText,
  sendChatMessage,
} from '@/lib/api'

const mockFetch = vi.fn()
global.fetch = mockFetch

beforeEach(() => {
  mockFetch.mockReset()
})

function mockOkResponse(data: unknown) {
  return {
    ok: true,
    json: () => Promise.resolve(data),
  }
}

function mockErrorResponse(status = 500) {
  return {
    ok: false,
    status,
    json: () => Promise.resolve({ detail: 'error' }),
  }
}

describe('API client', () => {
  describe('uploadPDF', () => {
    it('calls the correct endpoint with FormData', async () => {
      const responseData = { id: 'abc', filename: 'test.pdf', page_count: 3, metadata: {} }
      mockFetch.mockResolvedValueOnce(mockOkResponse(responseData))

      const file = new File(['fake-pdf-content'], 'test.pdf', { type: 'application/pdf' })
      const result = await uploadPDF(file)

      expect(mockFetch).toHaveBeenCalledTimes(1)
      const [url, options] = mockFetch.mock.calls[0]
      expect(url).toBe('http://localhost:8000/api/pdf/upload')
      expect(options.method).toBe('POST')
      expect(options.body).toBeInstanceOf(FormData)
      expect((options.body as FormData).get('file')).toBe(file)
      expect(result).toEqual(responseData)
    })
  })

  describe('getDocumentInfo', () => {
    it('fetches document info for the given docId', async () => {
      const info = { id: 'doc-1', page_count: 5, metadata: {}, pages: [] }
      mockFetch.mockResolvedValueOnce(mockOkResponse(info))

      const result = await getDocumentInfo('doc-1')

      expect(mockFetch).toHaveBeenCalledWith('http://localhost:8000/api/pdf/doc-1/info')
      expect(result).toEqual(info)
    })
  })

  describe('getPageUrl', () => {
    it('returns the correct URL with default dpi', () => {
      const url = getPageUrl('doc-1', 0)
      expect(url).toBe('http://localhost:8000/api/pdf/doc-1/page/0?dpi=150')
    })

    it('returns the correct URL with custom dpi', () => {
      const url = getPageUrl('doc-1', 2, 300)
      expect(url).toBe('http://localhost:8000/api/pdf/doc-1/page/2?dpi=300')
    })
  })

  describe('findText', () => {
    it('sends find request with correct parameters', async () => {
      const findResult = { matches: [{ page: 0, bbox: [10, 20, 100, 40] }], count: 1 }
      mockFetch.mockResolvedValueOnce(mockOkResponse(findResult))

      const result = await findText('doc-1', 'hello')

      expect(mockFetch).toHaveBeenCalledTimes(1)
      const [url, options] = mockFetch.mock.calls[0]
      expect(url).toBe('http://localhost:8000/api/pdf/doc-1/find')
      expect(options.method).toBe('POST')
      expect(JSON.parse(options.body)).toEqual({
        find_text: 'hello',
        page: undefined,
        match_case: false,
      })
      expect(result).toEqual(findResult)
    })

    it('passes page and matchCase parameters', async () => {
      mockFetch.mockResolvedValueOnce(mockOkResponse({ matches: [], count: 0 }))

      await findText('doc-1', 'Hello', 2, true)

      const body = JSON.parse(mockFetch.mock.calls[0][1].body)
      expect(body.page).toBe(2)
      expect(body.match_case).toBe(true)
    })
  })

  describe('sendChatMessage', () => {
    it('sends chat message with correct payload', async () => {
      const chatResponse = {
        response: 'Here is the answer',
        changed: false,
        intent: {},
        new_page_count: null,
      }
      mockFetch.mockResolvedValueOnce(mockOkResponse(chatResponse))

      const result = await sendChatMessage('doc-1', 'What is this document about?', 0)

      expect(mockFetch).toHaveBeenCalledTimes(1)
      const [url, options] = mockFetch.mock.calls[0]
      expect(url).toBe('http://localhost:8000/api/pdf/doc-1/chat')
      expect(options.method).toBe('POST')
      expect(JSON.parse(options.body)).toEqual({
        message: 'What is this document about?',
        current_page: 0,
      })
      expect(result).toEqual(chatResponse)
    })
  })

  describe('error handling', () => {
    it('throws on failed upload', async () => {
      mockFetch.mockResolvedValueOnce(mockErrorResponse())
      const file = new File([''], 'test.pdf')
      await expect(uploadPDF(file)).rejects.toThrow('Upload failed')
    })

    it('throws on failed getDocumentInfo', async () => {
      mockFetch.mockResolvedValueOnce(mockErrorResponse())
      await expect(getDocumentInfo('doc-1')).rejects.toThrow('Failed to get document info')
    })

    it('throws on failed findText', async () => {
      mockFetch.mockResolvedValueOnce(mockErrorResponse())
      await expect(findText('doc-1', 'test')).rejects.toThrow('Find failed')
    })

    it('throws on failed sendChatMessage', async () => {
      mockFetch.mockResolvedValueOnce(mockErrorResponse())
      await expect(sendChatMessage('doc-1', 'hi', 0)).rejects.toThrow('Chat failed')
    })
  })
})
