import { describe, it, expect, beforeEach, vi } from 'vitest'
import { useEditorStore } from '@/lib/store'
import type { DocumentInfo } from '@/lib/api'

// Reset the store before each test
beforeEach(() => {
  vi.useRealTimers()
  // Clear all toasts manually since reset() doesn't clear them
  const state = useEditorStore.getState()
  state.toasts.forEach((t) => state.removeToast(t.id))
  useEditorStore.getState().reset()
  vi.restoreAllMocks()
})

describe('useEditorStore', () => {
  describe('setDocument', () => {
    it('sets docId, totalPages, and currentPage', () => {
      const doc: DocumentInfo = {
        id: 'doc-1',
        page_count: 5,
        metadata: { title: 'Test PDF' },
        pages: [
          { index: 0, width: 612, height: 792, rotation: 0 },
          { index: 1, width: 612, height: 792, rotation: 0 },
          { index: 2, width: 612, height: 792, rotation: 0 },
          { index: 3, width: 612, height: 792, rotation: 0 },
          { index: 4, width: 612, height: 792, rotation: 0 },
        ],
      }

      useEditorStore.getState().setDocument(doc, 'doc-1')

      const state = useEditorStore.getState()
      expect(state.docId).toBe('doc-1')
      expect(state.totalPages).toBe(5)
      expect(state.currentPage).toBe(0)
      expect(state.document).toEqual(doc)
    })
  })

  describe('setCurrentPage', () => {
    it('updates the current page', () => {
      useEditorStore.getState().setCurrentPage(3)
      expect(useEditorStore.getState().currentPage).toBe(3)
    })
  })

  describe('setZoom', () => {
    it('sets zoom to a valid value', () => {
      useEditorStore.getState().setZoom(1.5)
      expect(useEditorStore.getState().zoom).toBe(1.5)
    })

    it('clamps zoom to minimum of 0.25', () => {
      useEditorStore.getState().setZoom(0.1)
      expect(useEditorStore.getState().zoom).toBe(0.25)
    })

    it('clamps zoom to maximum of 3', () => {
      useEditorStore.getState().setZoom(5)
      expect(useEditorStore.getState().zoom).toBe(3)
    })

    it('allows zoom at exact boundaries', () => {
      useEditorStore.getState().setZoom(0.25)
      expect(useEditorStore.getState().zoom).toBe(0.25)

      useEditorStore.getState().setZoom(3)
      expect(useEditorStore.getState().zoom).toBe(3)
    })
  })

  describe('setActiveTool', () => {
    it.each(['select', 'text', 'highlight', 'draw', 'eraser'] as const)(
      'sets active tool to %s',
      (tool) => {
        useEditorStore.getState().setActiveTool(tool)
        expect(useEditorStore.getState().activeTool).toBe(tool)
      },
    )
  })

  describe('toggleSidebar', () => {
    it('toggles sidebar from open to closed', () => {
      // Default is true
      expect(useEditorStore.getState().sidebarOpen).toBe(true)
      useEditorStore.getState().toggleSidebar()
      expect(useEditorStore.getState().sidebarOpen).toBe(false)
    })

    it('toggles sidebar from closed to open', () => {
      useEditorStore.getState().setSidebarOpen(false) // ensure closed
      useEditorStore.getState().toggleSidebar() // open
      expect(useEditorStore.getState().sidebarOpen).toBe(true)
    })
  })

  describe('toggleDarkMode', () => {
    it('toggles dark mode', () => {
      const initial = useEditorStore.getState().darkMode
      useEditorStore.getState().toggleDarkMode()
      expect(useEditorStore.getState().darkMode).toBe(!initial)
    })
  })

  describe('addToast and removeToast', () => {
    it('adds a toast with default type info', () => {
      vi.useFakeTimers()
      useEditorStore.getState().addToast('Hello')
      const toasts = useEditorStore.getState().toasts
      expect(toasts).toHaveLength(1)
      expect(toasts[0].message).toBe('Hello')
      expect(toasts[0].type).toBe('info')
      vi.useRealTimers()
    })

    it('adds a toast with specified type', () => {
      vi.useFakeTimers()
      useEditorStore.getState().addToast('Error occurred', 'error')
      const toasts = useEditorStore.getState().toasts
      expect(toasts).toHaveLength(1)
      expect(toasts[0].type).toBe('error')
      vi.useRealTimers()
    })

    it('removes a toast by id', () => {
      vi.useFakeTimers()
      useEditorStore.getState().addToast('Toast 1')
      const id = useEditorStore.getState().toasts[0].id
      useEditorStore.getState().removeToast(id)
      expect(useEditorStore.getState().toasts).toHaveLength(0)
      vi.useRealTimers()
    })

    it('auto-removes toast after 4 seconds', () => {
      vi.useFakeTimers()
      useEditorStore.getState().addToast('Auto remove')
      expect(useEditorStore.getState().toasts).toHaveLength(1)
      vi.advanceTimersByTime(4000)
      expect(useEditorStore.getState().toasts).toHaveLength(0)
      vi.useRealTimers()
    })
  })

  describe('bumpVersion', () => {
    it('increments pageVersion and pdfVersion', () => {
      const before = useEditorStore.getState()
      expect(before.pageVersion).toBe(0)
      expect(before.pdfVersion).toBe(0)

      useEditorStore.getState().bumpVersion()

      const after = useEditorStore.getState()
      expect(after.pageVersion).toBe(1)
      expect(after.pdfVersion).toBe(1)
    })
  })

  describe('reset', () => {
    it('resets document state to defaults', () => {
      // Set up some state
      const doc: DocumentInfo = {
        id: 'doc-1',
        page_count: 5,
        metadata: {},
        pages: [],
      }
      const store = useEditorStore.getState()
      store.setDocument(doc, 'doc-1')
      store.setCurrentPage(3)
      store.setZoom(2)
      store.setActiveTool('draw')
      store.bumpVersion()

      // Reset
      useEditorStore.getState().reset()

      const state = useEditorStore.getState()
      expect(state.document).toBeNull()
      expect(state.docId).toBeNull()
      expect(state.currentPage).toBe(0)
      expect(state.totalPages).toBe(0)
      expect(state.zoom).toBe(1)
      expect(state.activeTool).toBe('select')
      expect(state.pageVersion).toBe(0)
      expect(state.pdfVersion).toBe(0)
      expect(state.textBlocks).toEqual([])
      expect(state.optimisticEdits).toEqual([])
    })
  })
})
