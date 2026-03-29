"use client";

import { useEditorStore } from "@/lib/store";
import UploadScreen from "@/components/UploadScreen";
import Editor from "@/components/Editor";

export default function Home() {
  const docId = useEditorStore((s) => s.docId);
  return docId ? <Editor /> : <UploadScreen />;
}
