"use client";

import { useCallback } from "react";
import { useEditorStore } from "@/lib/store";
import { getDocumentInfo } from "@/lib/api";
import type { CreatedDocument } from "@/lib/features/convert";
import SignPanel from "./features/SignPanel";
import FormsPanel from "./features/FormsPanel";
import RedactPanel from "./features/RedactPanel";
import ConvertPanel from "./features/ConvertPanel";
import OrganizeCommentsPanel from "./features/OrganizeCommentsPanel";
import OrganizeBookmarksPanel from "./features/OrganizeBookmarksPanel";
import DocumentToolsPanel from "./DocumentToolsPanel";

/**
 * The right-hand feature panel. Exactly one is open at a time (store.activePanel);
 * sign/forms/redact panels are tied to their page overlay through the store.
 * On phones it covers the page as a sheet.
 */
export default function SidePanel() {
  const activePanel = useEditorStore((s) => s.activePanel);
  const docId = useEditorStore((s) => s.docId);
  const currentPage = useEditorStore((s) => s.currentPage);
  const pageVersion = useEditorStore((s) => s.pageVersion);
  const filename = useEditorStore((s) => s.filename);
  const setActivePanel = useEditorStore((s) => s.setActivePanel);
  const setCurrentPage = useEditorStore((s) => s.setCurrentPage);
  const reloadDocument = useEditorStore((s) => s.reloadDocument);
  const addToast = useEditorStore((s) => s.addToast);
  const markupAuthor = useEditorStore((s) => s.markupSettings.author);

  const close = useCallback(() => setActivePanel(null), [setActivePanel]);
  const changed = useCallback(() => { void reloadDocument(); }, [reloadDocument]);

  const openCreated = useCallback(async (doc: CreatedDocument) => {
    try {
      const info = await getDocumentInfo(doc.id);
      const st = useEditorStore.getState();
      st.setDocument(info, doc.id);
      st.setFilename(doc.filename);
      st.setActivePanel(null);
      addToast(`Opened ${doc.filename}`, "success");
    } catch (e) {
      addToast(e instanceof Error ? e.message : "Could not open the new PDF", "error");
    }
  }, [addToast]);

  if (!docId || !activePanel) return null;

  let body: React.ReactNode = null;
  switch (activePanel) {
    case "sign":
      body = <SignPanel docId={docId} currentPage={currentPage} onDocumentChanged={changed} onClose={close} onNotify={addToast} />;
      break;
    case "forms":
      body = <FormsPanel docId={docId} currentPage={currentPage} onDocumentChanged={changed} refreshKey={pageVersion} onNavigatePage={setCurrentPage} onClose={close} />;
      break;
    case "redact":
      body = <RedactPanel docId={docId} currentPage={currentPage} onDocumentChanged={changed} onNavigate={setCurrentPage} onClose={close} notify={addToast} />;
      break;
    case "comments":
      body = <OrganizeCommentsPanel docId={docId} currentPage={currentPage} onJumpToPage={setCurrentPage} onDocumentChanged={changed} refreshKey={pageVersion} author={markupAuthor} onClose={close} />;
      break;
    case "bookmarks":
      body = <OrganizeBookmarksPanel docId={docId} currentPage={currentPage} onJumpToPage={setCurrentPage} onDocumentChanged={changed} refreshKey={pageVersion} onClose={close} />;
      break;
    case "convert":
      body = (
        <div className="flex h-full w-full sm:w-80 flex-col overflow-y-auto border-l border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900">
          <ConvertPanel docId={docId} currentPage={currentPage} onDocumentChanged={changed} onDocumentCreated={openCreated} filename={filename ?? undefined} onClose={close} />
        </div>
      );
      break;
    case "protect":
    case "tools":
      body = <DocumentToolsPanel section={activePanel} />;
      break;
  }

  return (
    <div
      data-panel={activePanel}
      className="fixed inset-x-0 top-12 bottom-14 z-40 sm:static sm:z-auto sm:inset-auto w-full sm:w-80 shrink-0 h-auto sm:h-full overflow-hidden shadow-xl sm:shadow-none"
    >
      {body}
    </div>
  );
}
