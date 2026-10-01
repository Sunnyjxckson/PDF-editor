"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import { useEditorStore } from "@/lib/store";
import { MODES, MODE_GROUPS, activateMode, isModeActive, shortcutLabel } from "@/lib/modes";

/** Acrobat-style vertical tool rail (desktop). Mobile uses the Toolbar menu. */
export default function ToolRail() {
  const activeTool = useEditorStore((s) => s.activeTool);
  const activePanel = useEditorStore((s) => s.activePanel);
  const organizeOpen = useEditorStore((s) => s.organizeOpen);
  const setActiveTool = useEditorStore((s) => s.setActiveTool);
  const togglePanel = useEditorStore((s) => s.togglePanel);
  const setOrganizeOpen = useEditorStore((s) => s.setOrganizeOpen);
  const docId = useEditorStore((s) => s.docId);

  if (!docId) return null;
  const state = { activeTool, activePanel, organizeOpen };

  return (
    <Tooltip.Provider delayDuration={400}>
      <nav
        aria-label="Tools"
        className="hidden sm:flex flex-col w-[72px] shrink-0 overflow-y-auto overflow-x-hidden bg-white dark:bg-gray-900 border-r border-gray-200 dark:border-gray-800 py-1.5"
      >
        {MODE_GROUPS.map((group, gi) => (
          <div key={group} role="group" aria-label={group} className={gi > 0 ? "mt-1 pt-1 border-t border-gray-100 dark:border-gray-800" : ""}>
            {MODES.filter((m) => m.group === group).map((mode) => {
              const active = isModeActive(mode, state);
              const Icon = mode.icon;
              return (
                <Tooltip.Root key={mode.id}>
                  <Tooltip.Trigger asChild>
                    <button
                      type="button"
                      aria-pressed={active}
                      aria-label={mode.label}
                      data-mode={mode.id}
                      onClick={() => activateMode(mode, { setActiveTool, togglePanel, setOrganizeOpen, organizeOpen })}
                      className={`mx-1.5 my-px w-[60px] flex flex-col items-center gap-0.5 rounded-lg px-1 py-1 text-[10px] leading-tight transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 ${
                        active
                          ? "bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300"
                          : "text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800"
                      }`}
                    >
                      <Icon className="w-5 h-5" />
                      <span className="text-center">{mode.label}</span>
                    </button>
                  </Tooltip.Trigger>
                  <Tooltip.Portal>
                    <Tooltip.Content
                      side="right"
                      sideOffset={6}
                      className="max-w-[220px] px-2.5 py-1.5 text-xs bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900 rounded-lg shadow-lg z-[100]"
                    >
                      <span className="font-medium">{mode.label}</span>
                      {mode.shortcut && (
                        <kbd className="ml-1.5 opacity-70">{shortcutLabel(mode.shortcut)}</kbd>
                      )}
                      <div className="opacity-80">{mode.hint}</div>
                      <Tooltip.Arrow className="fill-gray-900 dark:fill-gray-100" />
                    </Tooltip.Content>
                  </Tooltip.Portal>
                </Tooltip.Root>
              );
            })}
          </div>
        ))}
      </nav>
    </Tooltip.Provider>
  );
}
