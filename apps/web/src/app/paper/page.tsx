"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import {
  fmtDate,
  fmtMoney,
  fmtPercent,
  fmtShares,
  normalizePaperHistory,
  normalizePaperPositions,
  normalizePaperSignals,
  normalizePaperSummary,
  normalizePaperTrades,
  signClass,
  toNumber,
  type PaperCurvePoint,
  type PaperPositionView,
  type PaperSignalView,
  type PaperSummaryView,
  type PaperTradeView,
} from "@/lib/normalize";
import { Card, EmptyState, ErrorState, PageHeader, Spinner } from "@/components/ui";
import { FundLink } from "@/components/FundLink";

const API_DOWN_HINT =
  "模拟交易接口暂不可用。该功能依赖后端 /api/paper/* 接口，当前后端尚未上线该模块。";

const DEFAULT_INITIAL_CAPITAL = 1_000_000;

function MetricCard({
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

/* ---------- 模拟盘告示 ---------- */

function PaperNotice({ asOf }: { asOf?: string | null }) {
  return (
    <div className="mb-6 flex items-start gap-2.5 rounded-lg border border-sky-200 bg-sky-50 px-3.5 py-2.5 text-sm text-sky-800">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="mt-0.5 h-4 w-4 shrink-0">
        <path d="M12 8v5M12 16.5v.5" />
        <circle cx="12" cy="12" r="9" />
      </svg>
      <span>
        本页面为<span className="font-semibold">模拟交易（虚拟盘）</span>
        ，初始虚拟资金 100 万元，所有持仓与成交均为策略信号驱动的虚拟记录，
        <span className="font-semibold">不涉及任何真实资金与真实交易</span>
        {asOf ? `（数据日期：${fmtDate(asOf)}）` : ""}。仅供参考，不构成投资建议。
      </span>
    </div>
  );
}

/* ---------- 策略 vs 等权基准 曲线 ---------- */

function PaperCurveChart({ points }: { points: PaperCurvePoint[] }) {
  const chart = useMemo(() => {
    if (points.length < 2) return null;
    const values = points.flatMap((p) => (p.benchmark !== null ? [p.value, p.benchmark] : [p.value]));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || Math.max(Math.abs(max) * 0.01, 0.0001);
    const width = 560;
    const height = 170;
    const left = 55;
    const top = 16;
    const xFor = (i: number) => left + (i / (points.length - 1)) * width;
    const yFor = (v: number) => top + (1 - (v - min) / span) * height;

    const lineOf = (get: (p: PaperCurvePoint) => number | null) => {
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

    const navLine = lineOf((p) => p.value);
    const benchLine = lineOf((p) => p.benchmark);
    const areaPath = `${navLine} L${xFor(points.length - 1).toFixed(1)},${top + height} L${left},${top + height} Z`;
    const fmtAxis = (v: number) =>
      Math.abs(v) >= 10_000 ? `${(v / 10_000).toFixed(1)}万` : v.toFixed(0);
    const yLabels = [0, 1, 2, 3].map((i) => fmtAxis(max - (span * i) / 3));
    return { navLine, benchLine, areaPath, yLabels, width, height, left, top };
  }, [points]);

  if (!chart || points.length < 2) {
    return (
      <EmptyState
        title="暂无可绘制的模拟净值曲线"
        hint="GET /api/paper/history 返回为空或不足两个点。可先点击右上角「运行一次模拟」生成数据。"
      />
    );
  }

  const first = points[0];
  const latest = points[points.length - 1];
  const hasBenchmark = points.some((p) => p.benchmark !== null);
  const changeRate = first.value > 0 ? (latest.value / first.value - 1) * 100 : null;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs text-slate-500">最新模拟总值（{fmtDate(latest.date)}）</p>
          <p className="text-2xl font-semibold tabular-nums text-slate-900">
            ¥{fmtMoney(latest.value)}
          </p>
        </div>
        <div className="flex items-center gap-4 text-xs text-slate-500">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-5 bg-blue-600" /> 策略总值
          </span>
          {hasBenchmark && (
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-0.5 w-5 bg-slate-400" /> 等权基准
            </span>
          )}
          {changeRate !== null && (
            <span className={`text-sm font-medium ${changeRate < 0 ? "text-emerald-600" : "text-rose-600"}`}>
              {changeRate >= 0 ? "+" : ""}
              {changeRate.toFixed(2)}%
            </span>
          )}
        </div>
      </div>
      <svg viewBox="0 0 620 220" className="h-56 w-full overflow-visible sm:h-64">
        <defs>
          <linearGradient id="paperFill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#2563eb" stopOpacity="0.22" />
            <stop offset="100%" stopColor="#2563eb" stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {[0, 1, 2, 3].map((index) => {
          const y = chart.top + index * (chart.height / 3);
          return (
            <line
              key={index}
              x1={chart.left}
              x2={chart.left + chart.width}
              y1={y}
              y2={y}
              stroke="#e2e8f0"
              strokeDasharray="4"
            />
          );
        })}
        <path d={chart.areaPath} fill="url(#paperFill)" />
        {hasBenchmark && chart.benchLine && (
          <path
            d={chart.benchLine}
            fill="none"
            stroke="#94a3b8"
            strokeWidth="1.5"
            strokeDasharray="5 4"
          />
        )}
        <path
          d={chart.navLine}
          fill="none"
          stroke="#2563eb"
          strokeWidth="2.5"
          strokeLinecap="round"
        />
        {chart.yLabels.map((label, index) => (
          <text key={index} x="0" y={chart.top + 3 + index * (chart.height / 3)} fontSize="10" fill="#64748b">
            {label}
          </text>
        ))}
        <text x={chart.left} y="212" fontSize="10" fill="#64748b">
          {fmtDate(first.date)}
        </text>
        <text x={chart.left + chart.width - 70} y="212" fontSize="10" fill="#64748b">
          {fmtDate(latest.date)}
        </text>
      </svg>
    </div>
  );
}

/* ---------- 虚拟持仓表 ---------- */

function PaperPositionsTable({ positions }: { positions: PaperPositionView[] }) {
  if (positions.length === 0) {
    return (
      <EmptyState
        title="当前没有虚拟持仓"
        hint="GET /api/paper/positions 返回为空。模拟账户可能处于空仓状态。"
      />
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-sm">
        <thead>
          <tr className="bg-slate-50 text-left text-xs text-slate-500">
            <th className="px-4 py-2.5 font-medium sm:px-5">标的</th>
            <th className="px-4 py-2.5 text-right font-medium">份额</th>
            <th className="px-4 py-2.5 text-right font-medium">成本价</th>
            <th className="px-4 py-2.5 text-right font-medium">现价/净值</th>
            <th className="px-4 py-2.5 text-right font-medium">市值</th>
            <th className="px-4 py-2.5 text-right font-medium">权重</th>
            <th className="px-4 py-2.5 text-right font-medium">浮动盈亏</th>
            <th className="px-4 py-2.5 text-right font-medium sm:px-5">收益率</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr key={p.key} className="border-t border-slate-100 hover:bg-slate-50/60">
              <td className="px-4 py-3 sm:px-5">
                <FundLink code={p.code} name={p.name} />
                <p className="text-xs text-slate-400">{p.code}</p>
              </td>
              <td className="px-4 py-3 text-right tabular-nums text-slate-600">{fmtShares(p.shares)}</td>
              <td className="px-4 py-3 text-right tabular-nums text-slate-600">{fmtMoney(p.costPrice, 4)}</td>
              <td className="px-4 py-3 text-right tabular-nums text-slate-600">{fmtMoney(p.nav, 4)}</td>
              <td className="px-4 py-3 text-right tabular-nums text-slate-800">{fmtMoney(p.marketValue)}</td>
              <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                {p.weight !== null ? `${p.weight.toFixed(1)}%` : "—"}
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
  );
}

/* ---------- 虚拟交易表 ---------- */

const SIDE_BADGE: Record<PaperTradeView["side"], string> = {
  buy: "bg-rose-50 text-rose-700 border-rose-200",
  sell: "bg-emerald-50 text-emerald-700 border-emerald-200",
  other: "bg-slate-100 text-slate-600 border-slate-200",
};

function PaperTradesTable({ trades }: { trades: PaperTradeView[] }) {
  if (trades.length === 0) {
    return (
      <EmptyState
        title="暂无虚拟成交记录"
        hint="GET /api/paper/trades 返回为空。可点击「运行一次模拟」触发策略生成虚拟订单。"
      />
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-sm">
        <thead>
          <tr className="bg-slate-50 text-left text-xs text-slate-500">
            <th className="px-4 py-2.5 font-medium sm:px-5">日期</th>
            <th className="px-4 py-2.5 font-medium">标的</th>
            <th className="px-4 py-2.5 text-center font-medium">方向</th>
            <th className="px-4 py-2.5 text-right font-medium">份额</th>
            <th className="px-4 py-2.5 text-right font-medium">价格</th>
            <th className="px-4 py-2.5 text-right font-medium">金额</th>
            <th className="px-4 py-2.5 text-right font-medium sm:px-5">费用</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr key={t.key} className="border-t border-slate-100 hover:bg-slate-50/60">
              <td className="px-4 py-3 tabular-nums text-slate-600 sm:px-5">{fmtDate(t.date)}</td>
              <td className="px-4 py-3">
                <FundLink code={t.code} name={t.name} />
                <p className="text-xs text-slate-400">{t.code}</p>
                {t.reason && <p className="mt-0.5 text-xs text-slate-400">{t.reason}</p>}
              </td>
              <td className="px-4 py-3 text-center">
                <span
                  className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${SIDE_BADGE[t.side]}`}
                >
                  {t.sideLabel}
                </span>
              </td>
              <td className="px-4 py-3 text-right tabular-nums text-slate-600">{fmtShares(t.shares)}</td>
              <td className="px-4 py-3 text-right tabular-nums text-slate-600">{fmtMoney(t.price, 4)}</td>
              <td className="px-4 py-3 text-right tabular-nums text-slate-800">{fmtMoney(t.amount)}</td>
              <td className="px-4 py-3 text-right tabular-nums text-slate-600 sm:px-5">{fmtMoney(t.fee)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------- 最新信号（五档） ---------- */

const TIER_META: Record<number, { label: string; badge: string }> = {
  5: { label: "五档 · 核心配置", badge: "bg-rose-50 text-rose-700 border-rose-200" },
  4: { label: "四档 · 积极配置", badge: "bg-orange-50 text-orange-700 border-orange-200" },
  3: { label: "三档 · 标准配置", badge: "bg-slate-100 text-slate-600 border-slate-200" },
  2: { label: "二档 · 低配观察", badge: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  1: { label: "一档 · 回避/减仓", badge: "bg-emerald-100 text-emerald-800 border-emerald-300" },
};

function PaperSignalsPanel({ signals }: { signals: PaperSignalView[] }) {
  const groups = useMemo(() => {
    const byTier = new Map<number, PaperSignalView[]>();
    for (const s of signals) {
      const tier = s.tier ?? 3;
      if (!byTier.has(tier)) byTier.set(tier, []);
      byTier.get(tier)?.push(s);
    }
    return [5, 4, 3, 2, 1]
      .filter((t) => (byTier.get(t) ?? []).length > 0)
      .map((t) => ({
        tier: t,
        meta: TIER_META[t],
        items: (byTier.get(t) ?? []).sort(
          (a, b) => (b.targetWeight ?? Number.NEGATIVE_INFINITY) - (a.targetWeight ?? Number.NEGATIVE_INFINITY)
        ),
      }));
  }, [signals]);

  if (signals.length === 0) {
    return (
      <EmptyState
        title="暂无模拟信号"
        hint="GET /api/paper/signals 返回为空。信号由五档模型生成，驱动虚拟调仓。"
      />
    );
  }

  return (
    <div className="space-y-5">
      {groups.map((g) => (
        <section key={g.tier}>
          <div className="mb-2.5 flex items-center gap-2">
            <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${g.meta.badge}`}>
              {g.meta.label}
            </span>
            <span className="text-xs text-slate-400">{g.items.length} 只</span>
          </div>
          <div className="grid gap-3 lg:grid-cols-2">
            {g.items.map((s) => (
              <div key={s.key} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-slate-900">
                      <FundLink
                        code={s.code}
                        name={s.name}
                        className="font-semibold text-slate-900 hover:text-blue-700 hover:underline"
                      />
                      <span className="ml-1.5 text-xs font-normal text-slate-400">{s.code}</span>
                    </p>
                    <p className="mt-1 text-xs text-slate-500">
                      信号：<span className="text-slate-700">{s.signal}</span>
                      <span className="mx-1.5 text-slate-300">·</span>
                      目标权重：
                      <span className="tabular-nums text-slate-700">
                        {s.targetWeight !== null
                          ? `${(Math.abs(s.targetWeight) <= 1 ? s.targetWeight * 100 : s.targetWeight).toFixed(1)}%`
                          : "—"}
                      </span>
                      {s.score !== null && (
                        <>
                          <span className="mx-1.5 text-slate-300">·</span>
                          分数：<span className="tabular-nums text-slate-700">{s.score.toFixed(2)}</span>
                        </>
                      )}
                    </p>
                  </div>
                  {s.asOf && <p className="shrink-0 text-xs text-slate-400">{fmtDate(s.asOf)}</p>}
                </div>
                {s.reason && (
                  <p className="mt-2 rounded-lg bg-slate-50 px-3 py-2 text-xs leading-relaxed text-slate-600">
                    {s.reason}
                  </p>
                )}
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

/* ---------- 页面 ---------- */

export default function PaperPage() {
  const [summary, setSummary] = useState<PaperSummaryView | null>(null);
  const [history, setHistory] = useState<PaperCurvePoint[]>([]);
  const [positions, setPositions] = useState<PaperPositionView[]>([]);
  const [trades, setTrades] = useState<PaperTradeView[]>([]);
  const [signals, setSignals] = useState<PaperSignalView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [runMessage, setRunMessage] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, h, p, t, sig] = await Promise.all([
        api.paperSummary(),
        api.paperHistory(),
        api.paperPositions(),
        api.paperTrades(),
        api.paperSignals(),
      ]);
      setSummary(normalizePaperSummary(s));
      setHistory(normalizePaperHistory(h));
      setPositions(normalizePaperPositions(p));
      setTrades(normalizePaperTrades(t));
      setSignals(normalizePaperSignals(sig));
    } catch (e) {
      setError(
        e instanceof ApiError
          ? `${e.message}。${API_DOWN_HINT}`
          : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const run = useCallback(async () => {
    setRunning(true);
    setRunMessage(null);
    setRunError(null);
    try {
      const res = await api.paperRun();
      const count = toNumber(res.trade_count) ?? (Array.isArray(res.trades) ? res.trades.length : null);
      const base =
        typeof res.message === "string" && res.message
          ? res.message
          : "本次模拟运行完成（虚拟成交，非真实交易）";
      setRunMessage(count !== null && count > 0 ? `${base}，产生 ${count} 笔虚拟成交` : base);
      await load();
    } catch (e) {
      setRunError(
        e instanceof ApiError
          ? `${e.message}。${API_DOWN_HINT}`
          : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      setRunning(false);
    }
  }, [load]);

  const initialCapital = summary?.initialCapital ?? DEFAULT_INITIAL_CAPITAL;
  const totalValue = summary?.totalValue ?? null;
  const fallbackProfit =
    totalValue !== null ? totalValue - initialCapital : null;
  const totalProfit = summary?.totalProfit ?? fallbackProfit;
  const fallbackReturnRate =
    totalValue !== null && initialCapital > 0
      ? (totalValue - initialCapital) / initialCapital
      : null;
  const totalReturnRate =
    toNumber(summary?.totalReturnRate) !== null ? summary?.totalReturnRate : fallbackReturnRate;

  return (
    <>
      <PageHeader
        title="模拟交易"
        description="100 万虚拟资金按五档信号自动调仓的模拟盘，全部数据均为虚拟记录"
        action={
          <button
            type="button"
            onClick={run}
            disabled={running || loading}
            className="inline-flex items-center gap-2 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {running && (
              <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
              </svg>
            )}
            {running ? "正在运行…" : "运行一次模拟"}
          </button>
        }
      />

      <PaperNotice asOf={summary?.asOf} />

      {runMessage && (
        <div className="mb-6 rounded-lg border border-emerald-200 bg-emerald-50 px-3.5 py-2.5 text-sm text-emerald-800">
          {runMessage}
        </div>
      )}
      {runError && (
        <div className="mb-6 rounded-lg border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-sm text-rose-700">
          模拟运行失败：{runError}
        </div>
      )}

      {loading ? (
        <Card>
          <Spinner label="正在加载模拟账户…" />
        </Card>
      ) : error ? (
        <Card>
          <ErrorState message={error} onRetry={load} />
        </Card>
      ) : (
        <>
          {/* 账户概览 */}
          <div className="mb-6 grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
            <MetricCard
              label="账户总值（虚拟）"
              value={totalValue !== null ? `¥${fmtMoney(totalValue)}` : "—"}
              sub={`初始资金 ¥${fmtMoney(initialCapital, 0)}`}
            />
            <MetricCard
              label="可用现金（虚拟）"
              value={summary?.cash !== null && summary?.cash !== undefined ? `¥${fmtMoney(summary.cash)}` : "—"}
              sub={
                summary?.marketValue !== null && summary?.marketValue !== undefined
                  ? `持仓市值 ¥${fmtMoney(summary.marketValue)}`
                  : undefined
              }
            />
            <MetricCard
              label="累计收益（虚拟）"
              value={totalProfit !== null ? `¥${fmtMoney(totalProfit)}` : "—"}
              sub={totalReturnRate !== null ? `累计收益率 ${fmtPercent(totalReturnRate)}` : undefined}
              valueClass={signClass(totalProfit)}
            />
            <MetricCard
              label="当日收益（虚拟）"
              value={
                summary?.dailyProfit !== null && summary?.dailyProfit !== undefined
                  ? `¥${fmtMoney(summary.dailyProfit)}`
                  : "—"
              }
              sub={
                summary?.dailyReturnRate !== null && summary?.dailyReturnRate !== undefined
                  ? `当日收益率 ${fmtPercent(summary.dailyReturnRate)}`
                  : summary?.positionCount !== null && summary?.positionCount !== undefined
                    ? `持仓 ${summary.positionCount} 只`
                    : undefined
              }
              valueClass={signClass(summary?.dailyProfit)}
            />
          </div>

          {/* 策略 vs 等权基准 */}
          <Card className="mb-6 px-4 py-5 sm:px-5">
            <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
              <div>
                <h2 className="text-sm font-semibold text-slate-800">策略与等权基准曲线</h2>
                <p className="mt-0.5 text-xs text-slate-400">
                  模拟账户总值走势 vs 等权持有基准（GET /api/paper/history）
                </p>
              </div>
              {summary?.benchmarkReturn !== null && summary?.benchmarkReturn !== undefined && (
                <p className="text-xs text-slate-500">
                  基准累计收益：
                  <span className={`tabular-nums font-medium ${signClass(summary.benchmarkReturn)}`}>
                    {fmtPercent(summary.benchmarkReturn)}
                  </span>
                </p>
              )}
            </div>
            <PaperCurveChart points={history} />
          </Card>

          {/* 当前虚拟持仓 */}
          <Card className="mb-6 overflow-hidden">
            <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-100 px-4 py-3.5 sm:px-5">
              <div>
                <h2 className="text-sm font-semibold text-slate-800">当前虚拟持仓</h2>
                <p className="mt-0.5 text-xs text-slate-400">
                  仅为模拟账户持仓快照，不代表真实持仓（GET /api/paper/positions）
                </p>
              </div>
              <span className="text-xs text-slate-400">{positions.length} 只</span>
            </div>
            <PaperPositionsTable positions={positions} />
          </Card>

          {/* 最近虚拟交易 */}
          <Card className="mb-6 overflow-hidden">
            <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-100 px-4 py-3.5 sm:px-5">
              <div>
                <h2 className="text-sm font-semibold text-slate-800">最近虚拟交易</h2>
                <p className="mt-0.5 text-xs text-slate-400">
                  策略信号触发的虚拟成交，不发生真实资金划转（GET /api/paper/trades）
                </p>
              </div>
              <span className="text-xs text-slate-400">{trades.length} 笔</span>
            </div>
            <PaperTradesTable trades={trades} />
          </Card>

          {/* 最新五档信号 */}
          <Card className="mb-6 px-4 py-5 sm:px-5">
            <div className="mb-4">
              <h2 className="text-sm font-semibold text-slate-800">最新五档信号</h2>
              <p className="mt-0.5 text-xs text-slate-400">
                驱动虚拟调仓的五档模型信号（GET /api/paper/signals）
              </p>
            </div>
            <PaperSignalsPanel signals={signals} />
          </Card>

          <p className="text-xs text-slate-400">
            本页面所有数据均为模拟（虚拟）记录，由规则信号自动驱动，不涉及真实资金、真实下单或任何交易指令，不构成投资建议。
          </p>
        </>
      )}
    </>
  );
}
