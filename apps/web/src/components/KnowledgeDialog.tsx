"use client";

import Link from "next/link";
import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { CATEGORY_LABELS, getKnowledgeEntry } from "@/lib/knowledge/entries";
import type { KnowledgeEntry, MetricDirection } from "@/lib/knowledge/types";

const DIRECTION_STYLE: Record<MetricDirection, string> = {
  higher: "border-rose-200 bg-rose-50 text-rose-700",
  lower: "border-emerald-200 bg-emerald-50 text-emerald-700",
  closer_to_zero: "border-blue-200 bg-blue-50 text-blue-700",
  threshold: "border-violet-200 bg-violet-50 text-violet-700",
  contextual: "border-amber-200 bg-amber-50 text-amber-700",
  neutral: "border-slate-200 bg-slate-100 text-slate-600",
};

export function DirectionBadge({ entry }: { entry: KnowledgeEntry }) {
  return (
    <span className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-semibold ${DIRECTION_STYLE[entry.direction]}`}>
      {entry.directionLabel}
    </span>
  );
}

export function KnowledgeDialog({
  slug,
  open,
  onClose,
  returnFocus,
}: {
  slug: string;
  open: boolean;
  onClose: () => void;
  returnFocus?: HTMLElement | null;
}) {
  const entry = getKnowledgeEntry(slug);
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const original = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusable = [...panelRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )];
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.body.style.overflow = original;
      document.removeEventListener("keydown", keydown);
      returnFocus?.focus();
    };
  }, [open, onClose, returnFocus]);

  if (!open || !entry || typeof document === "undefined") return null;

  return createPortal(
    <div className="fixed inset-0 z-[80] flex items-end justify-center sm:items-center sm:p-5">
      <button
        type="button"
        aria-label="关闭指标解释"
        className="absolute inset-0 bg-slate-950/45 backdrop-blur-[1px]"
        onClick={onClose}
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="relative z-10 flex max-h-[88vh] w-full flex-col overflow-hidden rounded-t-2xl bg-white shadow-2xl sm:max-w-2xl sm:rounded-2xl"
      >
        <div className="flex items-start justify-between gap-4 border-b border-slate-100 px-5 py-4 sm:px-6">
          <div>
            <p className="text-xs font-medium text-slate-400">{CATEGORY_LABELS[entry.category]}</p>
            <h2 id={titleId} className="mt-0.5 text-xl font-semibold text-slate-900">{entry.title}</h2>
            {entry.aliases.length > 0 && <p className="mt-1 text-xs text-slate-400">{entry.aliases.join(" · ")}</p>}
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className="rounded-lg p-2 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
            aria-label="关闭"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-5 w-5">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </div>

        <div className="overflow-y-auto px-5 py-5 sm:px-6">
          <DirectionBadge entry={entry} />
          <p className="mt-4 text-base font-medium leading-relaxed text-slate-800">{entry.summary}</p>
          <div className="mt-4 space-y-2 text-sm leading-relaxed text-slate-600">
            {(entry.definition ?? []).map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
          </div>
          {entry.formula && (
            <div className="mt-4 rounded-xl border border-blue-100 bg-blue-50/70 px-4 py-3">
              <p className="text-xs font-semibold text-blue-700">计算公式</p>
              <p className="mt-1 font-mono text-sm leading-relaxed text-slate-800">{entry.formula}</p>
            </div>
          )}
          <section className="mt-5">
            <h3 className="text-sm font-semibold text-slate-800">本系统口径</h3>
            <ul className="mt-2 list-inside list-disc space-y-1 text-sm leading-relaxed text-slate-600">
              {entry.systemConvention.map((item) => <li key={item}>{item}</li>)}
            </ul>
          </section>
          {entry.interpretation && entry.interpretation.length > 0 && (
            <section className="mt-5">
              <h3 className="text-sm font-semibold text-slate-800">如何解读</h3>
              <div className="mt-2 divide-y divide-slate-100 rounded-xl border border-slate-200">
                {entry.interpretation.map((item) => (
                  <div key={item.range} className="grid grid-cols-[100px_1fr] gap-3 px-3.5 py-2.5 text-sm">
                    <span className="font-medium tabular-nums text-slate-800">{item.range}</span>
                    <span className="text-slate-600">{item.meaning}</span>
                  </div>
                ))}
              </div>
            </section>
          )}
          <section className="mt-5 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
            <h3 className="text-sm font-semibold text-amber-800">注意事项</h3>
            <ul className="mt-2 list-inside list-disc space-y-1 text-xs leading-relaxed text-amber-800">
              {entry.cautions.map((item) => <li key={item}>{item}</li>)}
            </ul>
          </section>
          {entry.related.length > 0 && (
            <section className="mt-5">
              <h3 className="text-xs font-semibold text-slate-500">相关词条</h3>
              <div className="mt-2 flex flex-wrap gap-2">
                {entry.related.map((related) => {
                  const target = getKnowledgeEntry(related);
                  return target ? (
                    <Link key={related} href={`/knowledge/${related}`} className="rounded-full bg-slate-100 px-3 py-1 text-xs text-slate-600 hover:bg-slate-200">
                      {target.title}
                    </Link>
                  ) : null;
                })}
              </div>
            </section>
          )}
          <div className="mt-5 border-t border-slate-100 pt-3 text-[11px] leading-relaxed text-slate-400">
            {entry.codeRefs.map((item) => <p key={`${item.file}:${item.line}`}>实现参考：{item.label} · {item.file}:{item.line}</p>)}
          </div>
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-slate-100 bg-slate-50 px-5 py-3 sm:px-6">
          <p className="text-[11px] text-slate-400">知识解释仅供研究参考，不构成投资建议。</p>
          <Link href={`/knowledge/${entry.slug}`} className="shrink-0 text-xs font-medium text-blue-700 hover:underline">
            查看完整词条 →
          </Link>
        </div>
      </div>
    </div>,
    document.body
  );
}
