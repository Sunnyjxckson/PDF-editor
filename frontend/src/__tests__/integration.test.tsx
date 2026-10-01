import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { useEditorStore, type Tool } from '@/lib/store'
import { MODES, modeForShortcut, activateMode } from '@/lib/modes'
import type { DocumentInfo } from '@/lib/api'

vi.mock('@/components/features/SignPanel', () => ({ default: () => <div>sign-panel</div> }))
vi.mock('@/components/features/FormsPanel', () => ({ default: () => <div>forms-panel</div> }))
vi.mock('@/components/features/RedactPanel', () => ({ default: () => <div>redact-panel</div> }))
vi.mock('@/components/features/ConvertPanel', () => ({ default: () => <div>convert-panel</div> }))
vi.mock('@/components/features/OrganizeCommentsPanel', () => ({ default: () => <div>comments-panel</div> }))
vi.mock('@/components/features/OrganizeBookmarksPanel', () => ({ default: () => <div>bookmarks-panel</div> }))

import SidePanel from '@/components/SidePanel'
import { performUndo } from '@/lib/history'

const doc = (n: number): DocumentInfo => ({
  id: 'd1',
  page_count: n,
  metadata: {},
  pages: Array.from({ length: n }, (_, i) => ({ index: i, width: 612, height: 792, rotation: 0 })),
})

beforeEach(() => {
  useEditorStore.getState().reset()
})
afterEach(() => {
  vi.restoreAllMocks()
})

describe('store: re-setting the same document', () => {
  it('keeps version counters moving forward so cached renders are not reused', () => {
    const st = useEditorStore.getState()
    st.setDocument(doc(5), 'd1')
    st.bumpVersion()
    st.bumpVersion()
    st.setCurrentPage(4)
    useEditorStore.getState().setDocument(doc(3), 'd1') // e.g. after deleting 2 pages
    const s = useEditorStore.getState()
    expect(s.pageVersion).toBe(3)
    expect(s.pdfVersion).toBe(3)
    expect(s.currentPage).toBe(2) // clamped, not reset to 0
    expect(s.totalPages).toBe(3)
  })

  it('a different document still starts from scratch', () => {
    const st = useEditorStore.getState()
    st.setDocument(doc(5), 'd1')
    st.bumpVersion()
    st.setCurrentPage(3)
    useEditorStore.getState().setDocument({ ...doc(2), id: 'd2' }, 'd2')
    const s = useEditorStore.getState()
    expect(s.pageVersion).toBe(0)
    expect(s.currentPage).toBe(0)
  })
})

describe('store: one overlay mode at a time', () => {
  it('tools with a panel open it, and leaving the tool closes it', () => {
    const st = useEditorStore.getState()
    st.setActiveTool('sign')
    expect(useEditorStore.getState().activePanel).toBe('sign')
    useEditorStore.getState().setActiveTool('objects')
    expect(useEditorStore.getState().activePanel).toBeNull()
    expect(useEditorStore.getState().activeTool).toBe('objects')
  })

  it('opening a non-overlay panel drops the sign/forms/redact overlay', () => {
    useEditorStore.getState().setActiveTool('redact')
    useEditorStore.getState().setActivePanel('convert')
    const s = useEditorStore.getState()
    expect(s.activePanel).toBe('convert')
    expect(s.activeTool).toBe('select')
  })

  it('closing the forms panel leaves forms mode', () => {
    useEditorStore.getState().setActiveTool('forms')
    useEditorStore.getState().setActivePanel(null)
    expect(useEditorStore.getState().activeTool).toBe('select')
  })

  it('comment mode picks a markup tool so the overlay is live', () => {
    useEditorStore.getState().setActiveTool('comment')
    const s = useEditorStore.getState()
    expect(s.markupSettings.tool).toBe('highlight')
    expect(s.activePanel).toBe('comments')
  })
})

describe('modes', () => {
  it('every shortcut is unique and every tool is reachable', () => {
    const keys = MODES.map((m) => m.shortcut).filter(Boolean)
    expect(new Set(keys).size).toBe(keys.length)
    const tools: Tool[] = ['select', 'text', 'highlight', 'draw', 'eraser', 'region_select', 'edit_text', 'objects', 'comment', 'sign', 'forms', 'redact']
    for (const t of tools) expect(MODES.some((m) => m.tool === t)).toBe(true)
  })

  it('shortcut "e" edits text, Shift+E erases, "p" opens Organize', () => {
    const st = useEditorStore.getState()
    const actions = () => ({ ...useEditorStore.getState(), organizeOpen: useEditorStore.getState().organizeOpen })
    activateMode(modeForShortcut('e')!, actions())
    expect(useEditorStore.getState().activeTool).toBe('edit_text')
    activateMode(modeForShortcut('E')!, actions())
    expect(useEditorStore.getState().activeTool).toBe('eraser')
    activateMode(modeForShortcut('p')!, actions())
    expect(useEditorStore.getState().organizeOpen).toBe(true)
    // choosing a page tool leaves Organize
    activateMode(modeForShortcut('o')!, actions())
    expect(useEditorStore.getState().organizeOpen).toBe(false)
    expect(st).toBeDefined()
  })
})

describe('SidePanel', () => {
  it('renders exactly the active panel', () => {
    useEditorStore.getState().setDocument(doc(2), 'd1')
    useEditorStore.getState().setActiveTool('forms')
    const { rerender } = render(<SidePanel />)
    expect(screen.getByText('forms-panel')).toBeInTheDocument()
    act(() => useEditorStore.getState().setActiveTool('redact'))
    rerender(<SidePanel />)
    expect(screen.queryByText('forms-panel')).toBeNull()
    expect(screen.getByText('redact-panel')).toBeInTheDocument()
  })
})

describe('undo', () => {
  it('posts /undo then re-fetches /info and re-renders', async () => {
    useEditorStore.getState().setDocument(doc(2), 'd1')
    const calls: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      calls.push(`${init?.method ?? 'GET'} ${url.replace(/^https?:\/\/[^/]+/, '')}`)
      if (url.endsWith('/undo')) return new Response(JSON.stringify({ status: 'ok', undone_operation: 'Edit text', can_undo: false, can_redo: true }))
      return new Response(JSON.stringify(doc(1)))
    })
    const before = useEditorStore.getState().pageVersion
    expect(await performUndo()).toBe(true)
    expect(calls).toEqual(['POST /api/pdf/d1/undo', 'GET /api/pdf/d1/info'])
    const s = useEditorStore.getState()
    expect(s.totalPages).toBe(1)
    expect(s.pageVersion).toBe(before + 1)
    expect(s.toasts.at(-1)?.message).toBe('Undone: Edit text')
  })
})

describe('organize view', () => {
  it('opening Organize drops a page-overlay mode and its panel', () => {
    useEditorStore.getState().setActiveTool('redact')
    useEditorStore.getState().setOrganizeOpen(true)
    const s = useEditorStore.getState()
    expect(s.organizeOpen).toBe(true)
    expect(s.activeTool).toBe('select')
    expect(s.activePanel).toBeNull()
  })
})
