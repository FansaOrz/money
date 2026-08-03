"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, ApiError } from "@/lib/api";
import {
  fmtDate,
  fmtPercent,
  normalizeStockDataStatus,
  normalizeStockFactors,
  normalizeStockMaster,
  normalizeStockUniverse,
  signClass,
  stockFactorLabel,
  toNumber,
  type StockDataStatusView,
  type StockFactorRowView,
  type StockFactorsView,
  type StockListItemView,
} from "@/lib/normalize";
import { Card, EmptyState, ErrorState, PageHeader, Spinner } from "@/components/ui";
import { MetricLabel } from "@/components/KnowledgeLink";
import { WatchlistButton } from "@/components/WatchlistButton";

const API_DOWN_HINT =
  "股票研究接口暂不可用。该功能依赖后端 /api/stocks/* 接口，当前后端尚未上线该模块，页面结构已就绪。";

type SortKey = "compositeScore" | "rank" | "momentum" | "value" | "quality" | "growth" | "volatility" | "size" | "pe" | "pb" | "roe" | "return20d" | "return60d";

const COLUMNS: { key: SortKey; label: string; term?: string }[] = [
  { key: "compositeScore", label: "综合分", term: "composite-score" },
  { key: "momentum", label: "动量", term: "momentum-score" },
  { key: "value", label: "价值", term: "bp" },
  { key: "quality", label: "质量", term: "roe" },
  { key: "growth", label: "成长" },
  { key: "volatility", label: "波动率", term: "annual-volatility" },
  { key: "size", label: "市值因子" },
  { key: "pe", label: "PE", term: "pe" },
  { key: "pb", label: "PB", term: "pb" },
  { key: "roe", label: "ROE", term: "roe" },
  { key: "return20d", label: "20 日", term: "total-return" },
  { key: "return60d", label: "60 日", term: "total-return" },
];

function fmtFactor(v: number | null): string {
  if (v === null) return "—";
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(2);
}

function fmtMaybePercent(v: number | null): string {
  if (v === null) return "—";
  // 收益率类因子：|v|<=1 视为小数，否则已是百分数
  return fmtPercent(v);
}

/** 数据可用性总览条 */
function DataStatusBar({ status }: { status: StockDataStatusView | null }) {
  if (!status || status.sources.length === 0) return null;
  return (
    <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 bg-white px-3.5 py-2.5 text-xs">
      <span className="font-medium text-slate-600">数据可用性</span>
      {status.sources.map((s) => (
        <span
          key={s.key}
          className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 ${
            s.available
              ? "border-emerald-200 bg-emerald-50 text-emerald-700"
              : "border-slate-200 bg-slate-50 text-slate-500"
          }`}
          title={s.availableAt ? `数据时间：${s.availableAt}` : s.message ?? undefined}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${s.available ? "bg-emerald-500" : "bg-slate-300"}`} />
          {s.label}
          {s.availableAt && <span className="tabular-nums text-[10px] opacity-75">{fmtDate(s.availableAt)}</span>}
        </span>
      ))}
      {status.asOf && <span className="ml-auto text-slate-400">截至 {fmtDate(status.asOf)}</span>}
    </div>
  );
}

export default function StockScreenerPage() {
  const [search, setSearch] = useState("");
  const [industry, setIndustry] = useState("");
  const [universe, setUniverse] = useState("");

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<StockDataStatusView | null>(null);
  const [factors, setFactors] = useState<StockFactorsView | null>(null);
  const [masterItems, setMasterItems] = useState<StockListItemView[]>([]);
  const [universeItems, setUniverseItems] = useState<StockListItemView[]>([]);
  const [universeName, setUniverseName] = useState<string | null>(null);
  const [industries, setIndustries] = useState<string[]>([]);

  const [sortKey, setSortKey] = useState<SortKey>("compositeScore");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const load = useCallback(async (params: { industry?: string; search?: string; universe?: string }) => {
    setLoading(true);
    setError(null);
    try {
      const [statusRes, factorsRes, masterRes, universeRes] = await Promise.allSettled([
        api.stockDataStatus(),
        api.stockFactors({
          industry: params.industry || undefined,
          search: params.search || undefined,
          universe: params.universe || undefined,
          limit: 500,
        }),
        api.stockMaster({
          industry: params.industry || undefined,
          search: params.search || undefined,
          limit: 500,
        }),
        api.stockUniverse(params.universe ? { universe: params.universe } : undefined),
      ]);

      if (statusRes.status === "fulfilled") {
        setStatus(normalizeStockDataStatus(statusRes.value));
      }
      if (factorsRes.status === "fulfilled") {
        setFactors(normalizeStockFactors(factorsRes.value as never));
      }
      if (masterRes.status === "fulfilled") {
        const m = normalizeStockMaster(masterRes.value as never);
        setMasterItems(m.items);
        setIndustries((prev) => (m.industries.length > 0 ? m.industries : prev));
      }
      if (universeRes.status === "fulfilled") {
        const u = normalizeStockUniverse(universeRes.value as never);
        setUniverseItems(u.items);
        setUniverseName(u.name);
        setIndustries((prev) => (u.industries.length > 0 ? u.industries : prev));
      }

      const allRejected = [statusRes, factorsRes, masterRes, universeRes].every(
        (r) => r.status === "rejected"
      );
      if (allRejected) {
        const reason = (factorsRes as PromiseRejectedResult).reason;
        throw reason instanceof Error ? reason : new Error(String(reason));
      }
    } catch (e) {
      setError(
        e instanceof ApiError
          ? `${e.message}。${e.status === 404 ? API_DOWN_HINT : ""}`
          : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load({ industry: "", search: "", universe: "" });
  }, [load]);

  const applyFilters = useCallback(() => {
    void load({ industry, search: search.trim(), universe: universe.trim() });
  }, [load, industry, search, universe]);

  /* 合并因子表 + 主数据兜底列表：有因子数据用因子表，否则展示 master/universe 列表 */
  const rows = useMemo(() => {
    const factorRows = factors?.items ?? [];
    if (factorRows.length > 0) return factorRows;
    // 兜底：master 列表拼成无因子的行
    const base = masterItems.length > 0 ? masterItems : universeItems;
    return base.map(
      (it): StockFactorRowView => ({
        key: it.key,
        code: it.code,
        name: it.name,
        industry: it.industry,
        market: it.market,
        compositeScore: null,
        rank: null,
        percentile: null,
        momentum: null,
        value: null,
        quality: null,
        growth: null,
        volatility: null,
        size: null,
        pe: null,
        pb: null,
        roe: null,
        return20d: null,
        return60d: null,
        extraFactors: [],
      })
    );
  }, [factors, masterItems, universeItems]);

  const sortedRows = useMemo(() => {
    const arr = [...rows];
    const dir = sortDir === "asc" ? 1 : -1;
    arr.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      const an = toNumber(av) ?? Number.NEGATIVE_INFINITY;
      const bn = toNumber(bv) ?? Number.NEGATIVE_INFINITY;
      return (an - bn) * dir;
    });
    return arr;
  }, [rows, sortKey, sortDir]);

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const hasFactorData = (factors?.items.length ?? 0) > 0;

  return (
    <>
      <PageHeader
        title="股票筛选"
        description="按股票宇宙 / 行业 / 关键词筛选 A 股标的，查看研究因子横截面"
      />

      {/* 筛选条件 */}
      <Card className="mb-4 px-4 py-4 sm:px-5">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <label htmlFor="sc-universe" className="mb-1.5 block text-xs font-medium text-slate-500">
              股票宇宙（universe）
            </label>
            <input
              id="sc-universe"
              type="text"
              value={universe}
              onChange={(e) => setUniverse(e.target.value)}
              placeholder="如 hs300 / zz500 / all"
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-slate-500 focus:outline-none"
            />
          </div>
          <div>
            <label htmlFor="sc-industry" className="mb-1.5 block text-xs font-medium text-slate-500">
              行业
            </label>
            <select
              id="sc-industry"
              value={industry}
              onChange={(e) => setIndustry(e.target.value)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-slate-500 focus:outline-none"
            >
              <option value="">全部行业</option>
              {industries.map((ind) => (
                <option key={ind} value={ind}>
                  {ind}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="sc-search" className="mb-1.5 block text-xs font-medium text-slate-500">
              搜索（代码 / 名称）
            </label>
            <input
              id="sc-search"
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="如 600519 / 贵州茅台"
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-slate-500 focus:outline-none"
            />
          </div>
          <div className="flex items-end">
            <button
              type="button"
              onClick={applyFilters}
              disabled={loading}
              className="w-full rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? "加载中…" : "应用筛选"}
            </button>
          </div>
        </div>
        <p className="mt-2.5 text-xs text-slate-400">
          数据源：GET /api/stocks/master · /api/stocks/universe · /api/stocks/research/factors
          {universeName ? ` · 当前宇宙：${universeName}` : ""}
        </p>
      </Card>

      <DataStatusBar status={status} />

      {loading ? (
        <Card>
          <Spinner label="正在加载股票数据…" />
        </Card>
      ) : error ? (
        <Card>
          <ErrorState message={error} onRetry={() => void load({ industry, search, universe })} />
        </Card>
      ) : sortedRows.length === 0 ? (
        <Card>
          <EmptyState
            title="暂无股票数据"
            hint="后端 /api/stocks/* 接口尚未返回数据；接口上线后此处将展示因子横截面表。"
            action={
              <button
                type="button"
                onClick={() => void load({ industry, search, universe })}
                className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
              >
                刷新
              </button>
            }
          />
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-4 sm:px-5">
            <div>
              <h2 className="text-sm font-semibold text-slate-800">
                {hasFactorData ? "因子横截面" : "股票列表（因子数据暂缺）"}
              </h2>
              <p className="mt-0.5 text-xs text-slate-400">
                共 {sortedRows.length} 只
                {factors?.asOf ? ` · 因子日期 ${fmtDate(factors.asOf)}` : ""}
                {factors?.availableAt ? ` · 数据可用时间 ${fmtDate(factors.availableAt)}` : ""}
              </p>
            </div>
            {!hasFactorData && (
              <p className="text-xs text-amber-700">因子接口未返回数据，当前展示主数据列表兜底</p>
            )}
          </div>
          {factors && factors.warnings.length > 0 && (
            <div className="mx-4 mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs text-amber-800 sm:mx-5">
              {factors.warnings.map((w, i) => (
                <p key={i}>{w}</p>
              ))}
            </div>
          )}
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1080px] text-sm">
              <thead>
                <tr className="border-t border-slate-100 bg-slate-50 text-left text-xs text-slate-500">
                  <th className="px-4 py-2.5 font-medium sm:px-5">股票</th>
                  <th className="px-4 py-2.5 font-medium">行业</th>
                  {COLUMNS.map((c) => (
                    <th
                      key={c.key}
                      className="cursor-pointer select-none px-3 py-2.5 text-right font-medium hover:text-slate-800"
                      onClick={() => toggleSort(c.key)}
                      title="点击排序"
                    >
                      <MetricLabel term={c.term}>{c.label}</MetricLabel>
                      {sortKey === c.key ? (sortDir === "desc" ? " ↓" : " ↑") : ""}
                    </th>
                  ))}
                  <th className="px-4 py-2.5 text-right font-medium sm:px-5">操作</th>
                </tr>
              </thead>
              <tbody>
                {sortedRows.map((r) => (
                  <tr key={r.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                    <td className="px-4 py-3 sm:px-5">
                      <Link
                        href={`/stocks/${encodeURIComponent(r.code)}`}
                        className="font-medium text-slate-800 hover:text-blue-700"
                      >
                        {r.name}
                      </Link>
                      <p className="text-xs text-slate-400">{r.code}</p>
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-600">{r.industry}</td>
                    <td className="px-3 py-3 text-right font-semibold tabular-nums text-slate-800">
                      {fmtFactor(r.compositeScore)}
                    </td>
                    <td className={`px-3 py-3 text-right tabular-nums ${signClass(r.momentum)}`}>
                      {fmtMaybePercent(r.momentum)}
                    </td>
                    <td className="px-3 py-3 text-right tabular-nums text-slate-600">{fmtFactor(r.value)}</td>
                    <td className="px-3 py-3 text-right tabular-nums text-slate-600">{fmtFactor(r.quality)}</td>
                    <td className="px-3 py-3 text-right tabular-nums text-slate-600">{fmtFactor(r.growth)}</td>
                    <td className="px-3 py-3 text-right tabular-nums text-slate-600">{fmtFactor(r.volatility)}</td>
                    <td className="px-3 py-3 text-right tabular-nums text-slate-600">{fmtFactor(r.size)}</td>
                    <td className="px-3 py-3 text-right tabular-nums text-slate-600">{fmtFactor(r.pe)}</td>
                    <td className="px-3 py-3 text-right tabular-nums text-slate-600">{fmtFactor(r.pb)}</td>
                    <td className="px-3 py-3 text-right tabular-nums text-slate-600">
                      {r.roe === null ? "—" : fmtPercent(r.roe)}
                    </td>
                    <td className={`px-3 py-3 text-right tabular-nums ${signClass(r.return20d)}`}>
                      {r.return20d === null ? "—" : fmtPercent(r.return20d)}
                    </td>
                    <td className={`px-3 py-3 text-right tabular-nums ${signClass(r.return60d)}`}>
                      {r.return60d === null ? "—" : fmtPercent(r.return60d)}
                    </td>
                    <td className="px-4 py-3 text-right sm:px-5">
                      <div className="flex items-center justify-end gap-2">
                        <WatchlistButton kind="stock" code={r.code} name={r.name} size="sm" />
                        <Link
                          href={`/stocks/${encodeURIComponent(r.code)}`}
                          className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50"
                        >
                          详情
                        </Link>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {sortedRows.some((r) => r.extraFactors.length > 0) && (
            <p className="border-t border-slate-100 px-4 py-3 text-xs text-slate-400 sm:px-5">
              额外因子：{stockFactorLabel("momentum")} 等列展示的是标准因子；各股更多因子明细见个股详情页。
            </p>
          )}
        </Card>
      )}
    </>
  );
}
