"use client";

import { X } from "lucide-react";
import { useEditorStore } from "@/lib/store";
import * as Dialog from "@radix-ui/react-dialog";

const shortcuts = [
  { section: "Navigation", items: [
    { keys: ["Arrow Left"], desc: "Previous page" },
    { keys: ["Arrow Right"], desc: "Next page" },
    { keys: ["+", "="], desc: "Zoom in" },
    { keys: ["-"], desc: "Zoom out" },
    { keys: ["0"], desc: "Reset zoom to 100%" },
  ]},
  { section: "Tools", items: [
    { keys: ["V"], desc: "Select tool" },
    { keys: ["S"], desc: "Select Region tool" },
    { keys: ["T"], desc: "Text tool" },
    { keys: ["H"], desc: "Highlight tool" },
    { keys: ["D"], desc: "Draw tool" },
    { keys: ["E"], desc: "Eraser tool" },
  ]},
  { section: "Panels", items: [
    { keys: ["Ctrl", "/"], desc: "Toggle AI Chat" },
    { keys: ["Ctrl", "F"], desc: "Find & Replace" },
    { keys: ["["], desc: "Toggle page sidebar" },
  ]},
  { section: "General", items: [
    { keys: ["?"], desc: "Show keyboard shortcuts" },
    { keys: ["Esc"], desc: "Close panel / deselect" },
  ]},
];

export default function KeyboardShortcuts() {
  const shortcutsOpen = useEditorStore((s) => s.shortcutsOpen);
  const setShortcutsOpen = useEditorStore((s) => s.setShortcutsOpen);

  return (
    <Dialog.Root open={shortcutsOpen} onOpenChange={setShortcutsOpen}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/50 backdrop-blur-sm z-[200] animate-fade-in" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-[201] w-[90vw] max-w-lg max-h-[80vh] overflow-y-auto bg-white dark:bg-gray-900 rounded-2xl shadow-2xl border border-gray-200 dark:border-gray-700 animate-fade-in-scale">
          <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-800">
            <Dialog.Title className="text-lg font-semibold">Keyboard Shortcuts</Dialog.Title>
            <Dialog.Close className="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800">
              <X className="w-5 h-5" />
            </Dialog.Close>
          </div>
          <div className="p-6 space-y-6">
            {shortcuts.map((section) => (
              <div key={section.section}>
                <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">
                  {section.section}
                </h3>
                <div className="space-y-2">
                  {section.items.map((item) => (
                    <div
                      key={item.desc}
                      className="flex items-center justify-between"
                    >
                      <span className="text-sm text-gray-700 dark:text-gray-300">
                        {item.desc}
                      </span>
                      <div className="flex items-center gap-1">
                        {item.keys.map((key, i) => (
                          <span key={i}>
                            {i > 0 && <span className="text-gray-400 text-xs mx-0.5">+</span>}
                            <kbd className="inline-block px-2 py-0.5 text-xs font-mono bg-gray-100 dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-md text-gray-600 dark:text-gray-300">
                              {key}
                            </kbd>
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
