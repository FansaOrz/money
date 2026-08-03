"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, ApiError, invalidateApiCache, peekApiCache } from "@/lib/api";
import type {
  BacktestV2Result,
  ValidationResponse,
} from "@/lib/types";
import {
  fmtDate,
  fmtInt,
  fmtMoney,
  fmtPercent,
  normalizeBacktestV2,
  normalizeCatalogStats,
  normalizeDiscoveryFactors,
  normalizeDiscoverySignals,
  normalizeDualMomentum,
  normalizePoolDetail,
  normalizePools,
  normalizeValidation,
  signClass,
  toNumber,
  type BacktestV2CurvePointView,
  type BacktestV2View,
  type DiscoveryBreakdownItem,
  type DiscoveryCatalogStatsView,
  type DiscoveryFactorView,
  type DiscoveryFactorsView,
  type DiscoveryDualMomentumView,
  type DiscoveryPoolCoverageView,
  type DiscoveryPoolDetailView,
  type DiscoveryPoolView,
  type DiscoverySignalsView,
  type ValidationView,
} from "@/lib/normalize";
import { Card, EmptyState, ErrorState, PageHeader, Spinner } from "@/components/ui";
import { FundLink } from "@/components/FundLink";
import { MetricLabel } from "@/components/KnowledgeLink";
import {
  addToWatchlist,
  getWatchlist,
  removeFromWatchlist,
} from "@/lib/watchlist";

/* ==================== 常量 ==================== */

const CATALOG_API_HINT = "目录数据暂不可用，请检查后端服务或稍后重试。";
const POOLS_API_HINT = "候选池暂不可用，请先同步目录后重试。";

function isNotFound(e: unknown): boolean {
  return e instanceof ApiError && e.status === 404;
}

function errorMessage(e: unknown, hint404: string): string {
  if (e instanceof ApiError) {
    return e.status === 404 ? hint404 : e.message;
  }
  return "网络请求失败，请确认后端服务已启动";
}

/* ==================== 通用小组件 ==================== */

function fmtNumberOr(v: unknown, digits = 2): string {
  const n = toNumber(v);
  if (n === null) return "—";
  return n.toFixed(digits);
}

function simpleFundAction(fund: DiscoveryFactorView): { label: string; tone: string } {
  if ((fund.return1m ?? 0) >= 0.10) {
    return { label: "持有，别追高", tone: "bg-blue-50 text-blue-700" };
  }
  if (
    (fund.momentum121 ?? 0) > 0 &&
    (fund.return3m ?? 0) > 0 &&
    (fund.sharpe ?? 0) >= 0.5 &&
    (fund.maxDrawdown ?? -1) > -0.25
  ) {
    return { label: "可考虑加仓", tone: "bg-rose-50 text-rose-700" };
  }
  if ((fund.momentum121 ?? 0) < 0 && (fund.return3m ?? 0) < 0) {
    return { label: "建议减仓", tone: "bg-emerald-50 text-emerald-700" };
  }
  return { label: "暂时观望", tone: "bg-slate-100 text-slate-600" };
}

function MiniStat({
  label,
  term,
  value,
  valueClass = "text-slate-800",
  sub,
}: {
  label: string;
  /** 知识库词条 slug，提供时 label 可点击查看解释 */
  term?: string;
  value: string;
  valueClass?: string;
  sub?: string;
}) {
  return (
    <div className="rounded-lg bg-slate-50 px-3 py-2.5">
      <p className="text-xs text-slate-500">
        <MetricLabel term={term}>{label}</MetricLabel>
      </p>
      <p className={`mt-0.5 text-sm font-semibold tabular-nums ${valueClass}`}>{value}</p>
      {sub && <p className="mt-0.5 text-[11px] leading-snug text-slate-400">{sub}</p>}
    </div>
  );
}

function WarningsBlock({ warnings }: { warnings: string[] }) {
  const [expanded, setExpanded] = useState(false);
  if (warnings.length === 0) return null;
  const shown = expanded ? warnings : warnings.slice(0, 3);
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs text-amber-800">
      <p className="mb-1 font-medium">数据提示 {warnings.length} 条</p>
      {shown.map((w, i) => (
        <p key={i}>{w}</p>
      ))}
      {warnings.length > 3 && (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="mt-1.5 font-medium underline underline-offset-2"
        >
          {expanded ? "收起详情" : `查看全部 ${warnings.length} 条`}
        </button>
      )}
    </div>
  );
}

function SectionCard({
  title,
  description,
  action,
  children,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <Card className="px-4 py-5 sm:px-6">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-slate-900">{title}</h2>
          {description && <p className="mt-0.5 text-xs text-slate-500">{description}</p>}
        </div>
        {action}
      </div>
      {children}
    </Card>
  );
}

/** 分类分布条（类型/市场统计） */
function BreakdownBars({ items, total }: { items: DiscoveryBreakdownItem[]; total: number | null }) {
  if (items.length === 0) {
    return <p className="text-xs text-slate-400">暂无分类统计数据。</p>;
  }
  const max = Math.max(...items.map((it) => it.count ?? 0), 1);
  return (
    <ul className="space-y-2">
      {items.slice(0, 10).map((it) => {
        const share = total !== null && total > 0 && it.count !== null ? it.count / total : null;
        return (
          <li key={it.key}>
            <div className="flex items-baseline justify-between gap-2 text-xs">
              <span className="min-w-0 truncate text-slate-600" title={it.label}>
                {it.label}
              </span>
              <span className="shrink-0 tabular-nums text-slate-500">
                {it.count === null ? "—" : it.count.toLocaleString("zh-CN")}
                {share !== null && (
                  <span className="ml-1 text-slate-400">({(share * 100).toFixed(1)}%)</span>
                )}
              </span>
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-full rounded-full bg-slate-700"
                style={{ width: `${Math.max(2, ((it.count ?? 0) / max) * 100)}%` }}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/** 历史覆盖进度条 */
function CoverageProgress({ coverage }: { coverage: DiscoveryPoolCoverageView }) {
  const pct = coverage.ratio === null ? null : Math.round(coverage.ratio * 1000) / 10;
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs text-slate-500">
        <span>
          已覆盖 {coverage.coveredCount ?? "—"} / {coverage.memberCount ?? "—"} 只
        </span>
        <span className="tabular-nums font-semibold text-slate-800">
          {pct === null ? "—" : `${pct.toFixed(1)}%`}
        </span>
      </div>
      <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full rounded-full ${pct !== null && pct >= 90 ? "bg-emerald-500" : "bg-blue-600"}`}
          style={{ width: `${pct ?? 0}%` }}
        />
      </div>
      {(coverage.earliestNavDate || coverage.latestNavDate) && (
        <p className="mt-1.5 text-[11px] text-slate-400">
          净值区间 {fmtDate(coverage.earliestNavDate)} ~ {fmtDate(coverage.latestNavDate)}
        </p>
      )}
    </div>
  );
}

/* ==================== 1. 目录统计区块 ==================== */

function CatalogStatsSection() {
  const [stats, setStats] = useState<DiscoveryCatalogStatsView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncMessage, setSyncMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const raw = await api.discoveryCatalogStats();
      setStats(normalizeCatalogStats(raw));
    } catch (e) {
      setStats(null);
      setError(errorMessage(e, CATALOG_API_HINT));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const sync = useCallback(async () => {
    setSyncing(true);
    setSyncMessage(null);
    try {
      const raw = await api.discoveryCatalogSync();
      const ok = raw.ok !== false && raw.success !== false;
      setSyncMessage(
        raw.message ??
          (ok
            ? `同步完成：更新 ${fmtInt(raw.updated)} / 共 ${fmtInt(raw.total)} 只`
            : "同步已提交，结果未知")
      );
      await load();
    } catch (e) {
      setSyncMessage(errorMessage(e, "目录同步接口暂不可用（POST /api/discovery/catalog/sync）。"));
    } finally {
      setSyncing(false);
    }
  }, [load]);

  return (
    <SectionCard
      title="全市场目录"
      description="基金总数与按类型 / 市场的分布统计"
      action={
        <button
          type="button"
          onClick={sync}
          disabled={syncing}
          className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {syncing ? "同步中…" : "同步目录"}
        </button>
      }
    >
      {loading ? (
        <Spinner label="正在加载目录统计…" />
      ) : error ? (
        <ErrorState message={error} onRetry={load} />
      ) : !stats ? (
        <EmptyState title="目录统计为空" hint={CATALOG_API_HINT} />
      ) : (
        <div className="space-y-4">
          {syncMessage && (
            <p className="rounded-lg bg-slate-100 px-3 py-2 text-xs text-slate-600">{syncMessage}</p>
          )}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <MiniStat label="目录基金总数" value={fmtInt(stats.total)} sub="全市场可发现基金" />
            <MiniStat
              label="类型分类数"
              value={stats.byType.length > 0 ? String(stats.byType.length) : "—"}
            />
            <MiniStat
              label="市场分类数"
              value={stats.byMarket.length > 0 ? String(stats.byMarket.length) : "—"}
            />
            <MiniStat label="统计更新于" value={stats.updatedAt ? fmtDate(stats.updatedAt) : "—"} />
          </div>
          <div className="grid gap-5 sm:grid-cols-2">
            <div>
              <h3 className="mb-2 text-xs font-semibold text-slate-600">按基金类型</h3>
              <BreakdownBars items={stats.byType} total={stats.total} />
            </div>
            <div>
              <h3 className="mb-2 text-xs font-semibold text-slate-600">按市场</h3>
              <BreakdownBars items={stats.byMarket} total={stats.total} />
            </div>
          </div>
        </div>
      )}
    </SectionCard>
  );
}

/* ==================== 2. 候选池构建配置 ==================== */

interface PoolBuildConfig {
  name: string;
  minHistoryDays: number | null;
  maxSize: number | null;
  fundTypes: string[];
}

function PoolBuildSection({ onBuilt }: { onBuilt: (poolId: string) => void }) {
  const [name, setName] = useState("全市场核心基金池");
  const [maxSize, setMaxSize] = useState("800");
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resultMessage, setResultMessage] = useState<string | null>(null);

  const maxSizeNum = useMemo(() => {
    const n = Number.parseInt(maxSize, 10);
    return Number.isFinite(n) && n >= 500 && n <= 1000 ? n : null;
  }, [maxSize]);

  const build = useCallback(async () => {
    if (maxSizeNum === null) return;
    setBuilding(true);
    setError(null);
    setResultMessage(null);
    try {
      const raw = await api.discoveryPoolBuild({
        name: name.trim() || null,
        max_size: maxSizeNum,
      });
      const poolId =
        raw.pool_id ?? raw.id ?? (raw.pool ? (raw.pool.id ?? raw.pool.pool_id) : null) ??
        (raw.detail ? (raw.detail.id ?? raw.detail.pool_id) : null);
      const memberCount = toNumber(raw.member_count ?? raw.candidate_count);
      setResultMessage(
        raw.message ??
          `构建完成：入池 ${memberCount ?? "—"} 只${raw.excluded_count != null ? `，剔除 ${fmtInt(raw.excluded_count)} 只` : ""}`
      );
      if (poolId !== null && poolId !== undefined && poolId !== "") {
        onBuilt(String(poolId));
      }
    } catch (e) {
      setError(errorMessage(e, "候选池构建接口暂不可用（POST /api/discovery/pools/build）。"));
    } finally {
      setBuilding(false);
    }
  }, [maxSizeNum, name, onBuilt]);

  return (
    <SectionCard
      title="构建候选池"
      description="从最新活跃目录过滤、家族去重并按市场分层，历史净值由后台分批补齐"
    >
      <div className="grid items-end gap-4 sm:grid-cols-[1fr_180px_auto]">
        <div>
          <label htmlFor="pb-name" className="mb-1.5 block text-xs font-medium text-slate-500">
            候选池名称
          </label>
          <input
            id="pb-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-slate-500 focus:outline-none"
          />
        </div>
        <div>
          <label htmlFor="pb-maxsize" className="mb-1.5 block text-xs font-medium text-slate-500">
            池容量（500～1000）
          </label>
          <input
            id="pb-maxsize"
            type="number"
            min={500}
            max={1000}
            inputMode="numeric"
            value={maxSize}
            onChange={(e) => setMaxSize(e.target.value)}
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
          />
          {maxSizeNum === null && <p className="mt-1 text-xs text-rose-600">请输入 500～1000 的整数</p>}
        </div>
        <button
          type="button"
          onClick={build}
          disabled={building || maxSizeNum === null}
          className="rounded-lg bg-slate-900 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {building ? "构建中…" : "构建候选池"}
        </button>
      </div>
      {error && <p className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">{error}</p>}
      {resultMessage && (
        <p className="mt-3 rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-700">{resultMessage}</p>
      )}
    </SectionCard>
  );
}

/* ==================== 3. 池详情：成员与历史覆盖进度 ==================== */

function PoolDetailSection({
  detail,
  refreshing,
  onRefresh,
}: {
  detail: DiscoveryPoolDetailView;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const members = showAll ? detail.members : detail.members.slice(0, 20);
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MiniStat label="池内成员" value={fmtInt(detail.memberCount)} />
        <MiniStat
          label="历史已覆盖"
          value={detail.coverage.coveredCount === null ? "—" : fmtInt(detail.coverage.coveredCount)}
          sub="净值历史达到要求的基金数"
        />
        <MiniStat
          label="平均覆盖率"
          value={
            detail.coverage.ratio === null ? "—" : `${(detail.coverage.ratio * 100).toFixed(1)}%`
          }
        />
        <MiniStat label="最新净值日期" value={fmtDate(detail.coverage.latestNavDate)} />
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-[260px] flex-1">
          <CoverageProgress coverage={detail.coverage} />
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={refreshing}
          className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          {refreshing ? "刷新中…" : "刷新历史覆盖"}
        </button>
      </div>
      {detail.members.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-100">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="bg-slate-50 text-left text-xs text-slate-500">
                <th className="px-4 py-2.5 font-medium">代码</th>
                <th className="px-4 py-2.5 font-medium">名称</th>
                <th className="px-4 py-2.5 font-medium">类型</th>
                <th className="px-4 py-2.5 font-medium">市场</th>
                <th className="px-4 py-2.5 text-right font-medium">净值条数</th>
                <th className="px-4 py-2.5 font-medium">净值区间</th>
                <th className="px-4 py-2.5 text-right font-medium">覆盖率</th>
              </tr>
            </thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                  <td className="px-4 py-2.5 tabular-nums text-slate-600">{m.code}</td>
                  <td className="max-w-[220px] truncate px-4 py-2.5" title={m.name}>
                    <FundLink code={m.code} name={m.name} />
                  </td>
                  <td className="px-4 py-2.5 text-xs text-slate-500">{m.fundType}</td>
                  <td className="px-4 py-2.5 text-xs text-slate-500">{m.market}</td>
                  <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">
                    {fmtInt(m.navCount)}
                  </td>
                  <td className="px-4 py-2.5 text-xs tabular-nums text-slate-500">
                    {fmtDate(m.firstNavDate)} ~ {fmtDate(m.latestNavDate)}
                  </td>
                  <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">
                    <span
                      className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                        m.navReady
                          ? "bg-emerald-50 text-emerald-700"
                          : "bg-amber-50 text-amber-700"
                      }`}
                    >
                      {m.navReady ? "研究就绪" : "等待回填"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {detail.members.length > 20 && (
            <button
              type="button"
              onClick={() => setShowAll((v) => !v)}
              className="w-full border-t border-slate-100 py-2 text-xs font-medium text-slate-500 hover:bg-slate-50"
            >
              {showAll ? "收起" : `展开全部 ${detail.members.length} 只成员`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/* ==================== 4. 因子榜 ==================== */

type FactorSortKey =
  | "momentum121"
  | "quantile"
  | "return1m"
  | "return3m"
  | "return1y"
  | "return3y"
  | "annualVolatility"
  | "maxDrawdown"
  | "sharpe"
  | "sortino"
  | "calmar"
  | "cvar95"
  | "sampleCount";

/** 排序选项：asc=true 表示数值越小越好（升序靠前），其余按降序（越大越好） */
const FACTOR_SORT_OPTIONS: { key: FactorSortKey; label: string; asc: boolean }[] = [
  { key: "momentum121", label: "12-1 动量", asc: false },
  { key: "quantile", label: "同类动量分位", asc: false },
  { key: "return1m", label: "近 1 月收益", asc: false },
  { key: "return3m", label: "近 3 月收益", asc: false },
  { key: "return1y", label: "近 1 年收益", asc: false },
  { key: "return3y", label: "近 3 年收益", asc: false },
  { key: "sharpe", label: "夏普", asc: false },
  { key: "sortino", label: "索提诺", asc: false },
  { key: "calmar", label: "Calmar", asc: false },
  { key: "cvar95", label: "CVaR95", asc: false },
  { key: "annualVolatility", label: "年化波动", asc: true },
  { key: "maxDrawdown", label: "最大回撤", asc: true },
  { key: "sampleCount", label: "样本数", asc: false },
];

function FactorsSection({
  factors,
  heldSet,
  watchlist,
  onToggleWatch,
}: {
  factors: DiscoveryFactorsView;
  heldSet: Set<string>;
  watchlist: Set<string>;
  onToggleWatch: (code: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<FactorSortKey>("momentum121");
  const [watchOnly, setWatchOnly] = useState(false);
  const [limit, setLimit] = useState(50);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list = factors.items;
    if (watchOnly) list = list.filter((f) => watchlist.has(f.code));
    if (q) {
      list = list.filter(
        (f) =>
          f.code.toLowerCase().includes(q) ||
          f.name.toLowerCase().includes(q) ||
          f.fundType.toLowerCase().includes(q) ||
          f.market.toLowerCase().includes(q)
      );
    }
    const asc = FACTOR_SORT_OPTIONS.find((o) => o.key === sortKey)?.asc ?? false;
    const dir = asc ? 1 : -1; // 波动/回撤按越小越好升序，其余按越大越好降序
    const sorted = [...list].sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      if (av === null && bv === null) return 0;
      if (av === null) return 1;
      if (bv === null) return -1;
      return (av - bv) * dir;
    });
    return sorted;
  }, [factors.items, query, sortKey, watchOnly, watchlist]);

  const shown = filtered.slice(0, limit);

  return (
    <div className="space-y-4">
      <WarningsBlock warnings={factors.warnings} />
      {/* 筛选 / 搜索工具条 */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative min-w-[220px] flex-1">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400"
          >
            <circle cx="11" cy="11" r="7" />
            <path d="M20 20l-3.8-3.8" />
          </svg>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索代码 / 名称 / 类型 / 市场"
            className="w-full rounded-lg border border-slate-300 bg-white py-2 pl-9 pr-3 text-sm text-slate-800 focus:border-slate-500 focus:outline-none"
          />
        </div>
        <select
          value={sortKey}
          onChange={(e) => setSortKey(e.target.value as FactorSortKey)}
          className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 focus:border-slate-500 focus:outline-none"
          aria-label="排序字段"
        >
          {FACTOR_SORT_OPTIONS.map((o) => (
            <option key={o.key} value={o.key}>
              按{o.label}排序{o.asc ? "（小→大）" : "（大→小）"}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={watchOnly}
            onChange={(e) => setWatchOnly(e.target.checked)}
            className="h-4 w-4 rounded border-slate-300"
          />
          只看自选（{watchlist.size}）
        </label>
        <span className="text-xs tabular-nums text-slate-400">
          {filtered.length} / {factors.items.length} 只
          {factors.asOf && ` · as_of ${fmtDate(factors.asOf)}`}
        </span>
      </div>

      {shown.length === 0 ? (
        <EmptyState
          title="没有匹配的基金"
          hint={watchOnly ? "自选列表为空或全部不匹配，尝试取消「只看自选」。" : "调整搜索关键词试试。"}
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-100">
          <table className="w-full min-w-[1180px] text-sm">
            <thead>
              <tr className="bg-slate-50 text-left text-xs text-slate-500">
                <th className="px-3 py-2.5 font-medium">#</th>
                <th className="px-3 py-2.5 font-medium">代码 / 名称</th>
                <th className="px-3 py-2.5 font-medium">市场</th>
                <th className="px-3 py-2.5 font-medium">直白建议</th>
                <th className="px-3 py-2.5 text-right font-medium">
                  <MetricLabel term="momentum-12-1">12-1 动量</MetricLabel>
                </th>
                <th className="px-3 py-2.5 text-right font-medium">
                  <MetricLabel term="quantile">同类分位</MetricLabel>
                </th>
                <th className="px-3 py-2.5 text-right font-medium">
                  <MetricLabel term="total-return">近 1 月</MetricLabel>
                </th>
                <th className="px-3 py-2.5 text-right font-medium">
                  <MetricLabel term="total-return">近 3 月</MetricLabel>
                </th>
                <th className="px-3 py-2.5 text-right font-medium">
                  <MetricLabel term="total-return">近 1 年</MetricLabel>
                </th>
                <th className="px-3 py-2.5 text-right font-medium">
                  <MetricLabel term="annual-volatility">年化波动</MetricLabel>
                </th>
                <th className="px-3 py-2.5 text-right font-medium">
                  <MetricLabel term="max-drawdown">最大回撤</MetricLabel>
                </th>
                <th className="px-3 py-2.5 text-right font-medium">
                  <MetricLabel term="sharpe-ratio">夏普</MetricLabel>
                </th>
                <th className="px-3 py-2.5 text-right font-medium">
                  <MetricLabel term="sortino-ratio">索提诺</MetricLabel>
                </th>
                <th className="px-3 py-2.5 text-right font-medium">
                  <MetricLabel term="calmar-ratio">Calmar</MetricLabel>
                </th>
                <th className="px-3 py-2.5 text-right font-medium">样本数</th>
                <th className="px-3 py-2.5 text-center font-medium">自选</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((f, i) => {
                const held = heldSet.has(f.code);
                const watched = watchlist.has(f.code);
                const action = simpleFundAction(f);
                return (
                  <tr key={f.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                    <td className="px-3 py-2.5 tabular-nums text-slate-400">{f.rank ?? i + 1}</td>
                    <td className="max-w-[240px] px-3 py-2.5">
                      <div className="flex items-center gap-1.5">
                        <span className="tabular-nums text-slate-600">{f.code}</span>
                        {held && (
                          <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-700">
                            已持有
                          </span>
                        )}
                      </div>
                      <FundLink
                        code={f.code}
                        name={f.name}
                        className="block truncate text-xs font-medium text-slate-500 hover:text-blue-700 hover:underline"
                        title={f.name}
                      />
                    </td>
                    <td className="px-3 py-2.5 text-xs text-slate-500">{f.fundType}</td>
                    <td className="px-3 py-2.5">
                      <span className={`whitespace-nowrap rounded-full px-2 py-1 text-xs font-medium ${action.tone}`}>
                        {action.label}
                      </span>
                    </td>
                    <td className={`px-3 py-2.5 text-right font-semibold tabular-nums ${signClass(f.momentum121)}`}>
                      {fmtPercent(f.momentum121)}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-slate-600">
                      {f.quantile === null ? "—" : fmtPercent(f.quantile)}
                    </td>
                    <td className={`px-3 py-2.5 text-right tabular-nums ${signClass(f.return1m)}`}>
                      {fmtPercent(f.return1m)}
                    </td>
                    <td className={`px-3 py-2.5 text-right tabular-nums ${signClass(f.return3m)}`}>
                      {fmtPercent(f.return3m)}
                    </td>
                    <td className={`px-3 py-2.5 text-right tabular-nums ${signClass(f.return1y)}`}>
                      {fmtPercent(f.return1y)}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-slate-600">
                      {fmtPercent(f.annualVolatility)}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-slate-600">
                      {fmtPercent(f.maxDrawdown)}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-slate-600">
                      {fmtNumberOr(f.sharpe)}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-slate-600">
                      {fmtNumberOr(f.sortino)}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-slate-600">
                      {fmtNumberOr(f.calmar)}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-slate-600">
                      {fmtInt(f.sampleCount)}
                    </td>
                    <td className="px-3 py-2.5 text-center">
                      <button
                        type="button"
                        onClick={() => onToggleWatch(f.code)}
                        aria-label={watched ? `取消自选 ${f.code}` : `加入自选 ${f.code}`}
                        title={watched ? "取消自选" : "加入自选（保存在本地）"}
                        className={`rounded-md p-1 transition-colors ${
                          watched
                            ? "text-amber-500 hover:text-amber-600"
                            : "text-slate-300 hover:text-amber-500"
                        }`}
                      >
                        <svg viewBox="0 0 24 24" fill={watched ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
                          <path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.5 9.7l5.9-.9z" />
                        </svg>
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {filtered.length > limit && (
            <button
              type="button"
              onClick={() => setLimit((v) => v + 100)}
              className="w-full border-t border-slate-100 py-2 text-xs font-medium text-slate-500 hover:bg-slate-50"
            >
              加载更多（已显示 {shown.length} / {filtered.length}）
            </button>
          )}
        </div>
      )}
      {factors.methodology && (
        <p className="rounded-lg bg-slate-100 px-3 py-2 text-xs leading-relaxed text-slate-500">
          方法说明：{factors.methodology}
        </p>
      )}
      <p className="text-[11px] text-slate-400">
        自选仅保存在浏览器 localStorage，不会上传；因子榜依赖 GET /api/discovery-quant/pools/{"{id}"}/factors。
      </p>
    </div>
  );
}

/* ==================== 5. 双动量 ==================== */

function DualMomentumSection({ view }: { view: DiscoveryDualMomentumView }) {
  const [top, setTop] = useState(20);
  const sorted = useMemo(
    () =>
      [...view.items].sort((a, b) => {
        if (a.relativeRank !== null && b.relativeRank !== null) return a.relativeRank - b.relativeRank;
        if (a.relativeRank !== null) return -1;
        if (b.relativeRank !== null) return 1;
        return (b.absoluteMomentum ?? Number.NEGATIVE_INFINITY) - (a.absoluteMomentum ?? Number.NEGATIVE_INFINITY);
      }),
    [view.items]
  );
  const shown = sorted.slice(0, top);
  return (
    <div className="space-y-4">
      <WarningsBlock warnings={view.warnings} />
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MiniStat label="参与排名" value={fmtInt(view.candidateCount ?? view.items.length)} />
        <MiniStat
          label="通过绝对动量"
          term="momentum-12-1"
          value={
            view.eligibleCount !== null
              ? fmtInt(view.eligibleCount)
              : String(view.items.filter((m) => m.pass).length)
          }
          sub="12-1 动量 > 0"
        />
        <MiniStat label="as_of" term="pit-as-of" value={view.asOf ? fmtDate(view.asOf) : "—"} />
        <MiniStat
          label="展示前 N"
          value={String(Math.min(top, sorted.length))}
          sub="按相对动量排名"
        />
      </div>
      {shown.length === 0 ? (
        <EmptyState title="暂无双动量排名" hint="接口已返回，但排名列表为空。" />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-100">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="bg-slate-50 text-left text-xs text-slate-500">
                <th className="px-4 py-2.5 font-medium">
                  <MetricLabel term="relative-rank">相对排名</MetricLabel>
                </th>
                <th className="px-4 py-2.5 font-medium">代码 / 名称</th>
                <th className="px-4 py-2.5 font-medium">市场</th>
                <th className="px-4 py-2.5 text-right font-medium">
                  <MetricLabel term="momentum-12-1">绝对动量（12-1）</MetricLabel>
                </th>
                <th className="px-4 py-2.5 text-right font-medium">
                  <MetricLabel term="quantile">相对分位</MetricLabel>
                </th>
                <th className="px-4 py-2.5 font-medium">
                  <MetricLabel term="momentum-12-1">绝对动量过滤</MetricLabel>
                </th>
              </tr>
            </thead>
            <tbody>
              {shown.map((m, i) => (
                <tr key={m.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                  <td className="px-4 py-2.5 tabular-nums text-slate-600">{m.relativeRank ?? i + 1}</td>
                  <td className="max-w-[240px] px-4 py-2.5">
                    <span className="tabular-nums text-slate-600">{m.code}</span>
                    <FundLink
                      code={m.code}
                      name={m.name}
                      className="ml-2 truncate text-xs font-medium text-slate-500 hover:text-blue-700 hover:underline"
                      title={m.name}
                    />
                  </td>
                  <td className="px-4 py-2.5 text-xs text-slate-500">{m.market}</td>
                  <td className={`px-4 py-2.5 text-right font-semibold tabular-nums ${signClass(m.absoluteMomentum)}`}>
                    {fmtPercent(m.absoluteMomentum)}
                  </td>
                  <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">
                    {m.percentile === null ? "—" : `${m.percentile.toFixed(0)}%`}
                  </td>
                  <td className="px-4 py-2.5">
                    {m.pass ? (
                      <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
                        通过
                      </span>
                    ) : (
                      <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-500">
                        未通过
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {sorted.length > top && (
            <button
              type="button"
              onClick={() => setTop((v) => v + 30)}
              className="w-full border-t border-slate-100 py-2 text-xs font-medium text-slate-500 hover:bg-slate-50"
            >
              加载更多（已显示 {shown.length} / {sorted.length}）
            </button>
          )}
        </div>
      )}
      {view.methodology && (
        <p className="rounded-lg bg-slate-100 px-3 py-2 text-xs leading-relaxed text-slate-500">
          方法说明：{view.methodology}
        </p>
      )}
    </div>
  );
}

/* ==================== 6. 风险指标 ==================== */

function RiskSection({ factors }: { factors: DiscoveryFactorsView }) {
  const [topN, setTopN] = useState(20);
  const rows = useMemo(
    () =>
      [...factors.items]
        .filter((f) => f.annualVolatility !== null || f.maxDrawdown !== null || f.sharpe !== null)
        .sort((a, b) => (b.sharpe ?? Number.NEGATIVE_INFINITY) - (a.sharpe ?? Number.NEGATIVE_INFINITY)),
    [factors.items]
  );
  const shown = rows.slice(0, topN);
  const summary = useMemo(() => {
    const pick = (get: (f: DiscoveryFactorView) => number | null) => {
      const vals = rows.map(get).filter((v): v is number => v !== null);
      if (vals.length === 0) return null;
      return vals.reduce((a, b) => a + b, 0) / vals.length;
    };
    return {
      avgVol: pick((f) => f.annualVolatility),
      avgMdd: pick((f) => f.maxDrawdown),
      avgSharpe: pick((f) => f.sharpe),
      avgCvar: pick((f) => f.cvar95),
    };
  }, [rows]);

  if (rows.length === 0) {
    return <EmptyState title="暂无风险指标数据" hint="因子榜响应中缺少波动率 / 回撤 / 夏普字段。" />;
  }
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MiniStat label="池内平均年化波动" term="annual-volatility" value={fmtPercent(summary.avgVol)} />
        <MiniStat label="池内平均最大回撤" term="max-drawdown" value={fmtPercent(summary.avgMdd)} />
        <MiniStat label="池内平均夏普" term="sharpe-ratio" value={fmtNumberOr(summary.avgSharpe)} />
        <MiniStat label="池内平均 CVaR95" term="cvar95" value={fmtPercent(summary.avgCvar)} />
      </div>
      <div className="overflow-x-auto rounded-lg border border-slate-100">
        <table className="w-full min-w-[960px] text-sm">
          <thead>
            <tr className="bg-slate-50 text-left text-xs text-slate-500">
              <th className="px-4 py-2.5 font-medium">代码 / 名称</th>
              <th className="px-4 py-2.5 text-right font-medium">
                <MetricLabel term="annual-volatility">年化波动率</MetricLabel>
              </th>
              <th className="px-4 py-2.5 text-right font-medium">
                <MetricLabel term="max-drawdown">最大回撤</MetricLabel>
              </th>
              <th className="px-4 py-2.5 text-right font-medium">
                <MetricLabel term="sharpe-ratio">夏普比率</MetricLabel>
              </th>
              <th className="px-4 py-2.5 text-right font-medium">
                <MetricLabel term="sortino-ratio">索提诺</MetricLabel>
              </th>
              <th className="px-4 py-2.5 text-right font-medium">
                <MetricLabel term="calmar-ratio">Calmar</MetricLabel>
              </th>
              <th className="px-4 py-2.5 text-right font-medium">
                <MetricLabel term="cvar95">CVaR95</MetricLabel>
              </th>
              <th className="px-4 py-2.5 text-right font-medium">
                <MetricLabel term="total-return">近 1 年收益</MetricLabel>
              </th>
            </tr>
          </thead>
          <tbody>
            {shown.map((f) => (
              <tr key={f.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                <td className="max-w-[260px] px-4 py-2.5">
                  <span className="tabular-nums text-slate-600">{f.code}</span>
                  <FundLink
                    code={f.code}
                    name={f.name}
                    className="ml-2 truncate text-xs font-medium text-slate-500 hover:text-blue-700 hover:underline"
                    title={f.name}
                  />
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">
                  {fmtPercent(f.annualVolatility)}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">
                  {fmtPercent(f.maxDrawdown)}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums text-slate-800">
                  {fmtNumberOr(f.sharpe)}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">
                  {fmtNumberOr(f.sortino)}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">
                  {fmtNumberOr(f.calmar)}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">
                  {fmtPercent(f.cvar95)}
                </td>
                <td className={`px-4 py-2.5 text-right tabular-nums ${signClass(f.return1y)}`}>
                  {fmtPercent(f.return1y)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length > topN && (
          <button
            type="button"
            onClick={() => setTopN((v) => v + 30)}
            className="w-full border-t border-slate-100 py-2 text-xs font-medium text-slate-500 hover:bg-slate-50"
          >
            加载更多（已显示 {shown.length} / {rows.length}）
          </button>
        )}
      </div>
      <p className="text-[11px] text-slate-400">按夏普降序；指标取自因子榜响应中的风险字段。</p>
    </div>
  );
}

/* ==================== 7. 当期入选信号 ==================== */

function SignalsSection({ view }: { view: DiscoverySignalsView }) {
  return (
    <div className="space-y-4">
      <WarningsBlock warnings={view.warnings} />
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MiniStat
          label="信号日 / 成交日"
          term="pit-as-of"
          value={`${fmtDate(view.asOf)} → ${fmtDate(view.tradeDate)}`}
        />
        <MiniStat label="候选 / 通过" value={`${view.candidateCount ?? "—"} / ${view.eligibleCount ?? "—"}`} />
        <MiniStat
          label="现金权重"
          term="cash-weight"
          value={fmtPercent(view.cashWeight)}
          sub={view.frozen ? "本期冻结沿用持仓" : "波动目标降仓部分"}
        />
        <MiniStat label="入选只数" value={String(view.selected.length)} />
      </div>
      {view.frozen && view.freezeReason && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          本期信号冻结：{view.freezeReason}
        </p>
      )}
      {view.selected.length === 0 ? (
        <EmptyState title="当期无入选基金" hint="可能全部候选未通过绝对动量过滤，或接口尚未上线。" />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-100">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="bg-slate-50 text-left text-xs text-slate-500">
                <th className="px-4 py-2.5 font-medium">代码 / 名称</th>
                <th className="px-4 py-2.5 font-medium">市场</th>
                <th className="px-4 py-2.5 text-right font-medium">
                  <MetricLabel term="hrp">目标权重</MetricLabel>
                </th>
                <th className="px-4 py-2.5 text-right font-medium">
                  <MetricLabel term="momentum-12-1">12-1 动量</MetricLabel>
                </th>
                <th className="px-4 py-2.5 text-right font-medium">
                  <MetricLabel term="relative-rank">市场内排名</MetricLabel>
                </th>
                <th className="px-4 py-2.5 font-medium">入选理由</th>
              </tr>
            </thead>
            <tbody>
              {view.selected.map((s) => (
                <tr key={s.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                  <td className="max-w-[220px] px-4 py-2.5">
                    <span className="tabular-nums text-slate-600">{s.code}</span>
                    <FundLink
                      code={s.code}
                      name={s.name}
                      className="block truncate text-xs font-medium text-slate-500 hover:text-blue-700 hover:underline"
                      title={s.name}
                    />
                  </td>
                  <td className="px-4 py-2.5 text-xs text-slate-500">{s.market}</td>
                  <td className="px-4 py-2.5 text-right font-semibold tabular-nums text-slate-800">
                    {fmtPercent(s.weight)}
                  </td>
                  <td className={`px-4 py-2.5 text-right tabular-nums ${signClass(s.momentum121)}`}>
                    {fmtPercent(s.momentum121)}
                  </td>
                  <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">
                    {s.rank ?? "—"}
                  </td>
                  <td className="max-w-[280px] px-4 py-2.5 text-xs text-slate-500">
                    {s.reasons.length > 0 ? s.reasons.join("；") : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ==================== 8. V2 回测 ==================== */

/** 策略/基准双净值曲线（轻量 SVG，与量化页同风格） */
function DualCurveChart({
  points,
  gradientId,
  endLabel,
}: {
  points: BacktestV2CurvePointView[];
  gradientId: string;
  endLabel: string;
}) {
  const chart = useMemo(() => {
    if (points.length < 2) return null;
    const values = points.flatMap((p) => (p.benchmark !== null ? [p.strategy, p.benchmark] : [p.strategy]));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || Math.max(Math.abs(max) * 0.01, 0.0001);
    const width = 560;
    const height = 170;
    const left = 30;
    const top = 16;
    const xFor = (i: number) => left + (i / (points.length - 1)) * width;
    const yFor = (v: number) => top + (1 - (v - min) / span) * height;
    const lineOf = (get: (p: BacktestV2CurvePointView) => number | null) => {
      let path = "";
      let started = false;
      points.forEach((p, i) => {
        const v = get(p);
        if (v === null) return;
        path += `${started ? "L" : "M"}${xFor(i).toFixed(1)},${yFor(v).toFixed(1)} `;
        started = true;
      });
      return path.trim();
    };
    const navLine = lineOf((p) => p.strategy);
    const benchLine = lineOf((p) => p.benchmark);
    const areaPath = `${navLine} L${xFor(points.length - 1).toFixed(1)},${top + height} L${left},${top + height} Z`;
    const yLabels = [0, 1, 2, 3].map((i) => (max - (span * i) / 3).toFixed(3));
    return { navLine, benchLine, areaPath, yLabels, width, height, left, top };
  }, [points]);

  if (!chart || points.length < 2) {
    return <EmptyState title="结果中没有可绘制的净值曲线" hint="接口已返回，但曲线数据为空或不足两个点。" />;
  }
  const latest = points[points.length - 1];
  const hasBenchmark = points.some((p) => p.benchmark !== null);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs text-slate-500">
            {endLabel}（{fmtDate(latest.date)}）
          </p>
          <p className="text-2xl font-semibold tabular-nums text-slate-900">
            {fmtMoney(latest.strategy, 4)}
          </p>
        </div>
        <div className="flex items-center gap-4 text-xs text-slate-500">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-5 bg-blue-600" /> 策略净值
          </span>
          {hasBenchmark && (
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-0.5 w-5 bg-slate-400" /> 池内等权基准
            </span>
          )}
        </div>
      </div>
      <svg viewBox="0 0 600 220" className="h-56 w-full overflow-visible sm:h-64">
        <defs>
          <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#2563eb" stopOpacity="0.22" />
            <stop offset="100%" stopColor="#2563eb" stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {[0, 1, 2, 3].map((index) => {
          const y = chart.top + index * (chart.height / 3);
          return (
            <line key={index} x1={chart.left} x2={chart.left + chart.width} y1={y} y2={y} stroke="#e2e8f0" strokeDasharray="4" />
          );
        })}
        <path d={chart.areaPath} fill={`url(#${gradientId})`} />
        {hasBenchmark && chart.benchLine && (
          <path d={chart.benchLine} fill="none" stroke="#94a3b8" strokeWidth="1.5" strokeDasharray="5 4" />
        )}
        <path d={chart.navLine} fill="none" stroke="#2563eb" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
        {chart.yLabels.map((label, i) => (
          <text key={i} x={2} y={chart.top + i * (chart.height / 3) + 3} fontSize="9" fill="#94a3b8">
            {label}
          </text>
        ))}
        <text x={chart.left} y="212" fontSize="10" fill="#64748b">
          {fmtDate(points[0].date)}
        </text>
        <text x={chart.left + chart.width - 70} y="212" fontSize="10" fill="#64748b">
          {fmtDate(latest.date)}
        </text>
      </svg>
    </div>
  );
}

function BacktestSection({ poolId }: { poolId: string }) {
  const [topN, setTopN] = useState("8");
  const [intervalMonths, setIntervalMonths] = useState("1");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestV2View | null>(null);
  const requestId = useRef(0);

  const topNNum = useMemo(() => {
    const n = Number.parseInt(topN, 10);
    return Number.isFinite(n) && n >= 1 && n <= 50 ? n : null;
  }, [topN]);
  const intervalNum = useMemo(() => {
    const n = Number.parseInt(intervalMonths, 10);
    return Number.isFinite(n) && n >= 1 && n <= 12 ? n : null;
  }, [intervalMonths]);

  const run = useCallback(async () => {
    if (topNNum === null || intervalNum === null) return;
    const id = ++requestId.current;
    setRunning(true);
    setError(null);
    try {
      const raw: BacktestV2Result = await api.discoveryPoolBacktest(poolId, {
        top_n: topNNum,
        rebalance_interval_months: intervalNum,
      });
      if (id !== requestId.current) return;
      setResult(normalizeBacktestV2(raw));
    } catch (e) {
      if (id !== requestId.current) return;
      setResult(null);
      setError(
        errorMessage(
          e,
          "候选池回测接口暂不可用，依赖后端 POST /api/discovery-quant/pools/{id}/backtest。"
        )
      );
    } finally {
      if (id === requestId.current) setRunning(false);
    }
  }, [poolId, topNNum, intervalNum]);

  const s = result?.strategy ?? null;
  const b = result?.benchmark ?? null;

  return (
    <div className="grid gap-6 lg:grid-cols-[260px_1fr]">
      <div className="space-y-4">
        <div>
          <label htmlFor="dbt-topn" className="mb-1.5 block text-xs font-medium text-slate-500">
            入选只数上限 top_n（1-50）
          </label>
          <input
            id="dbt-topn"
            type="number"
            min={1}
            max={50}
            inputMode="numeric"
            value={topN}
            onChange={(e) => setTopN(e.target.value)}
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
          />
          {topNNum === null && <p className="mt-1 text-xs text-rose-600">请输入 1-50 的整数</p>}
        </div>
        <div>
          <label htmlFor="dbt-interval" className="mb-1.5 block text-xs font-medium text-slate-500">
            调仓间隔（月，1-12）
          </label>
          <input
            id="dbt-interval"
            type="number"
            min={1}
            max={12}
            inputMode="numeric"
            value={intervalMonths}
            onChange={(e) => setIntervalMonths(e.target.value)}
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
          />
          {intervalNum === null && <p className="mt-1 text-xs text-rose-600">请输入 1-12 的整数</p>}
        </div>
        <button
          type="button"
          onClick={run}
          disabled={running || topNNum === null || intervalNum === null}
          className="w-full rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running ? "回测运行中…" : "运行 V2 回测"}
        </button>
        <p className="rounded-lg bg-slate-100 px-3 py-2 text-xs leading-relaxed text-slate-500">
          在候选池内运行月频<MetricLabel term="momentum-12-1">动量</MetricLabel> +{" "}
          <MetricLabel term="hrp">HRP 配置</MetricLabel>回测，对比
          <MetricLabel term="b0-benchmark">池内等权基准</MetricLabel>。
        </p>
      </div>
      <div className="min-w-0">
        {running ? (
          <Spinner label="正在运行候选池 V2 月频回测…" />
        ) : error ? (
          <ErrorState message={error} onRetry={run} />
        ) : !result ? (
          <EmptyState
            title="尚未运行 V2 回测"
            hint="点击「运行 V2 回测」调用 POST /api/discovery-quant/pools/{id}/backtest。"
          />
        ) : (
          <div className="space-y-5">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h3 className="text-sm font-semibold text-slate-800">
                候选池 V2 回测 · top_n {topNNum} · 每 {intervalNum} 月调仓
              </h3>
              {(result.startDate || result.endDate) && (
                <p className="text-xs text-slate-400">
                  {fmtDate(result.startDate)} ~ {fmtDate(result.endDate)}
                </p>
              )}
            </div>
            <WarningsBlock warnings={result.warnings} />
            <div className="overflow-x-auto rounded-lg border border-slate-100">
              <table className="w-full min-w-[560px] text-sm">
                <thead>
                  <tr className="bg-slate-50 text-left text-xs text-slate-500">
                    <th className="px-4 py-2.5 font-medium">指标</th>
                    <th className="px-4 py-2.5 text-right font-medium">策略</th>
                    <th className="px-4 py-2.5 text-right font-medium">基准（池内等权）</th>
                  </tr>
                </thead>
                <tbody>
                  {(
                    [
                      ["累计收益", "total-return", fmtPercent(s?.totalReturn), fmtPercent(b?.totalReturn), signClass(s?.totalReturn)],
                      ["年化收益", "annualized-return", fmtPercent(s?.annualReturn), fmtPercent(b?.annualReturn), signClass(s?.annualReturn)],
                      ["最大回撤", "max-drawdown", fmtPercent(s?.maxDrawdown), fmtPercent(b?.maxDrawdown), "text-slate-800"],
                      ["夏普比率", "sharpe-ratio", fmtNumberOr(s?.sharpe), fmtNumberOr(b?.sharpe), "text-slate-800"],
                      ["年化波动率", "annual-volatility", fmtPercent(s?.annualVolatility), fmtPercent(b?.annualVolatility), "text-slate-800"],
                      ["日胜率", "win-rate", fmtPercent(s?.winRate), fmtPercent(b?.winRate), "text-slate-800"],
                    ] as [string, string, string, string, string][]
                  ).map(([label, term, sv, bv, cls]) => (
                    <tr key={label} className="border-t border-slate-100">
                      <td className="px-4 py-2.5 text-xs text-slate-500">
                        <MetricLabel term={term}>{label}</MetricLabel>
                      </td>
                      <td className={`px-4 py-2.5 text-right font-semibold tabular-nums ${cls}`}>{sv}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">{bv}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <MiniStat
                label="超额收益（总）"
                term="excess-return"
                value={fmtPercent(result.excessReturn)}
                valueClass={signClass(result.excessReturn)}
              />
              <MiniStat label="平均每次调仓换手" term="turnover" value={fmtPercent(result.avgTurnover)} />
              <MiniStat label="累计费用" term="transaction-cost" value={fmtMoney(result.totalFees)} />
              <MiniStat
                label="调仓 / 冻结次数"
                term="freeze-rule"
                value={`${result.rebalanceCount ?? "—"} / ${result.frozenCount ?? "—"}`}
              />
            </div>
            <DualCurveChart points={result.curve} gradientId="discoveryBtFill" endLabel="期末策略净值" />
            {result.methodology && (
              <p className="rounded-lg bg-slate-100 px-3 py-2 text-xs leading-relaxed text-slate-500">
                方法说明：{result.methodology}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* ==================== 9. 统计验证 ==================== */

function ValidationSection({ poolId }: { poolId: string }) {
  const [trainWindow, setTrainWindow] = useState("120");
  const [testWindow, setTestWindow] = useState("20");
  const [topN, setTopN] = useState("8");
  const [trialCount, setTrialCount] = useState("1");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ValidationView | null>(null);
  const requestId = useRef(0);

  const trainNum = useMemo(() => {
    const n = Number.parseInt(trainWindow, 10);
    return Number.isFinite(n) && n > 0 ? n : null;
  }, [trainWindow]);
  const testNum = useMemo(() => {
    const n = Number.parseInt(testWindow, 10);
    return Number.isFinite(n) && n > 0 ? n : null;
  }, [testWindow]);
  const topNNum = useMemo(() => {
    const n = Number.parseInt(topN, 10);
    return Number.isFinite(n) && n >= 1 && n <= 50 ? n : null;
  }, [topN]);
  const trialNum = useMemo(() => {
    const n = Number.parseInt(trialCount, 10);
    return Number.isFinite(n) && n >= 1 && n <= 10000 ? n : null;
  }, [trialCount]);

  const run = useCallback(async () => {
    if (trainNum === null || testNum === null || topNNum === null || trialNum === null) return;
    const id = ++requestId.current;
    setRunning(true);
    setError(null);
    try {
      const raw: ValidationResponse = await api.discoveryPoolValidation(poolId, {
        window: {
          train_window: trainNum,
          test_window: testNum,
          step: testNum,
        },
        top_n: topNNum,
        trial_count: trialNum,
        include_costs: true,
      });
      if (id !== requestId.current) return;
      setResult(normalizeValidation(raw));
    } catch (e) {
      if (id !== requestId.current) return;
      setResult(null);
      setError(
        errorMessage(
          e,
          "候选池验证接口暂不可用，依赖后端 POST /api/discovery-quant/pools/{id}/validation。"
        )
      );
    } finally {
      if (id === requestId.current) setRunning(false);
    }
  }, [poolId, trainNum, testNum, topNNum, trialNum]);

  return (
    <div className="grid gap-6 lg:grid-cols-[260px_1fr]">
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label htmlFor="dv-train" className="mb-1.5 block text-xs font-medium text-slate-500">
              训练窗口
            </label>
            <input
              id="dv-train"
              type="number"
              min={1}
              inputMode="numeric"
              value={trainWindow}
              onChange={(e) => setTrainWindow(e.target.value)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
            />
          </div>
          <div>
            <label htmlFor="dv-test" className="mb-1.5 block text-xs font-medium text-slate-500">
              测试窗口
            </label>
            <input
              id="dv-test"
              type="number"
              min={1}
              inputMode="numeric"
              value={testWindow}
              onChange={(e) => setTestWindow(e.target.value)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
            />
          </div>
          <div>
            <label htmlFor="dv-topn" className="mb-1.5 block text-xs font-medium text-slate-500">
              每期入选 top_n
            </label>
            <input
              id="dv-topn"
              type="number"
              min={1}
              max={50}
              inputMode="numeric"
              value={topN}
              onChange={(e) => setTopN(e.target.value)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
            />
          </div>
          <div>
            <label htmlFor="dv-trials" className="mb-1.5 block text-xs font-medium text-slate-500">
              历史试验数
            </label>
            <input
              id="dv-trials"
              type="number"
              min={1}
              max={10000}
              inputMode="numeric"
              value={trialCount}
              onChange={(e) => setTrialCount(e.target.value)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
            />
          </div>
        </div>
        {(trainNum === null || testNum === null || topNNum === null || trialNum === null) && (
          <p className="text-xs text-rose-600">请检查窗口 / top_n / 试验数是否为合法正整数</p>
        )}
        <button
          type="button"
          onClick={run}
          disabled={running || trainNum === null || testNum === null || topNNum === null || trialNum === null}
          className="w-full rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running ? "验证运行中…" : "运行统计验证"}
        </button>
        <p className="rounded-lg bg-slate-100 px-3 py-2 text-xs leading-relaxed text-slate-500">
          样本外 <MetricLabel term="walk-forward">Walk-Forward 验证</MetricLabel> +{" "}
          <MetricLabel term="rank-ic">Rank IC</MetricLabel> / 五档单调性 /{" "}
          <MetricLabel term="deflated-sharpe">DSR</MetricLabel> /{" "}
          <MetricLabel term="white-reality-check">White Reality Check</MetricLabel>。
        </p>
      </div>
      <div className="min-w-0">
        {running ? (
          <Spinner label="正在运行候选池样本外统计验证（含 bootstrap，可能需数十秒）…" />
        ) : error ? (
          <ErrorState message={error} onRetry={run} />
        ) : !result ? (
          <EmptyState
            title="尚未运行统计验证"
            hint="点击「运行统计验证」调用 POST /api/discovery-quant/pools/{id}/validation。"
          />
        ) : (
          <div className="space-y-6">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h3 className="text-sm font-semibold text-slate-800">
                验证结果 · as_of {fmtDate(result.asOf)}
              </h3>
              <p className="text-xs text-slate-400">
                样本外 {fmtDate(result.startDate)} ~ {fmtDate(result.endDate)} · 样本 {result.sampleCount ?? "—"} 天 /
                样本外 {result.oosCount ?? "—"} 天
              </p>
            </div>
            <WarningsBlock warnings={result.warnings} />
            <section>
              <h4 className="mb-2 text-xs font-semibold text-slate-600">样本外风险与收益（策略 vs 基准）</h4>
              <div className="overflow-x-auto rounded-lg border border-slate-100">
                <table className="w-full min-w-[560px] text-sm">
                  <thead>
                    <tr className="bg-slate-50 text-left text-xs text-slate-500">
                      <th className="px-4 py-2.5 font-medium">指标</th>
                      <th className="px-4 py-2.5 text-right font-medium">策略</th>
                      <th className="px-4 py-2.5 text-right font-medium">基准（等权）</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(
                      [
                        ["累计收益", "total-return", fmtPercent(result.strategy.totalReturn), fmtPercent(result.benchmark.totalReturn), signClass(result.strategy.totalReturn)],
                        ["年化收益", "annualized-return", fmtPercent(result.strategy.annualReturn), fmtPercent(result.benchmark.annualReturn), signClass(result.strategy.annualReturn)],
                        ["夏普比率", "sharpe-ratio", fmtNumberOr(result.strategy.sharpe), fmtNumberOr(result.benchmark.sharpe), "text-slate-800"],
                        ["最大回撤", "max-drawdown", fmtPercent(result.strategy.maxDrawdown), fmtPercent(result.benchmark.maxDrawdown), "text-slate-800"],
                        ["CVaR95", "cvar95", fmtPercent(result.strategy.cvar95), fmtPercent(result.benchmark.cvar95), "text-slate-800"],
                        ["Calmar", "calmar-ratio", fmtNumberOr(result.strategy.calmar), fmtNumberOr(result.benchmark.calmar), "text-slate-800"],
                        ["日胜率", "win-rate", fmtPercent(result.strategy.winRate), fmtPercent(result.benchmark.winRate), "text-slate-800"],
                      ] as [string, string, string, string, string][]
                    ).map(([label, term, sv, bv, cls]) => (
                      <tr key={label} className="border-t border-slate-100">
                        <td className="px-4 py-2.5 text-xs text-slate-500">
                          <MetricLabel term={term}>{label}</MetricLabel>
                        </td>
                        <td className={`px-4 py-2.5 text-right font-semibold tabular-nums ${cls}`}>{sv}</td>
                        <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">{bv}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
                <MiniStat label="信息比率 IR" term="information-ratio" value={fmtNumberOr(result.informationRatio)} />
                <MiniStat
                  label="超额收益（总）"
                  term="excess-return"
                  value={fmtPercent(result.excessReturn)}
                  valueClass={signClass(result.excessReturn)}
                />
                <MiniStat
                  label="Rank IC 均值"
                  term="rank-ic"
                  value={fmtNumberOr(result.predictiveness.rankIcMean, 3)}
                  valueClass={signClass(result.predictiveness.rankIcMean)}
                  sub={`参与 ${result.predictiveness.rankIcCount ?? 0} 期（Spearman）`}
                />
              </div>
            </section>
            <section>
              <h4 className="mb-2 text-xs font-semibold text-slate-600">稳健性检验</h4>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <MiniStat
                  label="Deflated Sharpe（DSR）"
                  term="deflated-sharpe"
                  value={fmtNumberOr(result.robustness.deflatedSharpe, 3)}
                  sub={`试验数 ${result.robustness.trialCount ?? 1}`}
                />
                <MiniStat
                  label="WRC p 值"
                  term="white-reality-check"
                  value={
                    result.robustness.realityCheckP === null
                      ? "—"
                      : result.robustness.realityCheckP < 0.001
                        ? "<0.001"
                        : result.robustness.realityCheckP.toFixed(3)
                  }
                  valueClass={
                    result.robustness.realityCheckP !== null && result.robustness.realityCheckP < 0.05
                      ? "text-emerald-700"
                      : "text-slate-800"
                  }
                />
                <MiniStat
                  label="五档收益差 Q5−Q1"
                  term="quintile-spread"
                  value={fmtPercent(result.predictiveness.quintileSpread)}
                  valueClass={signClass(result.predictiveness.quintileSpread)}
                />
                <MiniStat
                  label="五档严格单调递增"
                  term="tier-score"
                  value={result.predictiveness.quintileMonotonic ? "是" : "否"}
                  valueClass={result.predictiveness.quintileMonotonic ? "text-emerald-700" : "text-amber-700"}
                />
              </div>
            </section>
            {result.methodology && (
              <p className="rounded-lg bg-slate-100 px-3 py-2 text-xs leading-relaxed text-slate-500">
                方法说明：{result.methodology}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* ==================== 页面主组件 ==================== */

type DiscoveryStage = "data" | "screen" | "portfolio" | "validate";

type DiscoveryTab = "factors" | "dual" | "risk" | "signals" | "backtest" | "validation";

const STAGE_TABS: { key: DiscoveryStage; label: string; description: string }[] = [
  { key: "data", label: "1. 准备数据", description: "目录、候选池与历史覆盖" },
  { key: "screen", label: "2. 筛选基金", description: "因子榜、双动量与风险" },
  { key: "portfolio", label: "3. 生成组合", description: "当期 V2 目标权重" },
  { key: "validate", label: "4. 验证策略", description: "回测与统计验证" },
];

function DiscoveryPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const stageParam = searchParams.get("stage") as DiscoveryStage | null;
  const initialStage = STAGE_TABS.some((item) => item.key === stageParam) ? stageParam! : "data";
  const cachedPools = normalizePools(peekApiCache<never>("/api/discovery/pools"));
  const requestedPoolId = searchParams.get("pool") ?? cachedPools[0]?.id ?? null;
  const cachedDetailRaw = requestedPoolId
    ? peekApiCache<never>(`/api/discovery/pools/${encodeURIComponent(requestedPoolId)}`)
    : undefined;
  const cachedFactorsRaw = requestedPoolId
    ? peekApiCache<never>(
        `/api/discovery/quant/factors?pool_id=${encodeURIComponent(requestedPoolId)}&limit=100&min_samples=120`
      )
    : undefined;
  const cachedDualRaw = requestedPoolId
    ? peekApiCache<never>(
        `/api/discovery/quant/dual-momentum?pool_id=${encodeURIComponent(requestedPoolId)}`
      )
    : undefined;
  const cachedSignalsRaw = requestedPoolId
    ? peekApiCache<never>(
        `/api/discovery/quant/signals-v2?pool_id=${encodeURIComponent(requestedPoolId)}`
      )
    : undefined;
  const cachedDetail =
    requestedPoolId && cachedDetailRaw
      ? normalizePoolDetail(cachedDetailRaw, requestedPoolId)
      : null;
  const cachedFactors = cachedFactorsRaw ? normalizeDiscoveryFactors(cachedFactorsRaw) : null;
  const cachedDual = cachedDualRaw ? normalizeDualMomentum(cachedDualRaw) : null;
  const cachedSignals = cachedSignalsRaw ? normalizeDiscoverySignals(cachedSignalsRaw) : null;
  const [stage, setStage] = useState<DiscoveryStage>(initialStage);
  // 持仓代码（「已持有」标记）
  const [heldCodes, setHeldCodes] = useState<string[]>([]);
  // 自选（localStorage）
  const [watchlist, setWatchlist] = useState<string[]>([]);
  // 候选池
  const [pools, setPools] = useState<DiscoveryPoolView[]>(cachedPools);
  const [poolsError, setPoolsError] = useState<string | null>(null);
  const [poolsLoading, setPoolsLoading] = useState(cachedPools.length === 0);
  const [selectedPoolId, setSelectedPoolId] = useState<string | null>(requestedPoolId);
  // 池详情
  const [detail, setDetail] = useState<DiscoveryPoolDetailView | null>(cachedDetail);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [coverageRefreshing, setCoverageRefreshing] = useState(false);
  // 池量化数据
  const [factors, setFactors] = useState<DiscoveryFactorsView | null>(cachedFactors);
  const [factorsError, setFactorsError] = useState<string | null>(null);
  const [dualMomentum, setDualMomentum] = useState<DiscoveryDualMomentumView | null>(cachedDual);
  const [dmError, setDmError] = useState<string | null>(null);
  const [signals, setSignals] = useState<DiscoverySignalsView | null>(cachedSignals);
  const [signalsError, setSignalsError] = useState<string | null>(null);
  const [quantLoading, setQuantLoading] = useState(false);
  // 每个大步骤内部的小任务标签
  const [tab, setTab] = useState<DiscoveryTab>(() => {
    const tabParam = searchParams.get("tab") as DiscoveryTab | null;
    if (["factors", "dual", "risk", "signals", "backtest", "validation"].includes(tabParam ?? "")) {
      return tabParam!;
    }
    if (initialStage === "portfolio") return "signals";
    if (initialStage === "validate") return "backtest";
    return "factors";
  });

  const poolRequestId = useRef(0);

  /* 初始：持仓代码 + 自选 + 池列表 */
  useEffect(() => {
    setWatchlist(
      getWatchlist()
        .filter((item) => item.kind === "fund")
        .map((item) => item.code)
    );
    let cancelled = false;
    api
      .positions()
      .then((list) => {
        if (cancelled) return;
        const codes = (Array.isArray(list) ? list : [])
          .map((p) => p.fund_code)
          .filter((c): c is string => typeof c === "string" && c.length > 0);
        setHeldCodes([...new Set(codes)]);
      })
      .catch(() => {
        /* 持仓获取失败仅影响「已持有」标记，静默 */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const loadPools = useCallback(async (preferId?: string | null, force = false) => {
    if (force) invalidateApiCache("/api/discovery/pools");
    if (pools.length === 0) setPoolsLoading(true);
    setPoolsError(null);
    try {
      const raw = await api.discoveryPools();
      const list = normalizePools(raw);
      const requestedPool = preferId ?? searchParams.get("pool");
      setPools(list);
      setSelectedPoolId((cur) => {
        if (requestedPool && list.some((p) => p.id === requestedPool)) return requestedPool;
        if (cur && list.some((p) => p.id === cur)) return cur;
        return list.length > 0 ? list[0].id : null;
      });
    } catch (e) {
      setPools([]);
      setPoolsError(errorMessage(e, POOLS_API_HINT));
    } finally {
      setPoolsLoading(false);
    }
  }, [pools.length, searchParams]);

  useEffect(() => {
    loadPools();
  }, [loadPools]);

  /* 候选池变化时始终加载上下文详情；量化数据按当前大任务懒加载。 */
  useEffect(() => {
    if (!selectedPoolId) {
      setDetail(null);
      setFactors(null);
      setDualMomentum(null);
      setSignals(null);
      return;
    }
    const id = ++poolRequestId.current;
    if (!detail || detail.id !== selectedPoolId) setDetailLoading(true);
    setDetailError(null);
    api
      .discoveryPoolDetail(selectedPoolId)
      .then((raw) => {
        if (id === poolRequestId.current) setDetail(normalizePoolDetail(raw, selectedPoolId));
      })
      .catch((error) => {
        if (id !== poolRequestId.current) return;
        setDetail(null);
        setDetailError(errorMessage(error, "候选池详情暂不可用，请稍后重试。"));
      })
      .finally(() => {
        if (id === poolRequestId.current) setDetailLoading(false);
      });
  }, [selectedPoolId]);

  useEffect(() => {
    if (!selectedPoolId || stage === "data") return;
    const id = ++poolRequestId.current;
    setQuantLoading(true);

    const requests: Promise<void>[] = [];
    if (stage === "screen" && (tab === "factors" || tab === "risk")) {
      setFactorsError(null);
      requests.push(
        api.discoveryPoolFactors(selectedPoolId).then(
          (raw) => {
            if (id === poolRequestId.current) setFactors(normalizeDiscoveryFactors(raw));
          },
          (error) => {
            if (id === poolRequestId.current) {
              setFactors(null);
              setFactorsError(errorMessage(error, "因子数据获取失败，请先检查历史覆盖。"));
            }
          }
        )
      );
    }
    if (stage === "screen" && tab === "dual") {
      setDmError(null);
      requests.push(
        api.discoveryPoolDualMomentum(selectedPoolId).then(
          (raw) => {
            if (id === poolRequestId.current) setDualMomentum(normalizeDualMomentum(raw));
          },
          (error) => {
            if (id === poolRequestId.current) {
              setDualMomentum(null);
              setDmError(errorMessage(error, "双动量数据获取失败，请先检查历史覆盖。"));
            }
          }
        )
      );
    }
    if (stage === "portfolio") {
      setSignalsError(null);
      requests.push(
        api.discoveryPoolSignals(selectedPoolId).then(
          (raw) => {
            if (id === poolRequestId.current) setSignals(normalizeDiscoverySignals(raw));
          },
          (error) => {
            if (id === poolRequestId.current) {
              setSignals(null);
              setSignalsError(errorMessage(error, "当期组合获取失败，请先完成历史净值回填。"));
            }
          }
        )
      );
    }
    void Promise.allSettled(requests).finally(() => {
      if (id === poolRequestId.current) setQuantLoading(false);
    });
  }, [selectedPoolId, stage, tab]);

  const heldSet = useMemo(() => new Set(heldCodes), [heldCodes]);
  const watchSet = useMemo(() => new Set(watchlist), [watchlist]);

  const toggleWatch = useCallback(
    (code: string) => {
      setWatchlist((current) => {
        if (current.includes(code)) {
          removeFromWatchlist("fund", code);
          return current.filter((item) => item !== code);
        }
        const name = factors?.items.find((item) => item.code === code)?.name ?? code;
        addToWatchlist("fund", code, name);
        return [...current, code];
      });
    },
    [factors]
  );

  const tabsByStage: Record<Exclude<DiscoveryStage, "data">, { key: DiscoveryTab; label: string }[]> = {
    screen: [
      { key: "factors", label: "因子榜" },
      { key: "dual", label: "双动量" },
      { key: "risk", label: "风险指标" },
    ],
    portfolio: [{ key: "signals", label: "当期入选信号" }],
    validate: [
      { key: "backtest", label: "V2 回测" },
      { key: "validation", label: "统计验证" },
    ],
  };

  const selectStage = useCallback(
    (next: DiscoveryStage) => {
      setStage(next);
      if (next === "screen" && !["factors", "dual", "risk"].includes(tab)) setTab("factors");
      if (next === "portfolio") setTab("signals");
      if (next === "validate" && !["backtest", "validation"].includes(tab)) setTab("backtest");
      const params = new URLSearchParams(searchParams.toString());
      params.set("stage", next);
      if (selectedPoolId) params.set("pool", selectedPoolId);
      router.replace(`/discovery?${params.toString()}`, { scroll: false });
    },
    [router, searchParams, selectedPoolId, tab]
  );

  const selectTab = useCallback(
    (next: DiscoveryTab) => {
      setTab(next);
      const params = new URLSearchParams(searchParams.toString());
      params.set("tab", next);
      params.set("stage", stage);
      if (selectedPoolId) params.set("pool", selectedPoolId);
      router.replace(`/discovery?${params.toString()}`, { scroll: false });
    },
    [router, searchParams, selectedPoolId, stage]
  );

  const refreshCoverage = useCallback(async () => {
    if (!selectedPoolId) return;
    setCoverageRefreshing(true);
    setDetailError(null);
    try {
      const raw = await api.discoveryPoolRefreshNav(selectedPoolId);
      setDetail(normalizePoolDetail(raw, selectedPoolId));
    } catch (error) {
      setDetailError(errorMessage(error, "历史覆盖状态刷新失败，请稍后重试。"));
    } finally {
      setCoverageRefreshing(false);
    }
  }, [selectedPoolId]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="基金发现"
        description="全市场基金目录 → 构建候选池 → 因子榜 / 双动量 / 风险指标 → V2 回测与统计验证。接口未上线时各区块独立降级展示。"
      />

      <div className="grid gap-2 rounded-2xl border border-slate-200 bg-white p-2 shadow-sm sm:grid-cols-4">
        {STAGE_TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => selectStage(item.key)}
            className={`rounded-xl px-4 py-3 text-left transition-colors ${
              stage === item.key
                ? "bg-slate-900 text-white"
                : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
            }`}
          >
            <span className="block text-sm font-semibold">{item.label}</span>
            <span className={`mt-0.5 block text-[11px] ${stage === item.key ? "text-slate-300" : "text-slate-400"}`}>
              {item.description}
            </span>
          </button>
        ))}
      </div>

      {stage === "data" && (
        <>
          <CatalogStatsSection />
          <PoolBuildSection onBuilt={(id) => loadPools(id)} />
        </>
      )}

      {/* 候选池上下文始终可见，便于四步共享同一研究范围 */}
      <SectionCard
        title="候选池"
        description="选择候选池查看成员与净值历史覆盖进度"
        action={
          <button
            type="button"
            onClick={() => loadPools(undefined, true)}
            disabled={poolsLoading}
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            刷新池列表
          </button>
        }
      >
        {poolsLoading && pools.length === 0 ? (
          <Spinner label="正在加载候选池列表…" />
        ) : poolsError ? (
          <ErrorState message={poolsError} onRetry={() => loadPools()} />
        ) : pools.length === 0 ? (
          <EmptyState
            title="暂无候选池"
            hint="使用上方「构建候选池」从全市场目录筛选生成第一个候选池。"
          />
        ) : (
          <div className="space-y-5">
            <div className="flex flex-wrap items-center gap-2">
              {pools.map((p) => (
                <button
                  key={p.key}
                  type="button"
                  onClick={() => {
                    setSelectedPoolId(p.id);
                    const params = new URLSearchParams(searchParams.toString());
                    params.set("pool", p.id);
                    params.set("stage", stage);
                    router.replace(`/discovery?${params.toString()}`, { scroll: false });
                  }}
                  className={`rounded-full border px-3.5 py-1.5 text-xs font-medium transition-colors ${
                    selectedPoolId === p.id
                      ? "border-slate-900 bg-slate-900 text-white"
                      : "border-slate-300 text-slate-600 hover:bg-slate-100"
                  }`}
                  title={p.description || p.name}
                >
                  {p.name}
                  <span className="ml-1.5 tabular-nums opacity-70">
                    {p.memberCount ?? "—"}
                  </span>
                </button>
              ))}
            </div>
            {detailLoading && !detail ? (
              <Spinner label="正在加载候选池详情…" />
            ) : detailError ? (
              <ErrorState
                message={detailError}
                onRetry={() => {
                  const cur = selectedPoolId;
                  setSelectedPoolId(null);
                  setTimeout(() => setSelectedPoolId(cur), 0);
                }}
              />
            ) : detail ? (
              <PoolDetailSection
                detail={detail}
                refreshing={coverageRefreshing}
                onRefresh={() => void refreshCoverage()}
              />
            ) : (
              <EmptyState title="候选池详情为空" hint="接口返回为空或尚未选择候选池。" />
            )}
          </div>
        )}
      </SectionCard>

      {/* 当前大步骤的小任务 */}
      {selectedPoolId && stage !== "data" && (
        <Card className="px-4 py-5 sm:px-6">
          <div className="mb-5 flex flex-wrap gap-1 border-b border-slate-100 pb-3">
            {tabsByStage[stage].map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => selectTab(t.key)}
                className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                  tab === t.key
                    ? "bg-slate-900 text-white"
                    : "text-slate-600 hover:bg-slate-100"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>

          {tab === "factors" &&
            (quantLoading && !factors && !factorsError ? (
              <Spinner label="正在加载因子榜…" />
            ) : factorsError ? (
              <ErrorState message={factorsError} />
            ) : factors ? (
              <FactorsSection
                factors={factors}
                heldSet={heldSet}
                watchlist={watchSet}
                onToggleWatch={toggleWatch}
              />
            ) : (
              <EmptyState title="暂无因子榜数据" hint="接口返回为空。" />
            ))}

          {tab === "dual" &&
            (quantLoading && !dualMomentum && !dmError ? (
              <Spinner label="正在加载双动量排名…" />
            ) : dmError ? (
              <ErrorState message={dmError} />
            ) : dualMomentum ? (
              <DualMomentumSection view={dualMomentum} />
            ) : (
              <EmptyState title="暂无双动量数据" hint="接口返回为空。" />
            ))}

          {tab === "risk" &&
            (quantLoading && !factors && !factorsError ? (
              <Spinner label="正在加载风险指标…" />
            ) : factorsError ? (
              <ErrorState message={factorsError} />
            ) : factors ? (
              <RiskSection factors={factors} />
            ) : (
              <EmptyState title="暂无风险指标数据" hint="接口返回为空。" />
            ))}

          {tab === "signals" &&
            (quantLoading && !signals && !signalsError ? (
              <Spinner label="正在加载当期入选信号…" />
            ) : signalsError ? (
              <ErrorState message={signalsError} />
            ) : signals ? (
              <SignalsSection view={signals} />
            ) : (
              <EmptyState title="暂无入选信号" hint="接口返回为空。" />
            ))}

          {tab === "backtest" && <BacktestSection poolId={selectedPoolId} />}

          {tab === "validation" && <ValidationSection poolId={selectedPoolId} />}
        </Card>
      )}
    </div>
  );
}

export default function DiscoveryPage() {
  return (
    <Suspense
      fallback={
        <Card>
          <Spinner label="正在加载基金发现任务…" />
        </Card>
      }
    >
      <DiscoveryPageContent />
    </Suspense>
  );
}
