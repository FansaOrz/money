"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, ApiError, peekApiCache } from "@/lib/api";
import type { PortfolioReturnsResponse, PortfolioSummary, Position } from "@/lib/types";
import {
  fmtDate,
  fmtMoney,
  fmtPercent,
  normalizePortfolioReturns,
  normalizePositions,
  normalizeSummary,
  signClass,
  type PositionView,
  type ReturnWindowView,
  type SummaryView,
} from "@/lib/normalize";
import { Card, EmptyState, ErrorState, PageHeader, SnapshotNotice, Spinner } from "@/components/ui";
import { AllocationBar } from "@/components/AllocationBar";
import { FundLink } from "@/components/FundLink";
import { MarketTrends } from "@/components/MarketTrends";

function StatCard({
  label,
  value,
  sub,
  valueClass = "text-slate-900",
}: {
  label: string;
  value: string;
  sub?: string;
  valueClass?: string;
}) {
  return (
    <Card className="px-4 py-4 sm:px-5">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`mt-1.5 text-xl font-semibold tabular-nums sm:text-2xl ${valueClass}`}>
        {value}
      </p>
      {sub && <p className="mt-1 text-xs text-slate-400">{sub}</p>}
    </Card>
  );
}

/** 区间收益卡：金额 + 收益率 + 覆盖率/滞后提示 */
function ReturnCard({ view }: { view: ReturnWindowView }) {
  const coveragePct = view.coverage !== null ? view.coverage * 100 : null;
  const partial = coveragePct !== null && coveragePct < 99.5;
  const qdiiLag = view.items.some(
    (it) => it.isQdii && it.staleReason !== null && it.status !== "stale"
  );
  const todayFreshCoverage =
    view.key === "1d" && view.asOfEndDate
      ? view.items
          .filter((item) => item.endDate === view.asOfEndDate)
          .reduce((sum, item) => sum + (item.weight ?? 0), 0)
      : null;
  const todayIsPartial = todayFreshCoverage !== null && todayFreshCoverage < 0.995;
  const displayedAmount = todayIsPartial
    ? view.items
        .filter((item) => item.endDate === view.asOfEndDate)
        .reduce((sum, item) => sum + (item.returnAmount ?? 0), 0)
    : view.returnAmount;
  const displayedRate = todayIsPartial ? null : view.returnRate;
  const hasData = displayedAmount !== null || displayedRate !== null;

  const hints: string[] = [];
  if (todayIsPartial) {
    hints.push(`仅汇总今日已更新的 ${(todayFreshCoverage! * 100).toFixed(0)}%，金额仍会变化`);
  }
  if (partial) hints.push(`覆盖率 ${coveragePct!.toFixed(0)}%`);
  if (qdiiLag) hints.push("QDII 净值滞后");
  if ((view.approximateCount ?? 0) > 0) hints.push("含估算");

  return (
    <Card className="px-4 py-4 sm:px-5">
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-xs text-slate-500">{view.label}收益（元）</p>
        {view.asOfEndDate && (
          <p className="text-[11px] tabular-nums text-slate-400">
            净值至 {fmtDate(view.asOfEndDate)}
          </p>
        )}
      </div>
      {hasData ? (
        <>
          <p
            className={`mt-1.5 text-xl font-semibold tabular-nums sm:text-2xl ${signClass(displayedAmount)}`}
          >
            {fmtMoney(displayedAmount)}
          </p>
          <p className={`mt-0.5 text-sm tabular-nums ${signClass(displayedRate)}`}>
            {fmtPercent(displayedRate)}
          </p>
          <p className="mt-1 text-xs text-slate-400">
            {hints.length > 0 ? hints.join(" · ") : "全量覆盖"}
          </p>
        </>
      ) : (
        <>
          <p className="mt-1.5 text-xl font-semibold tabular-nums text-slate-400 sm:text-2xl">—</p>
          <p className="mt-1 text-xs text-slate-400">
            {(view.staleCount ?? 0) > 0 ? "净值数据不足" : "暂无数据"}
          </p>
        </>
      )}
    </Card>
  );
}

export default function DashboardPage() {
  const cachedSummary = normalizeSummary(
    peekApiCache<PortfolioSummary>("/api/portfolio/summary") ?? null
  );
  const cachedPositions = normalizePositions(peekApiCache<Position[]>("/api/positions"));
  const cachedReturnsRaw = peekApiCache<PortfolioReturnsResponse>("/api/portfolio/returns");
  const cachedReturns = cachedReturnsRaw ? normalizePortfolioReturns(cachedReturnsRaw) : null;
  const [summary, setSummary] = useState<SummaryView | null>(cachedSummary);
  const [positions, setPositions] = useState<PositionView[]>(cachedPositions);
  const [returns, setReturns] = useState<ReturnWindowView[] | null>(cachedReturns);
  const [loading, setLoading] = useState(!cachedSummary && cachedPositions.length === 0);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [summaryRaw, positionsRaw, returnsRaw] = await Promise.allSettled([
        api.portfolioSummary(),
        api.positions(),
        api.portfolioReturns(),
      ]);

      if (summaryRaw.status === "fulfilled") {
        setSummary(normalizeSummary(summaryRaw.value as PortfolioSummary));
      }
      if (positionsRaw.status === "fulfilled") {
        setPositions(normalizePositions(positionsRaw.value as Position[]));
      }
      // 收益接口宽松容错：失败时不阻塞页面其余部分
      if (returnsRaw.status === "fulfilled") {
        setReturns(normalizePortfolioReturns(returnsRaw.value));
      } else {
        setReturns(null);
      }

      if (summaryRaw.status === "rejected" && positionsRaw.status === "rejected") {
        const reason = summaryRaw.reason;
        throw reason instanceof Error ? reason : new Error(String(reason));
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "网络请求失败，请确认后端服务已启动");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 60_000);
    const refreshOnFocus = () => void load();
    window.addEventListener("focus", refreshOnFocus);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshOnFocus);
    };
  }, [load]);

  const snapshotDate = summary?.asOf ? fmtDate(summary.asOf) : null;

  return (
    <>
      <PageHeader
        title="总览"
        description="组合总览与主要市场趋势"
        action={
          <Link
            href="/imports"
            className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
          >
            导入快照
          </Link>
        }
      />
      <SnapshotNotice date={snapshotDate} />

      {/* 主要市场趋势（自量化分析页迁移至总览，作为全局市场背景） */}
      <Card className="mb-6 px-4 py-5 sm:px-5">
        <h2 className="mb-4 text-sm font-semibold text-slate-800">A股 · 港股 · 美股趋势</h2>
        <MarketTrends />
      </Card>

      {loading ? (
        <Card>
          <Spinner label="正在加载组合数据…" />
        </Card>
      ) : error ? (
        <Card>
          <ErrorState message={error} onRetry={load} />
        </Card>
      ) : positions.length === 0 && !summary?.totalMarketValue ? (
        <Card>
          <EmptyState
            title="暂无持仓数据"
            hint="导入 PDF 净值快照后，这里将展示组合总览。"
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
        <>
          <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
            <StatCard
              label="总市值（元）"
              value={`¥${fmtMoney(summary?.totalMarketValue)}`}
              sub={snapshotDate ? `截至 ${snapshotDate}` : undefined}
            />
            <StatCard
              label="支付宝累计收益（元）"
              value={`¥${fmtMoney(summary?.totalProfit)}`}
              valueClass={signClass(summary?.totalProfit)}
              sub="以支付宝收益基准持续更新"
            />
            <StatCard
              label="今年收益（元）"
              value={`¥${fmtMoney(summary?.yearReturn)}`}
              valueClass={signClass(summary?.yearReturn)}
              sub="以支付宝年度收益基准持续更新"
            />
            <StatCard
              label="去年收益（元）"
              value={`¥${fmtMoney(summary?.previousYearReturn)}`}
              valueClass={signClass(summary?.previousYearReturn)}
              sub={`持仓基金 ${summary?.positionCount ?? positions.length} 只`}
            />
          </div>
          <p className="mt-3 text-xs text-slate-400">
            系统根据你提供的支付宝累计收益 ¥27,172.30、2026 年收益 -¥442.93、2025 年收益 ¥18,774.36 建立基准；后续净值变化会在此基础上更新。交易流水推算的成本仅用于单只基金分析。
          </p>

          {returns && returns.some((r) => r.returnAmount !== null || r.returnRate !== null) && (
            <>
              <div className="mt-6 grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
                {returns.map((r) => (
                  <ReturnCard key={r.key} view={r} />
                ))}
              </div>
              <p className="mt-2 text-xs text-slate-400">
                区间收益按当前份额与最新净值估算，QDII 基金净值披露滞后时以其实际净值日期计算；覆盖率表示参与加权的市值占比。
              </p>
            </>
          )}

          <Card className="mt-6 px-4 py-5 sm:px-5">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-800">持仓分布</h2>
              <Link href="/positions" className="text-xs font-medium text-slate-500 hover:text-slate-900">
                查看全部 →
              </Link>
            </div>
            <AllocationBar positions={positions} />
          </Card>

          <Card className="mt-6 overflow-hidden">
            <div className="flex items-center justify-between px-4 py-4 sm:px-5">
              <h2 className="text-sm font-semibold text-slate-800">市值前 5 持仓</h2>
              <Link href="/positions" className="text-xs font-medium text-slate-500 hover:text-slate-900">
                查看全部 →
              </Link>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead>
                  <tr className="border-t border-slate-100 bg-slate-50 text-left text-xs text-slate-500">
                    <th className="px-4 py-2.5 font-medium sm:px-5">基金</th>
                    <th className="px-4 py-2.5 text-right font-medium">净值</th>
                    <th className="px-4 py-2.5 text-right font-medium">市值（元）</th>
                    <th className="px-4 py-2.5 text-right font-medium">收益（元）</th>
                    <th className="px-4 py-2.5 text-right font-medium sm:px-5">收益率</th>
                  </tr>
                </thead>
                <tbody>
                  {[...positions]
                    .sort(
                      (a, b) =>
                        (Number(b.marketValue) || 0) - (Number(a.marketValue) || 0)
                    )
                    .slice(0, 5)
                    .map((p) => (
                      <tr key={p.key} className="border-t border-slate-100">
                        <td className="px-4 py-3 sm:px-5">
                          <FundLink code={p.code} name={p.name} className="block font-medium text-slate-800 hover:text-blue-700 hover:underline" />
                          <p className="text-xs text-slate-400">{p.code}</p>
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                          {fmtMoney(p.nav, 4)}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-800">
                          {fmtMoney(p.marketValue)}
                        </td>
                        <td className={`px-4 py-3 text-right tabular-nums ${signClass(p.profit)}`}>
                          {fmtMoney(p.profit)}
                        </td>
                        <td className={`px-4 py-3 text-right tabular-nums sm:px-5 ${signClass(p.returnRate)}`}>
                          {fmtPercent(p.returnRate)}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </>
  );
}
