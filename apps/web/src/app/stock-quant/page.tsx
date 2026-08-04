"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, invalidateApiCache } from "@/lib/api";
import type {
  StockPaperHistoryPoint,
  StockPaperSummary,
  StockPaperTrade,
} from "@/lib/types";
import { Card, EmptyState, ErrorState, PageHeader, Spinner } from "@/components/ui";

const money = new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: "CNY",
  maximumFractionDigits: 0,
});

function pct(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : `${(value * 100).toFixed(2)}%`;
}

function num(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : value.toFixed(2);
}

function metricColor(value: number | null | undefined): string {
  if (value === null || value === undefined) return "text-slate-700";
  if (value > 0) return "text-rose-600";
  if (value < 0) return "text-emerald-600";
  return "text-slate-700";
}

function NavChart({ points }: { points: StockPaperHistoryPoint[] }) {
  const chart = useMemo(() => {
    if (points.length < 2) return null;
    const all = points.flatMap((point) => [point.nav, point.benchmark_nav]);
    const low = Math.min(...all);
    const high = Math.max(...all);
    const pad = Math.max((high - low) * 0.12, 0.005);
    const min = low - pad;
    const max = high + pad;
    const xy = (value: number, index: number) => {
      const x = 20 + (index / Math.max(points.length - 1, 1)) * 760;
      const y = 195 - ((value - min) / Math.max(max - min, 1e-9)) * 170;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    };
    return {
      strategy: points.map((point, index) => xy(point.nav, index)).join(" "),
      benchmark: points
        .map((point, index) => xy(point.benchmark_nav, index))
        .join(" "),
      min,
      max,
    };
  }, [points]);
  if (!chart) {
    return <EmptyState title="等待更多净值点" hint="至少两个真实交易日后显示策略与基准曲线。" />;
  }
  return (
    <div>
      <div className="mb-2 flex gap-4 text-xs text-slate-500">
        <span><span className="mr-1 inline-block h-0.5 w-4 bg-blue-600 align-middle" />策略</span>
        <span><span className="mr-1 inline-block h-0.5 w-4 bg-slate-400 align-middle" />候选池等权基准</span>
      </div>
      <svg viewBox="0 0 800 220" className="h-56 w-full" role="img" aria-label="策略与基准净值曲线">
        {[25, 67.5, 110, 152.5, 195].map((y) => (
          <line key={y} x1="20" x2="780" y1={y} y2={y} stroke="#e2e8f0" strokeWidth="1" />
        ))}
        <polyline points={chart.benchmark} fill="none" stroke="#94a3b8" strokeWidth="2" />
        <polyline points={chart.strategy} fill="none" stroke="#2563eb" strokeWidth="2.5" />
      </svg>
      <div className="flex justify-between text-[11px] text-slate-400">
        <span>{points[0]?.date}</span>
        <span>净值范围 {chart.min.toFixed(3)}–{chart.max.toFixed(3)}</span>
        <span>{points.at(-1)?.date}</span>
      </div>
    </div>
  );
}

export default function StockQuantPage() {
  const [summary, setSummary] = useState<StockPaperSummary | null>(null);
  const [trades, setTrades] = useState<StockPaperTrade[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [summaryResult, tradesResult] = await Promise.all([
        api.stockPaperSummary(),
        api.stockPaperTrades(),
      ]);
      setSummary(summaryResult);
      setTrades(tradesResult);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "股票量化工作台加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const run = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      await api.stockPaperRun();
      invalidateApiCache("/api/stocks/paper");
      await load();
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : cause instanceof Error
            ? cause.message
            : "前向模拟运行失败"
      );
    } finally {
      setRunning(false);
    }
  }, [load]);

  if (loading && !summary) {
    return <Spinner label="正在加载 A 股量化工作台…" />;
  }

  return (
    <>
      <PageHeader
        title="A 股量化"
        description="规则多因子选股、月频调仓与两个月真实前向模拟；不会产生真实订单"
      />

      {error && (
        <Card className="mb-4">
          <ErrorState message={error} onRetry={() => void load()} />
        </Card>
      )}

      {summary && (
        <>
          <Card className="mb-4 px-4 py-4 sm:px-5">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-sm font-semibold text-slate-800">
                    {summary.strategy?.name ?? "两个月前向验证尚未启动"}
                  </h2>
                  <span
                    className={`rounded-full px-2 py-0.5 text-[11px] ${
                      summary.readiness.ready
                        ? "bg-emerald-50 text-emerald-700"
                        : "bg-amber-50 text-amber-700"
                    }`}
                  >
                    {summary.readiness.ready ? "数据可启动" : "数据待补齐"}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  行情日 {summary.readiness.latest_data_date ?? "—"} ·
                  日线 {summary.readiness.daily_ready_count}/{summary.readiness.universe_count} ·
                  行业 {summary.readiness.industry_ready_count}/{summary.readiness.universe_count} ·
                  财务 {summary.readiness.financial_ready_count}/{summary.readiness.universe_count} ·
                  估值 {summary.readiness.valuation_ready_count}/{summary.readiness.universe_count}
                </p>
              </div>
              <button
                type="button"
                onClick={() => void run()}
                disabled={running || !summary.readiness.ready}
                className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {running
                  ? "正在推进…"
                  : summary.started
                    ? "推进到最新行情日"
                    : "启动两个月前向验证"}
              </button>
            </div>

            {summary.strategy && (
              <div className="mt-4">
                <div className="mb-1.5 flex justify-between text-xs text-slate-500">
                  <span>{summary.strategy.trial_start}</span>
                  <span>
                    已观察 {summary.strategy.calendar_days_elapsed} 天 ·
                    剩余 {summary.strategy.calendar_days_remaining} 天
                  </span>
                  <span>{summary.strategy.trial_end}</span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-slate-100">
                  <div
                    className="h-full rounded-full bg-blue-600"
                    style={{ width: `${Math.min(summary.strategy.observation_progress * 100, 100)}%` }}
                  />
                </div>
              </div>
            )}

            {[...summary.readiness.blockers, ...summary.warnings].length > 0 && (
              <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs leading-relaxed text-amber-800">
                {[...summary.readiness.blockers, ...summary.warnings].map((warning, index) => (
                  <p key={`${warning}-${index}`}>{warning}</p>
                ))}
              </div>
            )}
          </Card>

          <div className="mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
            {[
              ["总资产", money.format(summary.total_value), "text-slate-800"],
              ["策略收益", pct(summary.metrics.total_return), metricColor(summary.metrics.total_return)],
              ["基准收益", pct(summary.metrics.benchmark_return), metricColor(summary.metrics.benchmark_return)],
              ["超额收益", pct(summary.metrics.excess_return), metricColor(summary.metrics.excess_return)],
              ["最大回撤", pct(summary.metrics.max_drawdown), "text-slate-800"],
              ["夏普比率", num(summary.metrics.sharpe), "text-slate-800"],
            ].map(([label, value, color]) => (
              <Card key={label} className="px-4 py-3">
                <p className="text-xs text-slate-500">{label}</p>
                <p className={`mt-1 text-lg font-semibold tabular-nums ${color}`}>{value}</p>
              </Card>
            ))}
          </div>

          <div className="grid gap-4 xl:grid-cols-[1.4fr_1fr]">
            <Card className="px-4 py-4 sm:px-5">
              <h2 className="mb-3 text-sm font-semibold text-slate-800">前向净值</h2>
              <NavChart points={summary.history} />
            </Card>
            <Card className="px-4 py-4 sm:px-5">
              <h2 className="mb-3 text-sm font-semibold text-slate-800">评估口径</h2>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
                {[
                  ["真实交易日", String(summary.metrics.trading_days)],
                  ["调仓次数", String(summary.metrics.rebalance_count)],
                  ["成交笔数", String(summary.metrics.trade_count)],
                  ["累计费用", money.format(summary.metrics.total_fees)],
                  ["年化波动", pct(summary.metrics.annual_volatility)],
                  ["信息比率", num(summary.metrics.information_ratio)],
                  ["日胜率", pct(summary.metrics.win_rate)],
                  ["现金", money.format(summary.cash)],
                ].map(([label, value]) => (
                  <div key={label}>
                    <dt className="text-xs text-slate-500">{label}</dt>
                    <dd className="mt-0.5 font-medium tabular-nums text-slate-800">{value}</dd>
                  </div>
                ))}
              </dl>
            </Card>
          </div>

          <Card className="mt-4 overflow-hidden">
            <div className="px-4 py-4 sm:px-5">
              <h2 className="text-sm font-semibold text-slate-800">最新目标信号</h2>
              <p className="mt-0.5 text-xs text-slate-500">
                {summary.latest_signal
                  ? `${summary.latest_signal.signal_date} · ${summary.latest_signal.status} · 股票仓位 ${pct(summary.latest_signal.invested_weight)}`
                  : "首次运行后生成 T 日收盘信号，下一交易日才会成交"}
              </p>
            </div>
            {summary.latest_signal?.items.length ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[760px] text-sm">
                  <thead className="border-t border-slate-100 bg-slate-50 text-xs text-slate-500">
                    <tr>
                      <th className="px-4 py-2.5 text-left font-medium sm:px-5">排名 / 股票</th>
                      <th className="px-4 py-2.5 text-left font-medium">行业</th>
                      <th className="px-4 py-2.5 text-right font-medium">复合分</th>
                      <th className="px-4 py-2.5 text-right font-medium">目标权重</th>
                      <th className="px-4 py-2.5 text-right font-medium">质量</th>
                      <th className="px-4 py-2.5 text-right font-medium">价值</th>
                      <th className="px-4 py-2.5 text-right font-medium">动量</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.latest_signal.items.map((item) => (
                      <tr key={item.code} className="border-t border-slate-100">
                        <td className="px-4 py-3 sm:px-5">
                          <span className="mr-2 text-xs text-slate-400">#{item.rank}</span>
                          <span className="font-medium text-slate-800">{item.name}</span>
                          <span className="ml-2 text-xs text-slate-400">{item.code}</span>
                        </td>
                        <td className="px-4 py-3 text-slate-600">{item.industry}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{num(item.composite)}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{pct(item.weight)}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{num(item.quality)}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{num(item.value)}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{num(item.momentum)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="尚无目标信号" hint="数据就绪后点击启动，首日只生成信号，不会当日成交。" />
            )}
          </Card>

          <Card className="mt-4 overflow-hidden">
            <div className="px-4 py-4 sm:px-5">
              <h2 className="text-sm font-semibold text-slate-800">当前模拟持仓</h2>
            </div>
            {summary.positions.length ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[720px] text-sm">
                  <thead className="border-t border-slate-100 bg-slate-50 text-xs text-slate-500">
                    <tr>
                      <th className="px-4 py-2.5 text-left font-medium sm:px-5">股票</th>
                      <th className="px-4 py-2.5 text-left font-medium">行业</th>
                      <th className="px-4 py-2.5 text-right font-medium">持仓股数</th>
                      <th className="px-4 py-2.5 text-right font-medium">市值</th>
                      <th className="px-4 py-2.5 text-right font-medium">权重</th>
                      <th className="px-4 py-2.5 text-right font-medium">持仓盈亏</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.positions.map((position) => (
                      <tr key={position.code} className="border-t border-slate-100">
                        <td className="px-4 py-3 sm:px-5">
                          <span className="font-medium text-slate-800">{position.name}</span>
                          <span className="ml-2 text-xs text-slate-400">{position.code}</span>
                        </td>
                        <td className="px-4 py-3 text-slate-600">{position.industry}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{position.shares.toFixed(0)}</td>
                        <td className="px-4 py-3 text-right tabular-nums">
                          {position.market_value == null ? "—" : money.format(position.market_value)}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums">{pct(position.weight)}</td>
                        <td className={`px-4 py-3 text-right tabular-nums ${metricColor(position.pnl)}`}>
                          {position.pnl == null ? "—" : money.format(position.pnl)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="当前持有现金" hint="首个信号会在下一真实交易日按开盘价模拟成交。" />
            )}
          </Card>

          <Card className="mt-4 overflow-hidden">
            <div className="px-4 py-4 sm:px-5">
              <h2 className="text-sm font-semibold text-slate-800">模拟成交</h2>
            </div>
            {trades.length ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[760px] text-sm">
                  <thead className="border-t border-slate-100 bg-slate-50 text-xs text-slate-500">
                    <tr>
                      <th className="px-4 py-2.5 text-left font-medium sm:px-5">成交日 / 股票</th>
                      <th className="px-4 py-2.5 text-left font-medium">方向</th>
                      <th className="px-4 py-2.5 text-right font-medium">股数</th>
                      <th className="px-4 py-2.5 text-right font-medium">价格</th>
                      <th className="px-4 py-2.5 text-right font-medium">金额</th>
                      <th className="px-4 py-2.5 text-right font-medium">费用</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map((trade) => (
                      <tr key={trade.id} className="border-t border-slate-100">
                        <td className="px-4 py-3 sm:px-5">
                          <span className="text-xs text-slate-400">{trade.trade_date}</span>
                          <span className="ml-2 font-medium text-slate-800">{trade.name}</span>
                          <span className="ml-1 text-xs text-slate-400">{trade.code}</span>
                        </td>
                        <td className={`px-4 py-3 ${trade.side === "buy" ? "text-rose-600" : "text-emerald-600"}`}>
                          {trade.side === "buy" ? "买入" : "卖出"}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums">{trade.shares.toFixed(0)}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{trade.price.toFixed(3)}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{money.format(trade.amount)}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{money.format(trade.fee)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="尚无成交" hint="T日信号要到下一个真实交易日才执行。" />
            )}
          </Card>
        </>
      )}
    </>
  );
}
