"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { NewsScope } from "@/lib/types";
import { fmtDate, normalizeNews, type NewsItemView } from "@/lib/normalize";
import { Card, EmptyState, ErrorState, PageHeader, Spinner } from "@/components/ui";

const TABS: { key: NewsScope; label: string; desc: string }[] = [
  { key: "related", label: "持仓相关", desc: "与当前持仓基金相关的资讯" },
  { key: "market", label: "市场要闻", desc: "宏观与市场层面的每日要闻" },
];

const API_DOWN_HINT =
  "资讯接口暂不可用。该功能依赖后端 GET /api/news?scope=related|market，当前后端尚未上线该模块。";

function sentimentBadge(sentiment: string | null): { text: string; cls: string } | null {
  if (!sentiment) return null;
  const s = sentiment.toLowerCase();
  if (s === "positive" || s === "bullish" || sentiment === "利好")
    return { text: "利好", cls: "bg-rose-50 text-rose-700" };
  if (s === "negative" || s === "bearish" || sentiment === "利空")
    return { text: "利空", cls: "bg-emerald-50 text-emerald-700" };
  if (s === "neutral" || sentiment === "中性") return { text: "中性", cls: "bg-slate-100 text-slate-600" };
  return { text: sentiment, cls: "bg-slate-100 text-slate-600" };
}

function NewsCard({ item }: { item: NewsItemView }) {
  const badge = sentimentBadge(item.sentiment);
  const body = (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm transition-colors hover:border-slate-400 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 className="min-w-0 flex-1 text-sm font-semibold leading-snug text-slate-900">
          {item.title}
        </h3>
        {badge && (
          <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${badge.cls}`}>
            {badge.text}
          </span>
        )}
      </div>
      {item.summary && (
        <p className="mt-2 line-clamp-3 text-sm leading-relaxed text-slate-600">{item.summary}</p>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-slate-400">
        {item.source && <span>{item.source}</span>}
        {item.publishedAt && <span>{fmtDate(item.publishedAt)}</span>}
        {item.tags.map((tag) => (
          <span key={tag} className="rounded-md bg-slate-100 px-1.5 py-0.5 text-slate-500">
            {tag}
          </span>
        ))}
      </div>
    </div>
  );
  if (item.url) {
    return (
      <a href={item.url} target="_blank" rel="noopener noreferrer" className="block">
        {body}
      </a>
    );
  }
  return body;
}

export default function NewsPage() {
  const [scope, setScope] = useState<NewsScope>("related");
  const [items, setItems] = useState<NewsItemView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (target: NewsScope) => {
    setLoading(true);
    setError(null);
    try {
      const raw = await api.news(target);
      setItems(normalizeNews(raw));
    } catch (e) {
      setItems([]);
      setError(
        e instanceof ApiError ? `${e.message}。${API_DOWN_HINT}` : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(scope);
  }, [scope, load]);

  const activeTab = TABS.find((t) => t.key === scope);

  return (
    <>
      <PageHeader title="每日资讯" description="聚合持仓相关与市场要闻，辅助了解持仓环境" />

      {/* 双 Tab */}
      <div className="mb-5 inline-flex rounded-xl bg-slate-200/70 p-1">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => setScope(tab.key)}
            className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
              scope === tab.key
                ? "bg-white text-slate-900 shadow-sm"
                : "text-slate-500 hover:text-slate-800"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {activeTab && <p className="-mt-3 mb-4 text-xs text-slate-400">{activeTab.desc}</p>}

      {loading ? (
        <Card>
          <Spinner label="正在加载资讯…" />
        </Card>
      ) : error ? (
        <Card>
          <ErrorState message={error} onRetry={() => load(scope)} />
        </Card>
      ) : items.length === 0 ? (
        <Card>
          <EmptyState
            title="暂无资讯"
            hint={`GET /api/news?scope=${scope} 返回为空。`}
            action={
              <button
                type="button"
                onClick={() => load(scope)}
                className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
              >
                刷新
              </button>
            }
          />
        </Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {items.map((item) => (
            <NewsCard key={item.key} item={item} />
          ))}
        </div>
      )}

      <p className="mt-6 text-xs text-slate-400">
        资讯内容来自第三方公开渠道，仅供参考，不构成投资建议。
      </p>
    </>
  );
}
