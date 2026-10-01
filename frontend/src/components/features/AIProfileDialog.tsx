"use client";

/**
 * Edit the personal profile used for "Fill form from my profile". Stored in
 * this browser's localStorage only; sent to the server only with that request.
 */
import { useState } from "react";
import { X } from "lucide-react";
import { PROFILE_FIELDS, clearProfile, loadProfile, saveProfile } from "@/lib/features/ai";

export interface AIProfileDialogProps {
  open: boolean;
  onClose: () => void;
}

export default function AIProfileDialog({ open, onClose }: AIProfileDialogProps) {
  if (!open) return null;
  return <ProfileForm onClose={onClose} />;
}

function ProfileForm({ onClose }: { onClose: () => void }) {
  const [profile, setProfile] = useState<Record<string, string>>(() => loadProfile());
  const [extraKey, setExtraKey] = useState("");
  const extras = Object.keys(profile).filter((k) => !PROFILE_FIELDS.some((f) => f.key === k));

  const field = (key: string, label: string) => (
    <label key={key} className="block">
      <span className="text-[11px] text-gray-500">{label}</span>
      <input
        aria-label={label}
        value={profile[key] ?? ""}
        onChange={(e) => setProfile((p) => ({ ...p, [key]: e.target.value }))}
        className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-2 py-1 text-sm"
      />
    </label>
  );

  return (
    <div role="dialog" aria-label="My profile" className="fixed inset-0 z-[70] bg-black/40 flex items-center justify-center p-4">
      <div className="w-full max-w-md rounded-xl bg-white dark:bg-gray-900 shadow-2xl p-4 space-y-3 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-sm">My profile for form filling</h3>
          <button onClick={onClose} aria-label="Close" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800">
            <X className="w-4 h-4" />
          </button>
        </div>
        <p className="text-xs text-gray-500">Saved only in this browser. Sent to the AI only when you ask it to fill a form from your profile.</p>
        <div className="grid grid-cols-2 gap-2">
          {PROFILE_FIELDS.map((f) => field(f.key, f.label))}
          {extras.map((k) => field(k, k))}
        </div>
        <div className="flex gap-2">
          <input
            placeholder="Add another field (e.g. passport_number)"
            value={extraKey}
            onChange={(e) => setExtraKey(e.target.value)}
            className="flex-1 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-2 py-1 text-xs"
          />
          <button
            onClick={() => {
              const k = extraKey.trim().replace(/\s+/g, "_");
              if (k) setProfile((p) => ({ ...p, [k]: p[k] ?? "" }));
              setExtraKey("");
            }}
            className="text-xs px-2 rounded-md border border-gray-300 dark:border-gray-600"
          >
            Add
          </button>
        </div>
        <div className="flex justify-between">
          <button
            onClick={() => { clearProfile(); setProfile({}); }}
            className="text-xs text-red-600 hover:underline"
          >
            Clear profile
          </button>
          <button
            onClick={() => { saveProfile(profile); onClose(); }}
            className="text-sm px-3 py-1.5 rounded-lg bg-purple-600 text-white hover:bg-purple-700"
          >
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
