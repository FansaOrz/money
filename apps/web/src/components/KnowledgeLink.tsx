"use client";

import { useCallback, useRef, useState, type ReactNode } from "react";
import { getKnowledgeEntry } from "@/lib/knowledge/entries";
import { KnowledgeDialog } from "@/components/KnowledgeDialog";

export function KnowledgeLink({
  slug,
  children,
  className = "",
}: {
  slug: string;
  children?: ReactNode;
  className?: string;
}) {
  const entry = getKnowledgeEntry(slug);
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);
  if (!entry) return <>{children ?? slug}</>;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
        className={`inline-flex cursor-help items-center gap-1 rounded-sm border-b border-dashed border-slate-400 text-left hover:border-blue-500 hover:text-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1 ${className}`}
        title={`点击查看：${entry.title}`}
      >
        {children ?? entry.title}
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" className="h-3.5 w-3.5 shrink-0 opacity-70" aria-hidden="true">
          <circle cx="10" cy="10" r="7.5" />
          <path d="M8.2 7.7a2 2 0 1 1 2.7 1.9c-.8.3-.9.8-.9 1.5M10 13.8v.1" />
        </svg>
      </button>
      <KnowledgeDialog slug={slug} open={open} onClose={close} returnFocus={triggerRef.current} />
    </>
  );
}

export function MetricLabel({ term, children }: { term?: string; children: ReactNode }) {
  return term ? <KnowledgeLink slug={term}>{children}</KnowledgeLink> : <>{children}</>;
}
