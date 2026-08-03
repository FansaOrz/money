"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, peekApiCache } from "@/lib/api";
import {
  fmtDate,
  fmtMoney,
  fmtPercent,
  normalizeBacktest,
  normalizeBacktestV2,
  normalizeQuantFunds,
  normalizeQuantPortfolio,
  normalizeSignalsV2,
  normalizeSnapshot,
  normalizeValidation,
  normalizeWalkForward,
  signClass,
  toNumber,
  type BacktestView,
  type BacktestV2CurvePointView,
  type BacktestV2View,
  type QuantFundView,
  type QuantPortfolioView,
  type SignalsV2View,
  type SnapshotView,
  type ValidationView,
  type WalkForwardView,
} from "@/lib/normalize";
import { Card, EmptyState, ErrorState, PageHeader, Spinner } from "@/components/ui";
import { FundLink } from "@/components/FundLink";
import { MetricLabel } from "@/components/KnowledgeLink";

type StrategyKey = "buy_hold" | "ma_cross" | "grid" | "dca" | "macd";

const STRATEGIES: { key: StrategyKey; label: string; desc: string }[] = [
  { key: "buy_hold", label: "买入持有", desc: "期初一次性买入并持有至期末" },
  { key: "ma_cross", label: "均线择时", desc: "净值短均线上穿长均线买入、下穿卖出" },
  { key: "grid", label: "网格交易", desc: "按固定价格网格分批低吸高抛" },
  { key: "dca", label: "定投", desc: "按固定交易日间隔分批投入" },
  { key: "macd", label: "MACD", desc: "MACD 金叉买入、死叉卖出" },
];

const API_DOWN_HINT =
  "量化接口暂不可用。该功能依赖后端 /api/quant/* 接口，当前后端尚未上线该模块。";

/** V2 配置方法的中文标签 */
const ALLOCATION_METHOD_LABELS: Record<string, string> = {
  hrp: "HRP 分层风险平价",
  inverse_vol: "逆波动率",
  equal_weight: "等权",
  frozen: "冻结沿用",
};

function fmtAllocationMethod(method: string): string {
  if (!method || method === "—") return "—";
  return method
    .split("+")
    .map((part) => ALLOCATION_METHOD_LABELS[part.trim()] ?? part.trim())
    .join(" + ");
}

function MetricCard({
  label,
  term,
  value,
  sub,
  valueClass = "text-slate-900",
}: {
  label: string;
  term?: string;
  value: string;
  sub?: string;
  valueClass?: string;
}) {
  return (
    <Card className="px-4 py-4 sm:px-5">
      <p className="text-xs text-slate-500">
        <MetricLabel term={term}>{label}</MetricLabel>
      </p>
      <p className={`mt-1.5 text-xl font-semibold tabular-nums sm:text-2xl ${valueClass}`}>
        {value}
      </p>
      {sub && <p className="mt-1 text-xs text-slate-400">{sub}</p>}
    </Card>
  );
}

/* ---------- 轻量 SVG 回测曲线 ---------- */

function BacktestChart({ view }: { view: BacktestView }) {
  const points = view.points;
  const chart = useMemo(() => {
    if (points.length < 2) return null;
    const values = points.flatMap((p) =>
      p.benchmark !== null ? [p.nav, p.benchmark] : [p.nav]
    );
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || Math.max(Math.abs(max) * 0.01, 0.0001);
    const width = 560;
    const height = 170;
    const left = 30;
    const top = 16;
    const xFor = (i: number) => left + (i / (points.length - 1)) * width;
    const yFor = (v: number) => top + (1 - (v - min) / span) * height;

    const lineOf = (get: (p: (typeof points)[number]) => number | null) => {
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

    const navLine = lineOf((p) => p.nav);
    const benchLine = lineOf((p) => p.benchmark);
    const areaPath = `${navLine} L${xFor(points.length - 1).toFixed(1)},${top + height} L${left},${top + height} Z`;
    const yLabels = [0, 1, 2, 3].map((i) => (max - (span * i) / 3).toFixed(3));
    return { navLine, benchLine, areaPath, yLabels, width, height, left, top };
  }, [points]);

  if (!chart || points.length < 2) {
    return (
      <EmptyState title="回测结果中没有可绘制的净值曲线" hint="接口已返回，但曲线数据为空或不足两个点。" />
    );
  }

  const first = points[0];
  const latest = points[points.length - 1];
  const hasBenchmark = points.some((p) => p.benchmark !== null);
  const gradientId = `btFill-${view.fundCode || "x"}-${view.strategyName}`;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs text-slate-500">
            期末净值（{fmtDate(latest.date)}）
          </p>
          <p className="text-2xl font-semibold tabular-nums text-slate-900">
            {fmtMoney(latest.nav, 4)}
          </p>
        </div>
        <div className="flex items-center gap-4 text-xs text-slate-500">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-5 bg-blue-600" /> 策略净值
          </span>
          {hasBenchmark && (
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-0.5 w-5 bg-slate-400" /> 基准
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
        <path d={chart.areaPath} fill={`url(#${gradientId})`} />
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

/* ---------- 策略/基准双净值曲线（V2 回测、统计验证共用） ---------- */

function DualCurveChart<P extends { date: string }>({
  points,
  getStrategy,
  getBenchmark,
  gradientId,
  endLabel,
  endValue,
  benchmarkLabel,
}: {
  points: P[];
  getStrategy: (p: P) => number;
  getBenchmark: (p: P) => number | null;
  gradientId: string;
  endLabel: string;
  endValue: number;
  benchmarkLabel: string;
}) {
  const chart = useMemo(() => {
    if (points.length < 2) return null;
    const values = points.flatMap((p) => {
      const b = getBenchmark(p);
      return b !== null ? [getStrategy(p), b] : [getStrategy(p)];
    });
    // getStrategy/getBenchmark 由父组件以 useCallback 固定引用
    // eslint-disable-next-line react-hooks/exhaustive-deps
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || Math.max(Math.abs(max) * 0.01, 0.0001);
    const width = 560;
    const height = 170;
    const left = 30;
    const top = 16;
    const xFor = (i: number) => left + (i / (points.length - 1)) * width;
    const yFor = (v: number) => top + (1 - (v - min) / span) * height;

    const lineOf = (get: (p: P) => number | null) => {
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

    const navLine = lineOf((p) => getStrategy(p));
    const benchLine = lineOf((p) => getBenchmark(p));
    const areaPath = `${navLine} L${xFor(points.length - 1).toFixed(1)},${top + height} L${left},${top + height} Z`;
    const yLabels = [0, 1, 2, 3].map((i) => (max - (span * i) / 3).toFixed(3));
    return { navLine, benchLine, areaPath, yLabels, width, height, left, top };
  }, [points, getStrategy, getBenchmark]);

  if (!chart || points.length < 2) {
    return (
      <EmptyState title="结果中没有可绘制的净值曲线" hint="接口已返回，但曲线数据为空或不足两个点。" />
    );
  }

  const first = points[0];
  const latest = points[points.length - 1];
  const hasBenchmark = points.some((p) => getBenchmark(p) !== null);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs text-slate-500">{endLabel}（{fmtDate(latest.date)}）</p>
          <p className="text-2xl font-semibold tabular-nums text-slate-900">
            {fmtMoney(endValue, 4)}
          </p>
        </div>
        <div className="flex items-center gap-4 text-xs text-slate-500">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-5 bg-blue-600" /> 策略净值
          </span>
          {hasBenchmark && (
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-0.5 w-5 bg-slate-400" /> {benchmarkLabel}
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
        <path d={chart.areaPath} fill={`url(#${gradientId})`} />
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

/* ---------- 回测面板 ---------- */

function BacktestPanel({ funds }: { funds: QuantFundView[] }) {
  const [fundCode, setFundCode] = useState<string>("");
  const [strategy, setStrategy] = useState<StrategyKey>("buy_hold");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestView | null>(null);
  const requestId = useRef(0);

  useEffect(() => {
    if (!fundCode && funds.length > 0) {
      setFundCode(funds[0].code);
    }
  }, [funds, fundCode]);

  const run = useCallback(async () => {
    if (!fundCode) return;
    const id = ++requestId.current;
    setRunning(true);
    setError(null);
    try {
      const raw = await api.quantBacktest({ code: fundCode, strategy });
      if (id !== requestId.current) return;
      setResult(normalizeBacktest(raw));
    } catch (e) {
      if (id !== requestId.current) return;
      setResult(null);
      setError(
        e instanceof ApiError
          ? `${e.message}。${e.status === 404 ? API_DOWN_HINT : ""}`
          : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      if (id === requestId.current) setRunning(false);
    }
  }, [fundCode, strategy]);

  const selectedStrategy = STRATEGIES.find((s) => s.key === strategy);

  return (
    <div className="grid gap-6 lg:grid-cols-[300px_1fr]">
      {/* 左侧：参数选择 */}
      <div className="space-y-4">
        <div>
          <label htmlFor="bt-fund" className="mb-1.5 block text-xs font-medium text-slate-500">
            回测基金
          </label>
          <select
            id="bt-fund"
            value={fundCode}
            onChange={(e) => setFundCode(e.target.value)}
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-slate-500 focus:outline-none"
          >
            {funds.length === 0 && <option value="">（先加载基金指标）</option>}
            {funds.map((f) => (
              <option key={f.key} value={f.code}>
                {f.name}（{f.code}）
              </option>
            ))}
          </select>
        </div>
        <div>
          <p className="mb-1.5 text-xs font-medium text-slate-500">回测策略</p>
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-1">
            {STRATEGIES.map((s) => (
              <button
                key={s.key}
                type="button"
                onClick={() => setStrategy(s.key)}
                className={`rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
                  strategy === s.key
                    ? "border-slate-900 bg-slate-900 text-white"
                    : "border-slate-200 bg-white text-slate-700 hover:border-slate-400"
                }`}
              >
                <span className="block font-medium">{s.label}</span>
                <span
                  className={`mt-0.5 block text-xs ${
                    strategy === s.key ? "text-slate-300" : "text-slate-400"
                  }`}
                >
                  {s.desc}
                </span>
              </button>
            ))}
          </div>
        </div>
        <button
          type="button"
          onClick={run}
          disabled={running || !fundCode}
          className="w-full rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running ? "回测运行中…" : "开始回测"}
        </button>
      </div>

      {/* 右侧：结果 */}
      <div className="min-w-0">
        {running ? (
          <Spinner label={`正在对 ${fundCode} 运行${selectedStrategy?.label ?? ""}回测…`} />
        ) : error ? (
          <ErrorState message={error} onRetry={run} />
        ) : !result ? (
          <EmptyState
            title="尚未运行回测"
            hint="选择基金与策略后点击「开始回测」，将调用 POST /api/quant/backtest 并绘制净值曲线。"
          />
        ) : (
          <div className="space-y-5">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h3 className="text-sm font-semibold text-slate-800">
                <FundLink
                  code={result.fundCode}
                  name={result.fundName}
                  className="font-semibold text-slate-800 hover:text-blue-700 hover:underline"
                />
                {result.fundCode ? `（${result.fundCode}）` : ""} · {result.strategyName || "回测结果"}
              </h3>
              {(result.startDate || result.endDate) && (
                <p className="text-xs text-slate-400">
                  {fmtDate(result.startDate)} ~ {fmtDate(result.endDate)}
                </p>
              )}
            </div>

            {result.warnings.length > 0 && (
              <div className="rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs text-amber-800">
                {result.warnings.map((w, i) => (
                  <p key={i}>{w}</p>
                ))}
              </div>
            )}

            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {(
                [
                  ["累计收益", "total-return", fmtPercent(result.summary.totalReturn), signClass(result.summary.totalReturn)],
                  ["年化收益", "annualized-return", fmtPercent(result.summary.annualizedReturn), signClass(result.summary.annualizedReturn)],
                  ["最大回撤", "max-drawdown", fmtPercent(result.summary.maxDrawdown), "text-slate-800"],
                  ["夏普比率", "sharpe-ratio", fmtNumberOr(result.summary.sharpeRatio), "text-slate-800"],
                  ["年化波动率", "annual-volatility", fmtPercent(result.summary.annualizedVolatility), "text-slate-800"],
                  ["基准收益", "b0-benchmark", fmtPercent(result.summary.benchmarkReturn), signClass(result.summary.benchmarkReturn)],
                  ["超额收益", "excess-return", fmtPercent(result.summary.excessReturn), signClass(result.summary.excessReturn)],
                  ["交易次数", undefined, result.summary.trades === null ? "—" : String(toNumber(result.summary.trades) ?? result.summary.trades), "text-slate-800"],
                ] as [string, string | undefined, string, string][]
              ).map(([label, term, value, cls]) => (
                <div key={label} className="rounded-lg bg-slate-50 px-3 py-2.5">
                  <p className="text-xs text-slate-500">
                    <MetricLabel term={term}>{label}</MetricLabel>
                  </p>
                  <p className={`mt-0.5 text-sm font-semibold tabular-nums ${cls}`}>{value}</p>
                </div>
              ))}
            </div>

            <BacktestChart view={result} />
          </div>
        )}
      </div>
    </div>
  );
}

function fmtNumberOr(v: unknown, digits = 2): string {
  const n = toNumber(v);
  if (n === null) return "—";
  return n.toFixed(digits);
}

/* ---------- Walk-Forward 面板 ---------- */

const WF_API_DOWN_HINT =
  "Walk-Forward 接口暂不可用。该功能依赖后端 POST /api/quant/walkforward。";

function WalkForwardChart({ points }: { points: WalkForwardView["points"] }) {
  const chart = useMemo(() => {
    if (points.length < 2) return null;
    const values = points.flatMap((p) =>
      p.benchmark !== null ? [p.nav, p.benchmark] : [p.nav]
    );
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || Math.max(Math.abs(max) * 0.01, 0.0001);
    const width = 560;
    const height = 170;
    const left = 30;
    const top = 16;
    const xFor = (i: number) => left + (i / (points.length - 1)) * width;
    const yFor = (v: number) => top + (1 - (v - min) / span) * height;

    const lineOf = (get: (p: (typeof points)[number]) => number | null) => {
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

    const navLine = lineOf((p) => p.nav);
    const benchLine = lineOf((p) => p.benchmark);
    const areaPath = `${navLine} L${xFor(points.length - 1).toFixed(1)},${top + height} L${left},${top + height} Z`;
    const yLabels = [0, 1, 2, 3].map((i) => (max - (span * i) / 3).toFixed(3));
    return { navLine, benchLine, areaPath, yLabels, width, height, left, top };
  }, [points]);

  if (!chart || points.length < 2) {
    return (
      <EmptyState title="结果中没有可绘制的净值曲线" hint="接口已返回，但曲线数据为空或不足两个点。" />
    );
  }

  const first = points[0];
  const latest = points[points.length - 1];
  const hasBenchmark = points.some((p) => p.benchmark !== null);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs text-slate-500">期末净值（{fmtDate(latest.date)}）</p>
          <p className="text-2xl font-semibold tabular-nums text-slate-900">
            {fmtMoney(latest.nav, 4)}
          </p>
        </div>
        <div className="flex items-center gap-4 text-xs text-slate-500">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-5 bg-blue-600" /> 策略净值
          </span>
          {hasBenchmark && (
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-0.5 w-5 bg-slate-400" /> 等权基准
            </span>
          )}
        </div>
      </div>
      <svg viewBox="0 0 600 220" className="h-56 w-full overflow-visible sm:h-64">
        <defs>
          <linearGradient id="wfFill" x1="0" x2="0" y1="0" y2="1">
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
        <path d={chart.areaPath} fill="url(#wfFill)" />
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

function WalkForwardPanel() {
  const [trainWindow, setTrainWindow] = useState("120");
  const [testWindow, setTestWindow] = useState("20");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<WalkForwardView | null>(null);
  const requestId = useRef(0);

  const trainNum = useMemo(() => {
    const n = Number.parseInt(trainWindow, 10);
    return Number.isFinite(n) && n > 0 ? n : null;
  }, [trainWindow]);
  const testNum = useMemo(() => {
    const n = Number.parseInt(testWindow, 10);
    return Number.isFinite(n) && n > 0 ? n : null;
  }, [testWindow]);

  const run = useCallback(async () => {
    if (trainNum === null || testNum === null) return;
    const id = ++requestId.current;
    setRunning(true);
    setError(null);
    try {
      const raw = await api.quantWalkForward({
        train_window: trainNum,
        test_window: testNum,
      });
      if (id !== requestId.current) return;
      setResult(normalizeWalkForward(raw));
    } catch (e) {
      if (id !== requestId.current) return;
      setResult(null);
      setError(
        e instanceof ApiError
          ? `${e.message}。${e.status === 404 ? WF_API_DOWN_HINT : ""}`
          : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      if (id === requestId.current) setRunning(false);
    }
  }, [trainNum, testNum]);

  return (
    <div className="grid gap-6 lg:grid-cols-[300px_1fr]">
      {/* 左侧：参数 */}
      <div className="space-y-4">
        <div>
          <label htmlFor="wf-train" className="mb-1.5 block text-xs font-medium text-slate-500">
            训练窗口（交易日）
          </label>
          <input
            id="wf-train"
            type="number"
            min={1}
            inputMode="numeric"
            value={trainWindow}
            onChange={(e) => setTrainWindow(e.target.value)}
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
          />
          {trainNum === null && (
            <p className="mt-1 text-xs text-rose-600">请输入大于 0 的整数</p>
          )}
        </div>
        <div>
          <label htmlFor="wf-test" className="mb-1.5 block text-xs font-medium text-slate-500">
            测试窗口（交易日）
          </label>
          <input
            id="wf-test"
            type="number"
            min={1}
            inputMode="numeric"
            value={testWindow}
            onChange={(e) => setTestWindow(e.target.value)}
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
          />
          {testNum === null && (
            <p className="mt-1 text-xs text-rose-600">请输入大于 0 的整数</p>
          )}
        </div>
        <p className="rounded-lg bg-slate-100 px-3 py-2 text-xs leading-relaxed text-slate-500">
          每 {trainWindow ?? "…"} 个交易日训练一次模型，随后 {testWindow ?? "…"} 个交易日样本外运行，
          滚动拼接成完整净值曲线，与等权基准对比。
        </p>
        <button
          type="button"
          onClick={run}
          disabled={running || trainNum === null || testNum === null}
          className="w-full rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running ? "运行中…" : "运行 Walk-Forward"}
        </button>
      </div>

      {/* 右侧：结果 */}
      <div className="min-w-0">
        {running ? (
          <Spinner label={`正在运行 Walk-Forward（训练 ${trainWindow} / 测试 ${testWindow}）…`} />
        ) : error ? (
          <ErrorState message={error} onRetry={run} />
        ) : !result ? (
          <EmptyState
            title="尚未运行 Walk-Forward"
            hint="设置训练/测试窗口后点击「运行 Walk-Forward」，将调用 POST /api/quant/walkforward。"
          />
        ) : (
          <div className="space-y-5">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h3 className="text-sm font-semibold text-slate-800">
                规则模型 Walk-Forward · 训练 {result.trainWindow ?? trainNum} / 测试{" "}
                {result.testWindow ?? testNum}
              </h3>
              <p className="text-xs text-slate-400">{result.segments.length} 个滚动窗口段</p>
            </div>

            {result.warnings.length > 0 && (
              <div className="rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs text-amber-800">
                {result.warnings.map((w, i) => (
                  <p key={i}>{w}</p>
                ))}
              </div>
            )}

            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {(
                [
                  ["年化收益", "annualized-return", fmtPercent(result.summary.annualizedReturn), signClass(result.summary.annualizedReturn)],
                  ["最大回撤", "max-drawdown", fmtPercent(result.summary.maxDrawdown), "text-slate-800"],
                  ["夏普比率", "sharpe-ratio", fmtNumberOr(result.summary.sharpeRatio), "text-slate-800"],
                  ["胜率", "win-rate", fmtPercent(result.summary.winRate), "text-slate-800"],
                  ["换手率", "turnover", fmtPercent(result.summary.turnover), "text-slate-800"],
                  ["超额收益", "excess-return", fmtPercent(result.summary.excessReturn), signClass(result.summary.excessReturn)],
                  ["等权基准年化", "equal-weight", fmtPercent(result.summary.benchmarkAnnualizedReturn), signClass(result.summary.benchmarkAnnualizedReturn)],
                  ["滚动段数", "walk-forward", String(result.segments.length), "text-slate-800"],
                ] as [string, string | undefined, string, string][]
              ).map(([label, term, value, cls]) => (
                <div key={label} className="rounded-lg bg-slate-50 px-3 py-2.5">
                  <p className="text-xs text-slate-500">
                    <MetricLabel term={term}>{label}</MetricLabel>
                  </p>
                  <p className={`mt-0.5 text-sm font-semibold tabular-nums ${cls}`}>{value}</p>
                </div>
              ))}
            </div>

            <WalkForwardChart points={result.points} />

            {result.segments.length > 0 && (
              <div className="overflow-x-auto rounded-lg border border-slate-100">
                <table className="w-full min-w-[720px] text-sm">
                  <thead>
                    <tr className="bg-slate-50 text-left text-xs text-slate-500">
                      <th className="px-4 py-2.5 font-medium">段</th>
                      <th className="px-4 py-2.5 font-medium">训练区间</th>
                      <th className="px-4 py-2.5 font-medium">测试区间</th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="total-return">测试收益</MetricLabel></th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="b0-benchmark">基准收益</MetricLabel></th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="excess-return">超额</MetricLabel></th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="sharpe-ratio">夏普</MetricLabel></th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.segments.map((s) => (
                      <tr key={s.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                        <td className="px-4 py-3 tabular-nums text-slate-600">#{s.index}</td>
                        <td className="px-4 py-3 text-xs text-slate-500">
                          {fmtDate(s.trainStart)} ~ {fmtDate(s.trainEnd)}
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-500">
                          {fmtDate(s.testStart)} ~ {fmtDate(s.testEnd)}
                          {s.holdings.length > 0 && (
                            <p className="mt-0.5 max-w-[220px] truncate text-slate-400" title={s.holdings.join("、")}>
                              持仓：{s.holdings.join("、")}
                            </p>
                          )}
                        </td>
                        <td className={`px-4 py-3 text-right tabular-nums ${signClass(s.testReturn)}`}>
                          {fmtPercent(s.testReturn)}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                          {fmtPercent(s.benchmarkReturn)}
                        </td>
                        <td className={`px-4 py-3 text-right tabular-nums ${signClass(s.excessReturn)}`}>
                          {fmtPercent(s.excessReturn)}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                          {fmtNumberOr(s.sharpeRatio)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* ---------- 通用小组件 ---------- */

/** 灰底指标小格（与回测/验证板块共用） */
function MiniStat({
  label,
  term,
  value,
  valueClass = "text-slate-800",
  sub,
}: {
  label: string;
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
  if (warnings.length === 0) return null;
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs text-amber-800">
      {warnings.map((w, i) => (
        <p key={i}>{w}</p>
      ))}
    </div>
  );
}

/** 五档（低→高）平均前瞻收益柱状图 */
function QuintileBarChart({ values }: { values: (number | null)[] }) {
  const items = values.map((v, i) => ({ label: `Q${i + 1}`, value: v }));
  const finite = items.filter((it) => it.value !== null) as { label: string; value: number }[];
  if (finite.length === 0) {
    return <p className="text-xs text-slate-400">暂无五档收益数据。</p>;
  }
  const maxAbs = Math.max(...finite.map((it) => Math.abs(it.value)), 0.0001);
  const H = 110;
  const MID = H / 2;
  return (
    <div>
      <svg viewBox={`0 0 ${items.length * 64} ${H + 22}`} className="h-32 w-full">
        <line x1="0" x2={items.length * 64} y1={MID} y2={MID} stroke="#e2e8f0" />
        {items.map((it, i) => {
          const x = i * 64 + 14;
          if (it.value === null) {
            return (
              <text key={it.label} x={x + 12} y={MID - 4} fontSize="9" fill="#94a3b8">
                —
              </text>
            );
          }
          const h = Math.max((Math.abs(it.value) / maxAbs) * (H / 2 - 8), 1.5);
          const positive = it.value >= 0;
          return (
            <g key={it.label}>
              <rect
                x={x}
                y={positive ? MID - h : MID}
                width="36"
                height={h}
                rx="2"
                fill={positive ? "#e11d48" : "#059669"}
                fillOpacity="0.85"
              />
              <text x={x + 18} y={positive ? MID - h - 4 : MID + h + 10} fontSize="9" textAnchor="middle" fill="#475569">
                {fmtPercent(it.value)}
              </text>
            </g>
          );
        })}
        {items.map((it, i) => (
          <text key={`l-${it.label}`} x={i * 64 + 32} y={H + 16} fontSize="10" textAnchor="middle" fill="#64748b">
            {it.label}
          </text>
        ))}
      </svg>
      <p className="mt-1 text-xs text-slate-400">按综合分五档分组（低→高）的平均前瞻收益；单调递增说明因子排序有效。</p>
    </div>
  );
}

/* ---------- 稳健策略 V2：当期信号 ---------- */

function SignalsV2Panel() {
  const [topN, setTopN] = useState("8");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SignalsV2View | null>(null);
  const requestId = useRef(0);

  const topNNum = useMemo(() => {
    const n = Number.parseInt(topN, 10);
    return Number.isFinite(n) && n >= 1 && n <= 30 ? n : null;
  }, [topN]);

  const run = useCallback(async () => {
    if (topNNum === null) return;
    const id = ++requestId.current;
    setRunning(true);
    setError(null);
    try {
      const raw = await api.quantV2Signals({ topN: topNNum });
      if (id !== requestId.current) return;
      setResult(normalizeSignalsV2(raw));
    } catch (e) {
      if (id !== requestId.current) return;
      setResult(null);
      setError(
        e instanceof ApiError
          ? `${e.message}。${e.status === 404 ? "V2 信号接口暂不可用，依赖后端 GET /api/quant/v2/signals。" : ""}`
          : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      if (id === requestId.current) setRunning(false);
    }
  }, [topNNum]);

  useEffect(() => {
    void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label htmlFor="v2-signal-topn" className="mb-1.5 block text-xs font-medium text-slate-500">
            入选只数上限 top_n
          </label>
          <input
            id="v2-signal-topn"
            type="number"
            min={1}
            max={30}
            inputMode="numeric"
            value={topN}
            onChange={(e) => setTopN(e.target.value)}
            className="w-28 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={run}
          disabled={running || topNNum === null}
          className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running ? "刷新中…" : "刷新当期信号"}
        </button>
      </div>

      {running ? (
        <Spinner label="正在计算当期 V2 信号…" />
      ) : error ? (
        <ErrorState message={error} onRetry={run} />
      ) : !result ? (
        <EmptyState title="暂无当期信号" hint="GET /api/quant/v2/signals 返回为空。" />
      ) : (
        <div className="space-y-5">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <MiniStat label="信号基准日" value={fmtDate(result.asOf)} sub={result.tradeDate ? `预计成交 ${fmtDate(result.tradeDate)}` : undefined} />
            <MiniStat label="现金权重" term="cash-weight" value={fmtPercent(result.cashWeight)} sub="含波动目标降仓与约束截断" />
            <MiniStat
              label="波动目标仓位系数"
              term="vol-target-scalar"
              value={result.volScalar === null ? "—" : `${(result.volScalar * 100).toFixed(1)}%`}
              sub={result.realizedVol !== null ? `EWMA60 年化波动 ${fmtPercent(result.realizedVol)}` : "1 为满仓"}
            />
            <MiniStat
              label="冻结状态"
              term="freeze-rule"
              value={result.frozen ? "已冻结" : "正常"}
              valueClass={result.frozen ? "text-amber-700" : "text-slate-800"}
              sub={result.frozen ? result.freezeReason ?? "高波动+急反弹，沿用持仓" : "未触发冻结条件"}
            />
            <MiniStat label="候选基金数" value={result.candidateCount === null ? "—" : String(result.candidateCount)} />
            <MiniStat label="通过动量过滤" term="momentum-12-1" value={result.eligibleCount === null ? "—" : String(result.eligibleCount)} sub="绝对动量 12-1 > 0" />
          </div>

          <WarningsBlock warnings={result.warnings} />

          {result.selected.length === 0 ? (
            <EmptyState
              title="本期无入选基金"
              hint="全部候选未通过绝对动量过滤或被约束剔除，资金以现金形式持有。"
            />
          ) : (
            <div className="overflow-x-auto rounded-lg border border-slate-100">
              <table className="w-full min-w-[860px] text-sm">
                <thead>
                  <tr className="bg-slate-50 text-left text-xs text-slate-500">
                    <th className="px-4 py-2.5 font-medium">基金</th>
                    <th className="px-4 py-2.5 font-medium">市场层</th>
                    <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="weight-caps">目标权重</MetricLabel></th>
                    <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="momentum-12-1">动量 12-1</MetricLabel></th>
                    <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="relative-rank">层内排名</MetricLabel></th>
                    <th className="px-4 py-2.5 font-medium">入选理由</th>
                  </tr>
                </thead>
                <tbody>
                  {result.selected.map((s) => (
                    <tr key={s.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                      <td className="px-4 py-3">
                        <FundLink code={s.code} name={s.name} className="block font-medium text-slate-800 hover:text-blue-700 hover:underline" />
                        <p className="text-xs text-slate-400">
                          {s.code}
                          {s.family && s.family !== s.code ? ` · ${s.family}` : ""}
                        </p>
                      </td>
                      <td className="px-4 py-3 text-xs text-slate-600">{s.market}</td>
                      <td className="px-4 py-3 text-right font-semibold tabular-nums text-slate-800">
                        {fmtPercent(s.weight)}
                      </td>
                      <td className={`px-4 py-3 text-right tabular-nums ${signClass(s.momentum121)}`}>
                        {fmtPercent(s.momentum121)}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                        {s.rankInMarket === null ? "—" : `#${s.rankInMarket}`}
                        {s.marketCandidates !== null && (
                          <span className="text-xs text-slate-400"> / {s.marketCandidates}</span>
                        )}
                      </td>
                      <td className="max-w-[280px] px-4 py-3 text-xs text-slate-500">
                        {s.reasons.length > 0 ? s.reasons.join("；") : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- 稳健策略 V2：回测 ---------- */

function BacktestV2Panel({ funds }: { funds: QuantFundView[] }) {
  const [topN, setTopN] = useState("8");
  const [intervalMonths, setIntervalMonths] = useState("1");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestV2View | null>(null);
  const requestId = useRef(0);

  const topNNum = useMemo(() => {
    const n = Number.parseInt(topN, 10);
    return Number.isFinite(n) && n >= 1 && n <= 30 ? n : null;
  }, [topN]);
  const intervalNum = useMemo(() => {
    const n = Number.parseInt(intervalMonths, 10);
    return Number.isFinite(n) && n >= 1 && n <= 12 ? n : null;
  }, [intervalMonths]);

  const getStrategy = useCallback((p: BacktestV2CurvePointView) => p.strategy, []);
  const getBenchmark = useCallback((p: BacktestV2CurvePointView) => p.benchmark, []);

  const run = useCallback(async () => {
    if (topNNum === null || intervalNum === null) return;
    const id = ++requestId.current;
    setRunning(true);
    setError(null);
    try {
      const raw = await api.quantV2Backtest({
        top_n: topNNum,
        rebalance_interval_months: intervalNum,
      });
      if (id !== requestId.current) return;
      setResult(normalizeBacktestV2(raw));
    } catch (e) {
      if (id !== requestId.current) return;
      setResult(null);
      setError(
        e instanceof ApiError
          ? `${e.message}。${e.status === 404 ? "V2 回测接口暂不可用，依赖后端 POST /api/quant/v2/backtest。" : ""}`
          : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      if (id === requestId.current) setRunning(false);
    }
  }, [topNNum, intervalNum]);

  const s = result?.strategy ?? null;
  const b = result?.benchmark ?? null;

  return (
    <div className="grid gap-6 lg:grid-cols-[300px_1fr]">
      {/* 左侧：参数 */}
      <div className="space-y-4">
        <div>
          <label htmlFor="v2-topn" className="mb-1.5 block text-xs font-medium text-slate-500">
            入选只数上限 top_n（1-30）
          </label>
          <input
            id="v2-topn"
            type="number"
            min={1}
            max={30}
            inputMode="numeric"
            value={topN}
            onChange={(e) => setTopN(e.target.value)}
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
          />
          {topNNum === null && <p className="mt-1 text-xs text-rose-600">请输入 1-30 的整数</p>}
        </div>
        <div>
          <label htmlFor="v2-interval" className="mb-1.5 block text-xs font-medium text-slate-500">
            调仓间隔（月，1-12）
          </label>
          <input
            id="v2-interval"
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
        <p className="rounded-lg bg-slate-100 px-3 py-2 text-xs leading-relaxed text-slate-500">
          候选池缺省为当前持仓基金（{funds.length} 只）。每月末打分：绝对动量 12-1 &gt; 0、
          同家族份额去重、层内前 30% 入选；层内 HRP 配置（失败回退逆波动/等权），
          单基金 8% / 家族 10% / QDII 30% 约束；EWMA60 波动目标 10% 只降仓，超出部分计入现金；
          信号 T+1 成交（QDII T+2）。费用模型默认零费用。
        </p>
        <button
          type="button"
          onClick={run}
          disabled={running || topNNum === null || intervalNum === null}
          className="w-full rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running ? "回测运行中…" : "运行 V2 回测"}
        </button>
      </div>

      {/* 右侧：结果 */}
      <div className="min-w-0">
        {running ? (
          <Spinner label="正在运行稳健策略 V2 月频回测…" />
        ) : error ? (
          <ErrorState message={error} onRetry={run} />
        ) : !result ? (
          <EmptyState
            title="尚未运行 V2 回测"
            hint="点击「运行 V2 回测」调用 POST /api/quant/v2/backtest，对比策略与候选池等权基准。"
          />
        ) : (
          <div className="space-y-5">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h3 className="text-sm font-semibold text-slate-800">
                稳健策略 V2 · top_n {topNNum} · 每 {intervalNum} 月调仓
              </h3>
              {(result.startDate || result.endDate) && (
                <p className="text-xs text-slate-400">
                  {fmtDate(result.startDate)} ~ {fmtDate(result.endDate)}
                </p>
              )}
            </div>

            <WarningsBlock warnings={result.warnings} />

            {/* 策略 vs 基准 */}
            <div className="overflow-x-auto rounded-lg border border-slate-100">
              <table className="w-full min-w-[560px] text-sm">
                <thead>
                  <tr className="bg-slate-50 text-left text-xs text-slate-500">
                    <th className="px-4 py-2.5 font-medium">指标</th>
                    <th className="px-4 py-2.5 text-right font-medium">策略</th>
                    <th className="px-4 py-2.5 text-right font-medium">基准（候选池等权）</th>
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
                    ] as [string, string | undefined, string, string, string][]
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
              <MiniStat label="超额收益（总）" term="excess-return" value={fmtPercent(result.excessReturn)} valueClass={signClass(result.excessReturn)} sub="策略总收益 − 基准总收益" />
              <MiniStat label="平均每次调仓换手" term="turnover" value={fmtPercent(result.avgTurnover)} sub="Σ|目标−漂移|/2" />
              <MiniStat label="累计费用" term="transaction-cost" value={fmtMoney(result.totalFees)} sub="默认零费用模型" />
              <MiniStat
                label="调仓 / 冻结次数"
                term="freeze-rule"
                value={`${result.rebalanceCount ?? "—"} / ${result.frozenCount ?? "—"}`}
                sub="冻结期沿用持仓、无成交"
              />
            </div>

            <DualCurveChart
              points={result.curve}
              getStrategy={getStrategy}
              getBenchmark={getBenchmark}
              gradientId="btv2Fill"
              endLabel="期末策略净值"
              endValue={result.curve.length > 0 ? result.curve[result.curve.length - 1].strategy : Number.NaN}
              benchmarkLabel="等权基准"
            />

            {/* 调仓明细 */}
            {result.rebalances.length > 0 && (
              <div className="overflow-x-auto rounded-lg border border-slate-100">
                <table className="w-full min-w-[860px] text-sm">
                  <thead>
                    <tr className="bg-slate-50 text-left text-xs text-slate-500">
                      <th className="px-4 py-2.5 font-medium">#</th>
                      <th className="px-4 py-2.5 font-medium">信号日 → 成交日</th>
                      <th className="px-4 py-2.5 font-medium"><MetricLabel term="hrp">配置方法</MetricLabel></th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="cash-weight">现金权重</MetricLabel></th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="vol-target-scalar">仓位系数</MetricLabel></th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="turnover">换手</MetricLabel></th>
                      <th className="px-4 py-2.5 font-medium">本期持仓（权重）</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.rebalances.map((r) => (
                      <tr key={r.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                        <td className="px-4 py-3 tabular-nums text-slate-600">{r.index}</td>
                        <td className="px-4 py-3 text-xs text-slate-500">
                          {fmtDate(r.signalDate)} → {fmtDate(r.fillDate)}
                          {r.frozen && (
                            <span className="ml-1.5 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800">
                              冻结
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-600">
                          {fmtAllocationMethod(r.allocationMethod)}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-800">
                          {fmtPercent(r.cashWeight)}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                          {r.volScalar === null ? "—" : `${(r.volScalar * 100).toFixed(0)}%`}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                          {fmtPercent(r.turnover)}
                        </td>
                        <td className="max-w-[260px] px-4 py-3 text-xs text-slate-500">
                          {r.holdings.length > 0 ? (
                            <span
                              title={r.holdings.map((h) => `${h.code} ${(h.weight * 100).toFixed(1)}%`).join("、")}
                            >
                              {r.holdings
                                .slice(0, 4)
                                .map((h) => `${h.code} ${(h.weight * 100).toFixed(1)}%`)
                                .join("、")}
                              {r.holdings.length > 4 ? ` 等 ${r.holdings.length} 只` : ""}
                            </span>
                          ) : (
                            "空仓（全现金）"
                          )}
                          {r.reason && <p className="mt-0.5 text-slate-400">{r.reason}</p>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

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

/* ---------- 统计验证面板 ---------- */

function ValidationPanel() {
  const [trainWindow, setTrainWindow] = useState("120");
  const [testWindow, setTestWindow] = useState("20");
  const [topN, setTopN] = useState("3");
  const [trialCount, setTrialCount] = useState("1");
  const [includeCosts, setIncludeCosts] = useState(true);
  const [asOf, setAsOf] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ValidationView | null>(null);
  const [snapshot, setSnapshot] = useState<SnapshotView | null>(null);
  const requestId = useRef(0);

  useEffect(() => {
    let cancelled = false;
    api
      .quantSnapshot()
      .then((raw) => {
        if (!cancelled) setSnapshot(normalizeSnapshot(raw));
      })
      .catch(() => {
        /* 快照仅作辅助信息，失败时静默 */
      });
    return () => {
      cancelled = true;
    };
  }, []);

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
    return Number.isFinite(n) && n >= 1 && n <= 20 ? n : null;
  }, [topN]);
  const trialNum = useMemo(() => {
    const n = Number.parseInt(trialCount, 10);
    return Number.isFinite(n) && n >= 1 && n <= 10000 ? n : null;
  }, [trialCount]);
  const asOfValid = asOf === "" || /^\d{4}-\d{2}-\d{2}$/.test(asOf);

  const run = useCallback(async () => {
    if (trainNum === null || testNum === null || topNNum === null || trialNum === null || !asOfValid) return;
    const id = ++requestId.current;
    setRunning(true);
    setError(null);
    try {
      const raw = await api.quantValidation({
        as_of: asOf || null,
        window: { train_window: trainNum, test_window: testNum, step: testNum },
        top_n: topNNum,
        include_costs: includeCosts,
        trial_count: trialNum,
      });
      if (id !== requestId.current) return;
      setResult(normalizeValidation(raw));
    } catch (e) {
      if (id !== requestId.current) return;
      setResult(null);
      setError(
        e instanceof ApiError
          ? `${e.message}。${e.status === 404 ? "统计验证接口暂不可用，依赖后端 POST /api/quant/validation。" : ""}`
          : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      if (id === requestId.current) setRunning(false);
    }
  }, [trainNum, testNum, topNNum, trialNum, includeCosts, asOf, asOfValid]);

  return (
    <div className="grid gap-6 lg:grid-cols-[300px_1fr]">
      {/* 左侧：参数 + as_of 快照 */}
      <div className="space-y-4">
        <div>
          <label htmlFor="va-asof" className="mb-1.5 block text-xs font-medium text-slate-500">
            as_of 快照基准日（可留空）
          </label>
          <input
            id="va-asof"
            type="date"
            value={asOf}
            onChange={(e) => setAsOf(e.target.value)}
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
          />
          <p className="mt-1 text-xs text-slate-400">
            指定后仅用当时可见数据（QDII lag2 / 国内 lag1），复现历史任一日研究视角。
          </p>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label htmlFor="va-train" className="mb-1.5 block text-xs font-medium text-slate-500">
              训练窗口
            </label>
            <input
              id="va-train"
              type="number"
              min={1}
              inputMode="numeric"
              value={trainWindow}
              onChange={(e) => setTrainWindow(e.target.value)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
            />
          </div>
          <div>
            <label htmlFor="va-test" className="mb-1.5 block text-xs font-medium text-slate-500">
              测试窗口
            </label>
            <input
              id="va-test"
              type="number"
              min={1}
              inputMode="numeric"
              value={testWindow}
              onChange={(e) => setTestWindow(e.target.value)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
            />
          </div>
          <div>
            <label htmlFor="va-topn" className="mb-1.5 block text-xs font-medium text-slate-500">
              每期入选 top_n
            </label>
            <input
              id="va-topn"
              type="number"
              min={1}
              max={20}
              inputMode="numeric"
              value={topN}
              onChange={(e) => setTopN(e.target.value)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-800 focus:border-slate-500 focus:outline-none"
            />
          </div>
          <div>
            <label htmlFor="va-trials" className="mb-1.5 block text-xs font-medium text-slate-500">
              历史试验数 trial_count
            </label>
            <input
              id="va-trials"
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
          <p className="text-xs text-rose-600">请检查窗口/top_n/trial_count 是否为合法正整数</p>
        )}
        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={includeCosts}
            onChange={(e) => setIncludeCosts(e.target.checked)}
            className="h-4 w-4 rounded border-slate-300"
          />
          样本外扣除交易费用（买 0.15%、卖 0.5%、7 日内 1.5%）
        </label>
        <button
          type="button"
          onClick={run}
          disabled={running || trainNum === null || testNum === null || topNNum === null || trialNum === null || !asOfValid}
          className="w-full rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running ? "验证运行中…" : "运行统计验证"}
        </button>

        {/* as_of 数据可用性快照（GET /api/quant/snapshot） */}
        {snapshot && (
          <div className="rounded-lg border border-slate-200 px-3 py-2.5">
            <p className="text-xs font-medium text-slate-600">
              数据可用性快照 · 基准日 {fmtDate(snapshot.asOf)}
            </p>
            <p className="mt-0.5 text-xs text-slate-400">
              可用交易日 {snapshot.tradeDayCount ?? "—"} 天
              {snapshot.truncated ? "（列表已截断，保留尾部）" : ""}
            </p>
            <ul className="mt-2 max-h-44 space-y-1 overflow-y-auto">
              {snapshot.funds.map((f) => (
                <li key={f.key} className="flex items-baseline justify-between gap-2 text-xs">
                  <span className="min-w-0 truncate text-slate-600" title={`${f.name}（${f.code}）`}>
                    <FundLink
                      code={f.code}
                      name={f.name}
                      className="text-slate-600 hover:text-blue-700 hover:underline"
                      title={`查看基金详情：${f.name}（${f.code}）`}
                    />
                    {f.isQdii && (
                      <span className="ml-1 rounded bg-sky-100 px-1 py-0.5 text-[10px] text-sky-700">QDII</span>
                    )}
                  </span>
                  <span className="shrink-0 tabular-nums text-slate-400">
                    lag{f.lagDays ?? "—"} · 有效至 {fmtDate(f.effectiveDate)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* 右侧：结果 */}
      <div className="min-w-0">
        {running ? (
          <Spinner label="正在运行样本外统计验证（含 bootstrap，可能需数十秒）…" />
        ) : error ? (
          <ErrorState message={error} onRetry={run} />
        ) : !result ? (
          <EmptyState
            title="尚未运行统计验证"
            hint="设置参数后点击「运行统计验证」，将调用 POST /api/quant/validation，输出样本外风险、因子有效性与稳健性检验。"
          />
        ) : (
          <div className="space-y-6">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h3 className="text-sm font-semibold text-slate-800">
                验证结果 · as_of {fmtDate(result.asOf)}
              </h3>
              <p className="text-xs text-slate-400">
                样本外 {fmtDate(result.startDate)} ~ {fmtDate(result.endDate)} ·
                样本 {result.sampleCount ?? "—"} 天 / 样本外 {result.oosCount ?? "—"} 天 ·
                候选 {result.candidateCodes.length} 只
              </p>
            </div>

            <WarningsBlock warnings={result.warnings} />

            {/* 样本外风险：策略 vs 基准 */}
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
                      ] as [string, string | undefined, string, string, string][]
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
                <MiniStat
                  label="信息比率 IR"
                  term="information-ratio"
                  value={fmtNumberOr(result.informationRatio)}
                  sub="主动收益均值 / 跟踪误差 × √252"
                />
                <MiniStat
                  label="超额收益（总）"
                  term="excess-return"
                  value={fmtPercent(result.excessReturn)}
                  valueClass={signClass(result.excessReturn)}
                  sub="策略总收益 − 基准总收益"
                />
                <MiniStat
                  label="CVaR95 口径"
                  term="cvar95"
                  value="最差 5% 日收益均值"
                  sub="尾部风险；越接近 0 越好"
                />
              </div>
            </section>

            {/* 因子预测有效性 */}
            <section>
              <h4 className="mb-2 text-xs font-semibold text-slate-600">因子预测有效性</h4>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <MiniStat
                  label="Rank IC 均值"
                  term="rank-ic"
                  value={fmtNumberOr(result.predictiveness.rankIcMean, 3)}
                  valueClass={signClass(result.predictiveness.rankIcMean)}
                  sub={`参与 ${result.predictiveness.rankIcCount ?? 0} 期（Spearman）`}
                />
                <MiniStat
                  label="五档收益差 Q5−Q1"
                  term="quintile-spread"
                  value={fmtPercent(result.predictiveness.quintileSpread)}
                  valueClass={signClass(result.predictiveness.quintileSpread)}
                />
                <MiniStat
                  label="组序 Kendall tau"
                  term="kendall-tau"
                  value={fmtNumberOr(result.predictiveness.quintileKendallTau, 3)}
                  valueClass={signClass(result.predictiveness.quintileKendallTau)}
                />
                <MiniStat
                  label="五档严格单调递增"
                  term="quintile-spread"
                  value={result.predictiveness.quintileMonotonic ? "是" : "否"}
                  valueClass={result.predictiveness.quintileMonotonic ? "text-emerald-700" : "text-amber-700"}
                />
              </div>
              <div className="mt-3">
                <QuintileBarChart values={result.predictiveness.quintileReturns} />
              </div>
            </section>

            {/* 稳健性检验 */}
            <section>
              <h4 className="mb-2 text-xs font-semibold text-slate-600">多重检验与抽样稳健性</h4>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <MiniStat
                  label="Deflated Sharpe（DSR）"
                  term="deflated-sharpe"
                  value={fmtNumberOr(result.robustness.deflatedSharpe, 3)}
                  sub={`P(观测夏普 > 期望最大夏普)；试验数 ${result.robustness.trialCount ?? 1}`}
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
                  sub={`White Reality Check · ${result.robustness.bootstrapResamples ?? "—"} 次 block bootstrap（块长 ${result.robustness.blockLength ?? "—"}）`}
                />
                <MiniStat
                  label="收益偏度 γ3 / 峰度 γ4"
                  term="deflated-sharpe"
                  value={`${fmtNumberOr(result.robustness.skew)} / ${fmtNumberOr(result.robustness.kurtosis)}`}
                  sub="DSR 修正输入；正态峰度为 0"
                />
                <MiniStat
                  label="夏普标准误 / 期望最大夏普"
                  term="deflated-sharpe"
                  value={`${fmtNumberOr(result.robustness.sharpeStd)} / ${fmtNumberOr(result.robustness.expectedMaxSharpe)}`}
                />
              </div>
            </section>

            {/* 参数邻域稳定性 */}
            <section>
              <h4 className="mb-2 text-xs font-semibold text-slate-600">参数邻域稳定性</h4>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <MiniStat label="中心参数夏普" term="sharpe-ratio" value={fmtNumberOr(result.neighborhood.centerSharpe)} />
                <MiniStat
                  label="邻域经验分位数"
                  term="neighborhood-stability"
                  value={
                    result.neighborhood.neighborhoodQuantile === null
                      ? "—"
                      : `${(result.neighborhood.neighborhoodQuantile * 100).toFixed(0)}%`
                  }
                  sub="中心夏普在邻域中的分位，越高越稳健"
                />
                <MiniStat
                  label="邻域夏普带"
                  term="neighborhood-stability"
                  value={
                    result.neighborhood.bandLow === null || result.neighborhood.bandHigh === null
                      ? "—"
                      : `[${fmtNumberOr(result.neighborhood.bandLow)}, ${fmtNumberOr(result.neighborhood.bandHigh)}]`
                  }
                  sub="去除极端各一后的上下限"
                />
                <MiniStat label="邻域参数点数" term="neighborhood-stability" value={result.neighborhood.neighborCount === null ? "—" : String(result.neighborhood.neighborCount)} sub="top_n/调仓间隔 ±1 与因子权重扰动" />
              </div>
              {result.neighborhood.neighbors.length > 0 && (
                <div className="mt-3 overflow-x-auto rounded-lg border border-slate-100">
                  <table className="w-full min-w-[480px] text-sm">
                    <thead>
                      <tr className="bg-slate-50 text-left text-xs text-slate-500">
                        <th className="px-4 py-2.5 font-medium">邻域参数点</th>
                        <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="sharpe-ratio">样本外夏普</MetricLabel></th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.neighborhood.neighbors.map((n) => (
                        <tr key={n.label} className="border-t border-slate-100 hover:bg-slate-50/60">
                          <td className="px-4 py-2.5 text-xs text-slate-600">{n.label}</td>
                          <td className="px-4 py-2.5 text-right tabular-nums text-slate-800">
                            {fmtNumberOr(n.sharpe)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            {/* 成本 */}
            <section>
              <h4 className="mb-2 text-xs font-semibold text-slate-600">交易成本</h4>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <MiniStat
                  label="费用口径"
                  term="transaction-cost"
                  value={result.costs.includeCosts ? "已扣费" : "未扣费"}
                  sub={`买 ${fmtPercent(result.costs.buyFeeRate)} / 卖 ${fmtPercent(result.costs.sellFeeRate)} / ${result.costs.shortTermDays ?? 7} 日内 ${fmtPercent(result.costs.shortTermSellFeeRate)}`}
                />
                <MiniStat
                  label="累计扣费占初始净值"
                  term="transaction-cost"
                  value={fmtPercent(result.costs.totalFeeRatio)}
                  sub={`发生扣费交易 ${result.costs.tradeDays ?? 0} 天`}
                />
                <MiniStat
                  label="卖出费率依据"
                  term="fifo-lot"
                  value={
                    result.costs.sellFeeBasis === "lots"
                      ? "真实流水 lot 持有期（FIFO）"
                      : result.costs.sellFeeBasis === "default"
                        ? "默认费率（无流水）"
                        : result.costs.sellFeeBasis ?? "—"
                  }
                />
              </div>
            </section>

            {/* 数据可用性 */}
            {result.fundSnapshots.length > 0 && (
              <section>
                <h4 className="mb-2 text-xs font-semibold text-slate-600">as_of 视角下各基金数据可用性</h4>
                <div className="overflow-x-auto rounded-lg border border-slate-100">
                  <table className="w-full min-w-[640px] text-sm">
                    <thead>
                      <tr className="bg-slate-50 text-left text-xs text-slate-500">
                        <th className="px-4 py-2.5 font-medium">基金</th>
                        <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="lag-days">数据滞后</MetricLabel></th>
                        <th className="px-4 py-2.5 text-right font-medium">最新净值日</th>
                        <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="pit-as-of">as_of 有效净值日</MetricLabel></th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.fundSnapshots.map((f) => (
                        <tr key={f.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                          <td className="px-4 py-2.5">
                            <FundLink code={f.code} name={f.name} />
                            <span className="ml-1.5 text-xs text-slate-400">{f.code}</span>
                            {f.isQdii && (
                              <span className="ml-1.5 rounded bg-sky-100 px-1 py-0.5 text-[10px] text-sky-700">QDII</span>
                            )}
                          </td>
                          <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">
                            T+{f.lagDays ?? "—"}
                          </td>
                          <td className="px-4 py-2.5 text-right tabular-nums text-slate-600">
                            {fmtDate(f.latestNavDate)}
                          </td>
                          <td className="px-4 py-2.5 text-right tabular-nums text-slate-800">
                            {fmtDate(f.effectiveDate)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}

            {result.methodology && (
              <p className="rounded-lg bg-slate-100 px-3 py-2 text-xs leading-relaxed text-slate-500">
                验证方法：{result.methodology}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* ---------- 量化分析主页面 ---------- */

export default function QuantPage() {
  const cachedPortfolio = normalizeQuantPortfolio(
    peekApiCache<Parameters<typeof normalizeQuantPortfolio>[0]>("/api/quant/portfolio")
  );
  const cachedFunds = normalizeQuantFunds(
    peekApiCache<Parameters<typeof normalizeQuantFunds>[0]>("/api/quant/funds")
  );
  const [portfolio, setPortfolio] = useState<QuantPortfolioView | null>(cachedPortfolio);
  const [funds, setFunds] = useState<QuantFundView[]>(cachedFunds);
  const [loading, setLoading] = useState(!cachedPortfolio && cachedFunds.length === 0);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [portfolioRaw, fundsRaw] = await Promise.allSettled([
        api.quantPortfolio(),
        api.quantFunds(),
      ]);
      if (portfolioRaw.status === "fulfilled") {
        setPortfolio(normalizeQuantPortfolio(portfolioRaw.value));
        setLoading(false);
      }
      if (fundsRaw.status === "fulfilled") {
        setFunds(normalizeQuantFunds(fundsRaw.value));
        // 基金列表通常比组合汇总更早返回，先展示可用内容，不必等全部请求。
        setLoading(false);
      }
      if (portfolioRaw.status === "rejected" && fundsRaw.status === "rejected") {
        const reason = portfolioRaw.reason;
        throw reason instanceof Error ? reason : new Error(String(reason));
      }
    } catch (e) {
      setError(
        e instanceof ApiError ? `${e.message}。${API_DOWN_HINT}` : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <>
      <PageHeader
        title="量化分析"
        description="组合与单基金的量化指标，以及基于历史净值的策略回测"
      />

      {loading ? (
        <Card>
          <Spinner label="正在加载量化数据…" />
        </Card>
      ) : error ? (
        <Card>
          <ErrorState message={error} onRetry={load} />
        </Card>
      ) : !portfolio && funds.length === 0 ? (
        <Card>
          <EmptyState
            title="暂无量化数据"
            hint="后端量化接口已连通，但当前没有可展示的指标数据。"
            action={
              <button
                type="button"
                onClick={load}
                className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
              >
                刷新
              </button>
            }
          />
        </Card>
      ) : (
        <>
          {/* 组合指标 */}
          <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
            <MetricCard
              label="年化收益率"
              term="annualized-return"
              value={fmtPercent(portfolio?.annualizedReturn)}
              valueClass={signClass(portfolio?.annualizedReturn)}
              sub={portfolio?.asOf ? `截至 ${fmtDate(portfolio.asOf)}` : undefined}
            />
            <MetricCard
              label="最大回撤"
              term="max-drawdown"
              value={fmtPercent(portfolio?.maxDrawdown)}
              sub="历史净值高点到低点的最大跌幅"
            />
            <MetricCard
              label="夏普比率"
              term="sharpe-ratio"
              value={fmtNumberOr(portfolio?.sharpeRatio)}
              sub="单位风险带来的超额收益"
            />
            <MetricCard
              label="年化波动率"
              term="annual-volatility"
              value={fmtPercent(portfolio?.annualizedVolatility)}
              sub="组合收益的波动程度"
            />
            <MetricCard
              label="组合总市值（元）"
              value={`¥${fmtMoney(portfolio?.totalMarketValue)}`}
              sub={`持仓基金 ${portfolio?.positionCount ?? funds.length} 只`}
            />
            <MetricCard
              label="累计收益率"
              term="total-return"
              value={fmtPercent(portfolio?.totalReturnRate)}
              valueClass={signClass(portfolio?.totalReturnRate)}
            />
            <MetricCard
              label="胜率"
              term="win-rate"
              value={fmtPercent(portfolio?.winRate)}
              sub="盈利交易日占比"
            />
            <MetricCard
              label="第一大基金权重"
              term="weight-caps"
              value={fmtPercent(portfolio?.concentrationTop1)}
              sub={`前三大合计 ${fmtPercent(portfolio?.concentrationTop3)}`}
            />
          </div>
          {portfolio?.methodology && (
            <p className="mt-3 rounded-lg bg-slate-100 px-3 py-2 text-xs leading-relaxed text-slate-500">
              指标口径：{portfolio.methodology}
            </p>
          )}

          {/* 基金指标表 */}
          <Card className="mt-6 overflow-hidden">
            <div className="px-4 py-4 sm:px-5">
              <h2 className="text-sm font-semibold text-slate-800">基金量化指标</h2>
              <p className="mt-0.5 text-xs text-slate-400">来自 GET /api/quant/funds</p>
            </div>
            {funds.length === 0 ? (
              <EmptyState title="暂无基金指标" hint="GET /api/quant/funds 返回为空。" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[820px] text-sm">
                  <thead>
                    <tr className="border-t border-slate-100 bg-slate-50 text-left text-xs text-slate-500">
                      <th className="px-4 py-2.5 font-medium sm:px-5">基金</th>
                      <th className="px-4 py-2.5 font-medium">当前建议</th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="total-return">近一年收益（含分红）</MetricLabel></th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="annual-volatility">年化波动率</MetricLabel></th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="max-drawdown">最大回撤</MetricLabel></th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="sharpe-ratio">夏普比率</MetricLabel></th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="win-rate">胜率</MetricLabel></th>
                      <th className="px-4 py-2.5 text-right font-medium"><MetricLabel term="total-return">持有收益率</MetricLabel></th>
                      <th className="px-4 py-2.5 text-right font-medium sm:px-5">市值（元）</th>
                    </tr>
                  </thead>
                  <tbody>
                    {funds.map((f) => (
                      <tr key={f.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                        <td className="px-4 py-3 sm:px-5">
                          <FundLink code={f.code} name={f.name} className="block font-medium text-slate-800 hover:text-blue-700 hover:underline" />
                          <p className="text-xs text-slate-400">{f.code}</p>
                        </td>
                        <td className="px-4 py-3">
                          {f.adviceLabel ? (
                            <span
                              className={`rounded-full px-2 py-1 text-xs font-medium ${
                                f.adviceAction === "add"
                                  ? "bg-rose-50 text-rose-700"
                                  : f.adviceAction === "hold"
                                    ? "bg-blue-50 text-blue-700"
                                    : f.adviceAction === "watch"
                                      ? "bg-slate-100 text-slate-600"
                                      : "bg-emerald-50 text-emerald-700"
                              }`}
                            >
                              {f.adviceLabel}
                            </span>
                          ) : "—"}
                        </td>
                        <td className={`px-4 py-3 text-right tabular-nums ${signClass(f.oneYearReturn)}`}>
                          {fmtPercent(f.oneYearReturn)}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                          {fmtPercent(f.annualizedVolatility)}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                          {fmtPercent(f.maxDrawdown)}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                          {fmtNumberOr(f.sharpeRatio)}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                          {fmtPercent(f.winRate)}
                        </td>
                        <td className={`px-4 py-3 text-right tabular-nums ${signClass(f.returnRate)}`}>
                          {fmtPercent(f.returnRate)}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-800 sm:px-5">
                          {fmtMoney(f.marketValue)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          {/* 回测 */}
          <Card className="mt-6 px-4 py-5 sm:px-5">
            <div className="mb-5">
              <h2 className="text-sm font-semibold text-slate-800">策略回测</h2>
              <p className="mt-0.5 text-xs text-slate-400">
                POST /api/quant/backtest · 基于历史净值的轻量回测，结果不构成投资建议
              </p>
            </div>
            <BacktestPanel funds={funds} />
          </Card>

          {/* Walk-Forward */}
          <Card className="mt-6 px-4 py-5 sm:px-5">
            <div className="mb-5">
              <h2 className="text-sm font-semibold text-slate-800">Walk-Forward 滚动验证</h2>
              <p className="mt-0.5 text-xs text-slate-400">
                POST /api/quant/walkforward · 规则模型样本外滚动回测，对比等权基准，结果不构成投资建议
              </p>
            </div>
            <WalkForwardPanel />
          </Card>

          {/* 稳健策略 V2 */}
          <Card className="mt-6 px-4 py-5 sm:px-5">
            <div className="mb-5">
              <h2 className="text-sm font-semibold text-slate-800">稳健策略 V2</h2>
              <p className="mt-0.5 text-xs text-slate-400">
                月频双动量（绝对动量 12-1 &gt; 0 + 层内相对排名）· 层内 HRP / 逆波动 / 等权配置 ·
                波动率目标 10%（只降仓，超出计现金）· T+1 成交（QDII T+2）
              </p>
            </div>
            <div className="mb-5 rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs leading-relaxed text-amber-800">
              仅为研究信号与历史回测：若下方统计验证未通过（DSR 不显著、WRC p 值偏高、
              五档非单调或邻域稳定性差），<span className="font-semibold">该策略不得用于任何真实投资建议</span>；
              所有目标权重与回测结果均不构成投资建议，也不会产生任何实盘下单。
            </div>

            <section>
              <h3 className="mb-3 text-xs font-semibold text-slate-600">
                当期信号
                <span className="ml-2 font-normal text-slate-400">GET /api/quant/v2/signals</span>
              </h3>
              <SignalsV2Panel />
            </section>

            <hr className="my-6 border-slate-100" />

            <section>
              <h3 className="mb-3 text-xs font-semibold text-slate-600">
                策略回测
                <span className="ml-2 font-normal text-slate-400">POST /api/quant/v2/backtest · 对比候选池等权基准</span>
              </h3>
              <BacktestV2Panel funds={funds} />
            </section>
          </Card>

          {/* 统计验证 */}
          <Card className="mt-6 px-4 py-5 sm:px-5">
            <div className="mb-5">
              <h2 className="text-sm font-semibold text-slate-800">统计验证</h2>
              <p className="mt-0.5 text-xs text-slate-400">
                POST /api/quant/validation · as_of 快照下 walk-forward 样本外验证：
                CVaR95 / Calmar / IR / Rank IC / 五档单调性 / DSR / WRC p 值 / 邻域稳定性 / 交易成本；
                仅为研究验证，不构成投资建议
              </p>
            </div>
            <ValidationPanel />
          </Card>
        </>
      )}
    </>
  );
}
