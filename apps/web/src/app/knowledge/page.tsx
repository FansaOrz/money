"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useMemo, useState } from "react";
import { CATEGORY_LABELS, KNOWLEDGE_ENTRIES } from "@/lib/knowledge/entries";
import { groupKnowledge, searchKnowledge } from "@/lib/knowledge/search";
import type { KnowledgeCategory, KnowledgeEntry } from "@/lib/knowledge/types";
import { DirectionBadge } from "@/components/KnowledgeDialog";
import { Card, EmptyState, PageHeader, Spinner } from "@/components/ui";

const ALL_CATEGORIES = Object.keys(CATEGORY_LABELS) as KnowledgeCategory[];

function isKnowledgeCategory(value: string | null): value is KnowledgeCategory {
  return value !== null && (ALL_CATEGORIES as string[]).includes(value);
}

function EntryCard({ entry }: { entry: KnowledgeEntry }) {
  return (
    <Link
      href={`/knowledge/${entry.slug}`}
      id={entry.slug}
      className="group flex h-full flex-col rounded-xl border border-slate-200 bg-white p-4 shadow-sm transition-colors hover:border-blue-300 hover:shadow"
    >
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-semibold text-slate-900 group-hover:text-blue-700">
          {entry.title}
        </h3>
        <DirectionBadge entry={entry} />
      </div>
      {entry.aliases.length > 0 && (
        <p className="mt-1 text-xs text-slate-400">{entry.aliases.join(" · ")}</p>
      )}
      <p className="mt-2 text-sm leading-relaxed text-slate-600">{entry.summary}</p>
      {entry.formula && (
        <p className="mt-3 rounded-lg bg-slate-50 px-3 py-2 font-mono text-xs leading-relaxed text-slate-700">
          {entry.formula}
        </p>
      )}
      <span className="mt-auto inline-flex pt-3 text-xs font-medium text-blue-700 group-hover:underline">
        查看完整词条 →
      </span>
    </Link>
  );
}

function KnowledgePageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const qParam = searchParams.get("q") ?? "";
  const categoryParam = searchParams.get("category");
  const activeCategory: KnowledgeCategory | "all" = isKnowledgeCategory(categoryParam)
    ? categoryParam
    : "all";

  // 输入框的本地草稿：受控于 URL，但输入时先更新本地，保证打字流畅
  const [draft, setDraft] = useState(qParam);
  const effectiveQuery = draft !== qParam ? draft : qParam;

  const syncUrl = useCallback(
    (nextQuery: string, nextCategory: KnowledgeCategory | "all") => {
      const params = new URLSearchParams();
      const trimmed = nextQuery.trim();
      if (trimmed) params.set("q", trimmed);
      if (nextCategory !== "all") params.set("category", nextCategory);
      const qs = params.toString();
      router.replace(qs ? `/knowledge?${qs}` : "/knowledge", { scroll: false });
    },
    [router]
  );

  const onQueryChange = useCallback(
    (value: string) => {
      setDraft(value);
      syncUrl(value, activeCategory);
    },
    [syncUrl, activeCategory]
  );

  const onCategoryChange = useCallback(
    (next: KnowledgeCategory | "all") => {
      syncUrl(draft, next);
    },
    [syncUrl, draft]
  );

  const results = useMemo(
    () => searchKnowledge(effectiveQuery, activeCategory),
    [effectiveQuery, activeCategory]
  );
  const groups = useMemo(() => groupKnowledge(results), [results]);

  return (
    <div>
      <PageHeader
        title="指标与方法百科"
        description={`共 ${KNOWLEDGE_ENTRIES.length} 个词条，解释本系统使用的收益、风险、因子与验证方法及口径。`}
      />

      {/* 搜索 + 分类过滤，状态同步到 URL（q / category） */}
      <Card className="mb-6 p-4">
        <div className="flex flex-col gap-3">
          <div className="relative">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400"
            >
              <circle cx="11" cy="11" r="7" />
              <path d="M20 20l-3.8-3.8" />
            </svg>
            <input
              type="search"
              value={draft}
              onChange={(event) => onQueryChange(event.target.value)}
              placeholder="搜索词条名称、别名或内容，例如：夏普、最大回撤、年化…"
              aria-label="搜索知识词条"
              className="w-full rounded-lg border border-slate-300 py-2 pl-9 pr-9 text-sm text-slate-800 placeholder:text-slate-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/30"
            />
            {draft && (
              <button
                type="button"
                aria-label="清空搜索"
                onClick={() => onQueryChange("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-4 w-4">
                  <path d="M6 6l12 12M18 6L6 18" />
                </svg>
              </button>
            )}
          </div>

          <div className="flex flex-wrap gap-1.5" role="group" aria-label="按分类过滤">
            <button
              type="button"
              onClick={() => onCategoryChange("all")}
              className={`rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${
                activeCategory === "all"
                  ? "bg-slate-900 text-white"
                  : "bg-slate-100 text-slate-600 hover:bg-slate-200"
              }`}
            >
              全部（{KNOWLEDGE_ENTRIES.length}）
            </button>
            {ALL_CATEGORIES.map((category) => {
              const count = KNOWLEDGE_ENTRIES.filter((entry) => entry.category === category).length;
              const active = activeCategory === category;
              return (
                <button
                  key={category}
                  type="button"
                  onClick={() => onCategoryChange(category)}
                  className={`rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${
                    active
                      ? "bg-slate-900 text-white"
                      : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                  }`}
                >
                  {CATEGORY_LABELS[category]}（{count}）
                </button>
              );
            })}
          </div>
        </div>
      </Card>

      {results.length === 0 ? (
        <Card>
          <EmptyState
            title="没有找到匹配的词条"
            hint="试试更换关键词，或清空分类过滤。"
            action={
              <button
                type="button"
                onClick={() => {
                  setDraft("");
                  syncUrl("", "all");
                }}
                className="rounded-lg border border-slate-300 px-3.5 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
              >
                清空搜索与过滤
              </button>
            }
          />
        </Card>
      ) : (
        <div className="space-y-8">
          {groups.map((group) => (
            <section key={group.category} id={`category-${group.category}`}>
              <div className="mb-3 flex items-baseline gap-2">
                <h2 className="text-base font-semibold text-slate-900">{group.label}</h2>
                <span className="text-xs text-slate-400">{group.entries.length} 个词条</span>
                <a
                  href={`#category-${group.category}`}
                  aria-label={`锚点链接：${group.label}`}
                  className="text-xs text-slate-300 opacity-0 transition-opacity hover:text-blue-600 focus-visible:opacity-100 [section:hover>&]:opacity-100"
                >
                  #
                </a>
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {group.entries.map((entry) => (
                  <EntryCard key={entry.slug} entry={entry} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}

      <p className="mt-8 text-xs leading-relaxed text-slate-400">
        词条内容解释本系统的指标定义与计算口径，仅供研究参考，不构成投资建议。
      </p>
    </div>
  );
}

export default function KnowledgePage() {
  return (
    <Suspense
      fallback={
        <Card>
          <Spinner label="正在加载知识中心…" />
        </Card>
      }
    >
      <KnowledgePageContent />
    </Suspense>
  );
}
