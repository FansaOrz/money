"use client";

import { Fragment, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { Position } from "@/lib/types";
import {
  fmtDate,
  fmtMoney,
  fmtPercent,
  fmtShares,
  normalizeFundReturnItems,
  normalizePortfolioReturns,
  normalizePositions,
  signClass,
  toNumber,
  RETURN_WINDOWS,
  type FundReturnItemView,
  type PositionView,
  type ReturnWindowView,
} from "@/lib/normalize";
import type { ReturnWindowKey } from "@/lib/types";
import { Card, EmptyState, ErrorState, PageHeader, SnapshotNotice, Spinner } from "@/components/ui";
import { FundLink } from "@/components/FundLink";
import { LazyFundHistoryChart } from "@/components/LazyFundHistoryChart";
import { WatchlistButton } from "@/components/WatchlistButton";
import { ExposurePanel } from "./ExposurePanel";

type ViewKey = "holdings" | "exposure";

const VIEW_TABS: { key: ViewKey; label: string }[] = [
  { key: "holdings", label: "持仓明细" },
  { key: "exposure", label: "穿透分析" },
];

type SortKey =
  | "name"
  | "shares"
  | "costPrice"
  | "nav"
  | "marketValue"
  | "profit"
  | "returnRate"
  | "windowReturn"
  | "windowReturnRate"
  | "weight";
type SortDirection = "asc" | "desc";

interface SortState {
  key: SortKey;
  direction: SortDirection;
}

const BASE_COLUMNS: { key: SortKey; label: string; align?: "right" }[] = [
  { key: "name", label: "基金" },
  { key: "shares", label: "份额", align: "right" },
  { key: "costPrice", label: "成本价", align: "right" },
  { key: "nav", label: "最新净值", align: "right" },
  { key: "marketValue", label: "市值（元）", align: "right" },
  { key: "profit", label: "累计收益（元）", align: "right" },
  { key: "returnRate", label: "累计收益率", align: "right" },
];

const WINDOW_AMOUNT_COLUMN: { key: SortKey; label: string; align?: "right" } = {
  key: "windowReturn",
  label: "窗口收益（元）",
  align: "right",
};
const WINDOW_RATE_COLUMN: { key: SortKey; label: string; align?: "right" } = {
  key: "windowReturnRate",
  label: "窗口收益率",
  align: "right",
};
const WEIGHT_COLUMN: { key: SortKey; label: string; align?: "right" } = {
  key: "weight",
  label: "占比",
};

/** 持仓行合并窗口收益后的视图模型 */
interface MergedRow extends PositionView {
  windowItem: FundReturnItemView | null;
  windowReturn: number | null;
  windowReturnRate: number | null;
}

/** 统一小任务标签：持仓明细 / 穿透分析（直链 /positions?view=holdings|exposure） */
function ViewTabs({ view }: { view: ViewKey }) {
  return (
    <div className="mb-5 inline-flex rounded-lg bg-slate-200/70 p-1 text-sm">
      {VIEW_TABS.map((tab) => (
        <Link
          key={tab.key}
          href={`/positions?view=${tab.key}`}
          aria-current={view === tab.key ? "page" : undefined}
          className={`rounded-md px-4 py-2 transition-colors ${
            view === tab.key
              ? "bg-white font-medium text-slate-900 shadow-sm"
              : "text-slate-600 hover:text-slate-900"
          }`}
        >
          {tab.label}
        </Link>
      ))}
    </div>
  );
}

function PositionsPageContent() {
  const searchParams = useSearchParams();
  const view: ViewKey = searchParams.get("view") === "exposure" ? "exposure" : "holdings";

  return (
    <>
      <ViewTabs view={view} />
      {view === "exposure" ? (
        <>
          <PageHeader
            title="穿透持仓"
            description="按基金当前市值 × 最新季度披露持仓比例，估算组合底层股票和行业暴露"
          />
          <ExposurePanel hideHeader />
        </>
      ) : (
        <HoldingsView />
      )}
    </>
  );
}

function HoldingsView() {
  const searchParams = useSearchParams();
  const highlightCode = searchParams.get("fund");
  const rowRefs = useRef<Map<string, HTMLTableRowElement>>(new Map());
  const didScrollRef = useRef(false);

  const [positions, setPositions] = useState<PositionView[]>([]);
  const [windowKey, setWindowKey] = useState<ReturnWindowKey>("1d");
  const [returns, setReturns] = useState<ReturnWindowView[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sort, setSort] = useState<SortState>({ key: "marketValue", direction: "desc" });
  // 展开状态按基金代码保存，排序/刷新后保持不变
  const [expandedCodes, setExpandedCodes] = useState<Set<string>>(() => new Set());

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [positionsRaw, returnsRaw] = await Promise.allSettled([
        api.positions(),
        api.portfolioReturns(),
      ]);
      if (positionsRaw.status === "fulfilled") {
        setPositions(normalizePositions(positionsRaw.value as Position[]));
      } else {
        throw positionsRaw.reason instanceof Error
          ? positionsRaw.reason
          : new Error(String(positionsRaw.reason));
      }
      // 收益接口宽松容错：失败时窗口列显示 —
      setReturns(
        returnsRaw.status === "fulfilled" ? normalizePortfolioReturns(returnsRaw.value) : null
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "网络请求失败，请确认后端服务已启动");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const activeWindow: ReturnWindowView | null = useMemo(
    () => returns?.find((w) => w.key === windowKey) ?? null,
    [returns, windowKey]
  );

  // 合并持仓与当前窗口收益 items（按基金代码匹配）
  const merged = useMemo<MergedRow[]>(() => {
    const items = activeWindow?.items ?? [];
    const byCode = new Map(items.map((it) => [it.code, it]));
    return positions.map((p) => {
      const it = byCode.get(p.code) ?? null;
      return {
        ...p,
        windowItem: it,
        windowReturn: it && it.status !== "stale" ? it.returnAmount : null,
        windowReturnRate: it && it.status !== "stale" ? it.returnRate : null,
      };
    });
  }, [positions, activeWindow]);

  // 解析 query：?fund=code 自动展开并滚动到对应基金
  useEffect(() => {
    if (!highlightCode || loading || positions.length === 0) return;
    const exists = positions.some((p) => p.code === highlightCode);
    if (!exists) return;
    setExpandedCodes((current) => {
      if (current.has(highlightCode)) return current;
      const next = new Set(current);
      next.add(highlightCode);
      return next;
    });
    if (didScrollRef.current) return;
    didScrollRef.current = true;
    // 等待展开行渲染后再滚动
    const timer = setTimeout(() => {
      rowRefs.current.get(highlightCode)?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 120);
    return () => clearTimeout(timer);
  }, [highlightCode, loading, positions]);

  const sorted = useMemo(() => {
    return [...merged].sort((a, b) => {
      const factor = sort.direction === "asc" ? 1 : -1;
      if (sort.key === "name") {
        return a.name.localeCompare(b.name, "zh-CN") * factor;
      }
      const av = toNumber(a[sort.key]);
      const bv = toNumber(b[sort.key]);
      if (av === null && bv === null) return 0;
      if (av === null) return 1;
      if (bv === null) return -1;
      return (av - bv) * factor;
    });
  }, [merged, sort]);

  const totalValue = positions.reduce((acc, p) => acc + (Number(p.marketValue) || 0), 0);
  const coveredPositions = positions.filter((position) => position.profitAvailable);
  const totalProfit = coveredPositions.reduce((acc, p) => acc + (Number(p.profit) || 0), 0);
  const coveredMarketValue = coveredPositions.reduce((acc, p) => acc + (Number(p.marketValue) || 0), 0);
  const coverageRate = totalValue > 0 ? (coveredMarketValue / totalValue) * 100 : 0;

  const toggleSort = (key: SortKey) => {
    setSort((current) => {
      if (current.key === key) {
        return { key, direction: current.direction === "asc" ? "desc" : "asc" };
      }
      return { key, direction: key === "name" ? "asc" : "desc" };
    });
  };

  const toggleExpanded = (code: string) => {
    setExpandedCodes((current) => {
      const next = new Set(current);
      if (next.has(code)) {
        next.delete(code);
      } else {
        next.add(code);
      }
      return next;
    });
  };

  const allExpanded = positions.length > 0 && positions.every((p) => expandedCodes.has(p.code));

  const toggleExpandAll = () => {
    setExpandedCodes((current) => {
      if (positions.length > 0 && positions.every((p) => current.has(p.code))) {
        return new Set();
      }
      return new Set(positions.map((p) => p.code));
    });
  };

  const windowMeta = RETURN_WINDOWS.find((w) => w.key === windowKey);
  const columns = [
    ...BASE_COLUMNS.slice(0, 5),
    {
      ...WINDOW_AMOUNT_COLUMN,
      label: `${windowMeta?.label ?? "窗口"}收益（元）`,
    },
    {
      ...WINDOW_RATE_COLUMN,
      label: `${windowMeta?.label ?? "窗口"}收益率`,
    },
    ...BASE_COLUMNS.slice(5),
    WEIGHT_COLUMN,
  ];
  const colCount = columns.length;

  return (
    <>
      <PageHeader
        title="持仓"
        description={
          positions.length > 0
            ? `共 ${positions.length} 只基金 · 总市值 ¥${fmtMoney(totalValue)} · 已覆盖持仓估算收益 ¥${fmtMoney(totalProfit)} · 成本覆盖 ${coverageRate.toFixed(1)}%`
            : "当前全部基金持仓"
        }
      />
      <SnapshotNotice />

      {loading ? (
        <Card>
          <Spinner label="正在加载持仓…" />
        </Card>
      ) : error ? (
        <Card>
          <ErrorState message={error} onRetry={load} />
        </Card>
      ) : positions.length === 0 ? (
        <Card>
          <EmptyState
            title="暂无持仓"
            hint="数据库中还没有持仓数据。"
            action={
              <Link
                href="/imports"
                className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
              >
                前往导入
              </Link>
            }
          />
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-4 py-2.5 sm:px-5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-slate-500">收益窗口</span>
              <div className="flex overflow-hidden rounded-lg border border-slate-200">
                {RETURN_WINDOWS.map((w) => (
                  <button
                    key={w.key}
                    type="button"
                    onClick={() => setWindowKey(w.key)}
                    className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                      windowKey === w.key
                        ? "bg-slate-900 text-white"
                        : "bg-white text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                    }`}
                  >
                    {w.label}
                  </button>
                ))}
              </div>
              {activeWindow && (
                <WindowHint view={activeWindow} />
              )}
            </div>
            <button
              type="button"
              onClick={toggleExpandAll}
              className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50 hover:text-slate-900"
            >
              {allExpanded ? "收起全部" : "展开全部"}
            </button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[960px] text-sm">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50 text-left text-xs text-slate-500">
                  {columns.map((column) => (
                    <th
                      key={column.key}
                      className={`px-4 py-2.5 font-medium ${column.align === "right" ? "text-right" : ""} ${column.key === "name" || column.key === "weight" ? "sm:px-5" : ""}`}
                    >
                      <button
                        type="button"
                        onClick={() => toggleSort(column.key)}
                        className="inline-flex items-center gap-1 hover:text-slate-900"
                      >
                        <span>{column.label}</span>
                        <SortIcon active={sort.key === column.key} direction={sort.direction} />
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sorted.map((p) => {
                  const expanded = expandedCodes.has(p.code);
                  const highlighted = highlightCode === p.code;
                  return (
                    <Fragment key={p.key}>
                      <tr
                        ref={(el) => {
                          if (el) {
                            rowRefs.current.set(p.code, el);
                          } else {
                            rowRefs.current.delete(p.code);
                          }
                        }}
                        onClick={() => toggleExpanded(p.code)}
                        aria-expanded={expanded}
                        className={`cursor-pointer border-t border-slate-100 hover:bg-slate-50/60 ${
                          expanded ? "bg-blue-50/70" : highlighted ? "bg-amber-50/70" : ""
                        }`}
                      >
                        <td className="px-4 py-3 sm:px-5">
                          <div className="flex items-start gap-2">
                            <ExpandArrow expanded={expanded} />
                            <div>
                              <FundLink
                                code={p.code}
                                name={p.name}
                                stopPropagation
                                className="font-medium text-slate-800 hover:text-blue-700 hover:underline"
                              />
                              <p className="text-xs text-slate-400">
                                {p.code}
                                {p.windowItem?.isQdii && (
                                  <span className="ml-1.5 rounded border border-slate-200 px-1 py-px text-[10px] text-slate-400">
                                    QDII
                                  </span>
                                )}
                              </p>
                              <div className="mt-1">
                                <WatchlistButton kind="fund" code={p.code} name={p.name} size="sm" />
                              </div>
                            </div>
                          </div>
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">{fmtShares(p.shares)}</td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">{fmtMoney(p.costPrice, 4)}</td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">{fmtMoney(p.nav, 4)}</td>
                        <td className="px-4 py-3 text-right tabular-nums font-medium text-slate-800">{fmtMoney(p.marketValue)}</td>
                        <WindowReturnCell row={p} kind="amount" />
                        <WindowReturnCell row={p} kind="rate" />
                        <td className={`px-4 py-3 text-right tabular-nums ${signClass(p.profit)}`}>{fmtMoney(p.profit)}</td>
                        <td className={`px-4 py-3 text-right tabular-nums ${signClass(p.returnRate)}`}>{fmtPercent(p.returnRate)}</td>
                        <td className="px-4 py-3 sm:px-5">
                          {p.weight !== null ? (
                            <div className="flex items-center gap-2">
                              <div className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-100">
                                <div
                                  className="h-full rounded-full bg-slate-700"
                                  style={{ width: `${Math.max(p.weight, 1)}%` }}
                                />
                              </div>
                              <span className="text-xs tabular-nums text-slate-500">
                                {p.weight.toFixed(1)}%
                              </span>
                            </div>
                          ) : (
                            <span className="text-xs text-slate-400">—</span>
                          )}
                        </td>
                      </tr>
                      {expanded && (
                        <tr className="border-t border-slate-100 bg-slate-50/40">
                          <td colSpan={colCount} className="px-4 py-4 sm:px-5">
                            {p.windowItem && (
                              <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
                                <span>
                                  {windowMeta?.label}实际净值区间：
                                  <span className="tabular-nums text-slate-700">
                                    {p.windowItem.startDate ? fmtDate(p.windowItem.startDate) : "—"}
                                    {" → "}
                                    {p.windowItem.endDate ? fmtDate(p.windowItem.endDate) : "—"}
                                  </span>
                                </span>
                                {p.windowItem.status === "approximate" && (
                                  <span className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-amber-700">
                                    窗口内有份额变动，收益为估算值
                                  </span>
                                )}
                                {p.windowItem.staleReason && (
                                  <span className="rounded border border-slate-200 bg-white px-1.5 py-0.5 text-slate-500">
                                    {p.windowItem.staleReason}
                                  </span>
                                )}
                              </div>
                            )}
                            <LazyFundHistoryChart fundCode={p.code} fundName={p.name} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="border-t border-slate-100 px-4 py-2.5 text-xs text-slate-400 sm:px-5">
            点击任意行展开 / 收起该基金历史走势。窗口收益按当前份额与最新净值估算；QDII 基金净值披露滞后，按其自身实际净值日期计算。
          </p>
        </Card>
      )}
    </>
  );
}

/** 窗口汇总提示：覆盖率 / 滞后 / 数据日期 */
function WindowHint({ view }: { view: ReturnWindowView }) {
  const coveragePct = view.coverage !== null ? view.coverage * 100 : null;
  const partial = coveragePct !== null && coveragePct < 99.5;
  const qdiiLag = view.items.some(
    (it) => it.isQdii && it.staleReason !== null && it.status !== "stale"
  );
  if (!partial && !qdiiLag && !view.asOfEndDate) return null;
  return (
    <span className="flex flex-wrap items-center gap-1.5 text-xs">
      {partial && (
        <span className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-amber-700">
          覆盖率 {coveragePct!.toFixed(0)}%
        </span>
      )}
      {qdiiLag && (
        <span className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-slate-500">
          QDII 净值滞后
        </span>
      )}
      {view.asOfEndDate && (
        <span className="tabular-nums text-slate-400">净值至 {fmtDate(view.asOfEndDate)}</span>
      )}
    </span>
  );
}

/** 窗口收益单元格：金额或收益率，附带状态提示 */
function WindowReturnCell({ row, kind }: { row: MergedRow; kind: "amount" | "rate" }) {
  const value = kind === "amount" ? row.windowReturn : row.windowReturnRate;
  const item = row.windowItem;
  if (!item) {
    return <td className="px-4 py-3 text-right tabular-nums text-slate-400">—</td>;
  }
  if (item.status === "stale" || value === null) {
    return (
      <td className="px-4 py-3 text-right" title={item.staleReason ?? "净值数据不足"}>
        <span className="tabular-nums text-slate-400">—</span>
      </td>
    );
  }
  return (
    <td
      className={`px-4 py-3 text-right tabular-nums ${signClass(value)}`}
      title={
        [
          item.status === "approximate" ? "窗口内有份额变动，为估算值" : null,
          item.staleReason,
          item.endDate ? `实际净值日期 ${fmtDate(item.endDate)}` : null,
        ]
          .filter(Boolean)
          .join("；") || undefined
      }
    >
      {kind === "amount" ? fmtMoney(value) : fmtPercent(value)}
      {item.status === "approximate" && <span className="ml-0.5 text-[10px] text-slate-400">≈</span>}
    </td>
  );
}

export default function PositionsPage() {
  return (
    <Suspense
      fallback={
        <Card>
          <Spinner label="正在加载持仓…" />
        </Card>
      }
    >
      <PositionsPageContent />
    </Suspense>
  );
}

function SortIcon({ active, direction }: { active: boolean; direction: SortDirection }) {
  return (
    <span className="flex flex-col leading-none">
      <svg viewBox="0 0 8 8" className={`h-2 w-2 ${active && direction === "asc" ? "text-slate-900" : "text-slate-300"}`} fill="currentColor">
        <path d="M4 0l4 6H0z" />
      </svg>
      <svg viewBox="0 0 8 8" className={`mt-0.5 h-2 w-2 ${active && direction === "desc" ? "text-slate-900" : "text-slate-300"}`} fill="currentColor">
        <path d="M4 8L0 2h8z" />
      </svg>
    </span>
  );
}

function ExpandArrow({ expanded }: { expanded: boolean }) {
  return (
    <svg
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`mt-1 h-3 w-3 shrink-0 transition-transform duration-150 ${expanded ? "rotate-90 text-blue-600" : "text-slate-400"}`}
    >
      <path d="M4 2l4 4-4 4" />
    </svg>
  );
}
