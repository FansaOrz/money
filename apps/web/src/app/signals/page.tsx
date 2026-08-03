"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import {
  buildTierModel,
  fmtDate,
  normalizeScreenerSignals,
  normalizeSignals,
  toNumber,
  type ScreenerDirection,
  type ScreenerScore,
  type ScreenerSignalView,
  type SignalSeverity,
  type SignalView,
} from "@/lib/normalize";
import type { ScreenerMeta, ScreenerSignal } from "@/lib/types";
import { Card, EmptyState, ErrorState, PageHeader, Spinner } from "@/components/ui";
import { FundLink } from "@/components/FundLink";
import { KnowledgeLink, MetricLabel } from "@/components/KnowledgeLink";

const SEVERITY_META: Record<
  SignalSeverity,
  { label: string; dot: string; badge: string; ring: string; order: number }
> = {
  high: {
    label: "高优先级",
    dot: "bg-rose-500",
    badge: "bg-rose-50 text-rose-700 border-rose-200",
    ring: "border-l-rose-500",
    order: 0,
  },
  medium: {
    label: "中优先级",
    dot: "bg-amber-500",
    badge: "bg-amber-50 text-amber-700 border-amber-200",
    ring: "border-l-amber-500",
    order: 1,
  },
  low: {
    label: "低优先级",
    dot: "bg-sky-500",
    badge: "bg-sky-50 text-sky-700 border-sky-200",
    ring: "border-l-sky-500",
    order: 2,
  },
};

const API_DOWN_HINT =
  "信号接口暂不可用。该功能依赖后端 GET /api/quant/signals，当前后端尚未上线该模块。";

const SCREENER_API_DOWN_HINT =
  "规则模型接口暂不可用。该功能依赖后端 GET /api/quant/screener/signals。";

/** 信号指标键 -> 中文标签与知识词条 slug */
const SIGNAL_METRIC_META: Record<string, { label: string; term?: string }> = {
  drawdown: { label: "回撤", term: "max-drawdown" },
  max_drawdown: { label: "最大回撤", term: "max-drawdown" },
  return_rate: { label: "收益率", term: "total-return" },
  return_1m: { label: "近 1 月收益", term: "total-return" },
  return_3m: { label: "近 3 月收益", term: "total-return" },
  return_1y: { label: "近 1 年收益", term: "total-return" },
  annualized_return: { label: "年化收益", term: "annualized-return" },
  volatility: { label: "波动率", term: "annual-volatility" },
  annual_volatility: { label: "年化波动率", term: "annual-volatility" },
  annualized_volatility: { label: "年化波动率", term: "annual-volatility" },
  sharpe: { label: "夏普比率", term: "sharpe-ratio" },
  sharpe_ratio: { label: "夏普比率", term: "sharpe-ratio" },
  sortino: { label: "索提诺比率", term: "sortino-ratio" },
  cvar95: { label: "CVaR95", term: "cvar95" },
  win_rate: { label: "胜率", term: "win-rate" },
  momentum: { label: "动量", term: "momentum-score" },
  momentum_12_1: { label: "动量（12-1）", term: "momentum-12-1" },
  quantile: { label: "同类分位数", term: "quantile" },
  score: { label: "综合分", term: "composite-score" },
  composite_score: { label: "综合分", term: "composite-score" },
  ma20: { label: "20 日均线", term: "trend-strength" },
  nav: { label: "最新净值" },
  rsi: { label: "RSI" },
};

function metricLabel(key: string): string {
  return SIGNAL_METRIC_META[key]?.label ?? key;
}

function MetricChips({ metrics }: { metrics: Record<string, unknown> }) {
  const entries = Object.entries(metrics).filter(([, v]) => toNumber(v) !== null);
  if (entries.length === 0) return null;
  return (
    <div className="mt-2.5 flex flex-wrap gap-1.5">
      {entries.slice(0, 6).map(([k, v]) => {
        const n = toNumber(v);
        return (
          <span
            key={k}
            className="rounded-md bg-slate-100 px-2 py-0.5 text-xs tabular-nums text-slate-600"
          >
            <MetricLabel term={SIGNAL_METRIC_META[k]?.term}>{metricLabel(k)}</MetricLabel>{" "}
            {n !== null && Math.abs(n) <= 1 && n !== 0 ? `${(n * 100).toFixed(2)}%` : String(v)}
          </span>
        );
      })}
    </div>
  );
}

function SignalCard({ signal }: { signal: SignalView }) {
  const meta = SEVERITY_META[signal.severity];
  return (
    <div
      className={`rounded-xl border border-slate-200 border-l-4 bg-white p-4 shadow-sm sm:p-5 ${meta.ring}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${meta.badge}`}>
              {meta.label}
            </span>
            <span className="text-sm font-semibold text-slate-900">{signal.signal}</span>
          </div>
          <p className="mt-1.5 text-sm text-slate-700">
            <FundLink
              code={signal.fundCode}
              name={
                <>
                  {signal.fundName}
                  {signal.fundCode && (
                    <span className="ml-1.5 text-xs font-normal text-slate-400 no-underline">
                      {signal.fundCode}
                    </span>
                  )}
                </>
              }
              className="font-medium text-slate-800 hover:text-blue-700 hover:underline"
            />
          </p>
        </div>
        {signal.date && (
          <p className="shrink-0 text-xs text-slate-400">{fmtDate(signal.date)}</p>
        )}
      </div>

      <div className="mt-3 rounded-lg bg-slate-50 px-3 py-2.5">
        <p className="text-xs font-medium text-slate-500">
          触发规则：<span className="text-slate-700">{signal.rule}</span>
        </p>
        {signal.reason && (
          <p className="mt-1 text-sm leading-relaxed text-slate-700">{signal.reason}</p>
        )}
      </div>

      {signal.metrics && <MetricChips metrics={signal.metrics} />}
    </div>
  );
}

/* ==================== 规则信号 Tab（原有 GET /api/quant/signals） ==================== */

function RuleSignalsTab() {
  const [signals, setSignals] = useState<SignalView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [severityFilter, setSeverityFilter] = useState<SignalSeverity | "all">("all");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const raw = await api.quantSignals();
      setSignals(normalizeSignals(raw));
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

  const sorted = useMemo(
    () =>
      [...signals].sort(
        (a, b) => SEVERITY_META[a.severity].order - SEVERITY_META[b.severity].order
      ),
    [signals]
  );

  const filtered = useMemo(
    () =>
      severityFilter === "all"
        ? sorted
        : sorted.filter((s) => s.severity === severityFilter),
    [sorted, severityFilter]
  );

  const counts = useMemo(() => {
    const c: Record<SignalSeverity, number> = { high: 0, medium: 0, low: 0 };
    for (const s of signals) c[s.severity] += 1;
    return c;
  }, [signals]);

  return (
    <>
      {/* severity 过滤 */}
      <div className="mb-5 flex flex-wrap gap-2">
        {(
          [
            ["all", `全部（${signals.length}）`],
            ["high", `高（${counts.high}）`],
            ["medium", `中（${counts.medium}）`],
            ["low", `低（${counts.low}）`],
          ] as [SignalSeverity | "all", string][]
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setSeverityFilter(key)}
            className={`flex items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-sm font-medium transition-colors ${
              severityFilter === key
                ? "border-slate-900 bg-slate-900 text-white"
                : "border-slate-200 bg-white text-slate-600 hover:border-slate-400"
            }`}
          >
            {key !== "all" && (
              <span className={`h-2 w-2 rounded-full ${SEVERITY_META[key].dot}`} />
            )}
            {label}
          </button>
        ))}
      </div>

      {loading ? (
        <Card>
          <Spinner label="正在加载研究信号…" />
        </Card>
      ) : error ? (
        <Card>
          <ErrorState message={error} onRetry={load} />
        </Card>
      ) : signals.length === 0 ? (
        <Card>
          <EmptyState
            title="暂无研究信号"
            hint="GET /api/quant/signals 返回为空。信号由后端规则引擎定期生成。"
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
      ) : filtered.length === 0 ? (
        <Card>
          <EmptyState
            title={`当前没有${SEVERITY_META[severityFilter as SignalSeverity]?.label ?? ""}信号`}
            hint="切换上方筛选查看其他优先级的信号。"
          />
        </Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {filtered.map((s) => (
            <SignalCard key={s.key} signal={s} />
          ))}
        </div>
      )}
    </>
  );
}

/* ==================== 综合信号 Tab（GET /api/quant/screener/signals） ==================== */

const SCORE_META: Record<
  ScreenerScore,
  { label: string; badge: string; bar: string; order: number }
> = {
  "2": {
    label: "+2 值得研究加仓",
    badge: "bg-rose-50 text-rose-700 border-rose-200",
    bar: "bg-rose-500",
    order: 0,
  },
  "1": {
    label: "+1 偏积极",
    badge: "bg-orange-50 text-orange-700 border-orange-200",
    bar: "bg-orange-400",
    order: 1,
  },
  "0": {
    label: "0 中性",
    badge: "bg-slate-100 text-slate-600 border-slate-200",
    bar: "bg-slate-400",
    order: 2,
  },
  "-1": {
    label: "-1 偏谨慎",
    badge: "bg-emerald-50 text-emerald-700 border-emerald-200",
    bar: "bg-emerald-500",
    order: 3,
  },
  "-2": {
    label: "-2 值得研究减仓",
    badge: "bg-emerald-100 text-emerald-800 border-emerald-300",
    bar: "bg-emerald-600",
    order: 4,
  },
};

const SCORE_ORDER: ScreenerScore[] = [2, 1, 0, -1, -2];

const DIRECTION_FILTERS: { key: ScreenerDirection | "all"; label: string }[] = [
  { key: "all", label: "全部方向" },
  { key: "long", label: "做多" },
  { key: "short", label: "做空/回避" },
  { key: "neutral", label: "中性" },
];

/** 市场代码 → 中文标签（与后端 quant_factors.MARKET_LABELS 对齐） */
const MARKET_LABELS: Record<string, string> = {
  us_nasdaq: "美股·纳斯达克",
  us_spx: "美股·标普",
  hk_tech: "港股·恒生科技",
  hk: "港股",
  cn_300: "A股·沪深300",
  cn: "A股",
  gold: "黄金",
  bond: "债券",
  money: "货币",
  overseas: "其他海外",
};

function marketLabel(market: string): string {
  return MARKET_LABELS[market] ?? market;
}

function TargetBadge({ inTarget }: { inTarget: boolean }) {
  return inTarget ? (
    <span className="inline-flex items-center rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-xs font-medium text-indigo-700">
      目标组合
    </span>
  ) : (
    <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-xs font-medium text-slate-500">
      仅分析
    </span>
  );
}

function fmtPercentile(p: number | null): string {
  if (p === null) return "—";
  return `${p.toFixed(0)}%`;
}

function fmtWeight(w: number | null): string {
  if (w === null) return "—";
  const pct = Math.abs(w) <= 1 ? w * 100 : w;
  return `${pct.toFixed(1)}%`;
}

function ScoreBadge({ score }: { score: ScreenerScore }) {
  const meta = SCORE_META[score];
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-semibold tabular-nums ${meta.badge}`}
    >
      {score > 0 ? `+${score}` : score}
    </span>
  );
}

function ScreenerCard({ view }: { view: ScreenerSignalView }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <ScoreBadge score={view.score} />
            <TargetBadge inTarget={view.inTarget} />
            <FundLink
              code={view.fundCode}
              name={
                <>
                  {view.fundName}
                  {view.fundCode && view.fundCode !== "—" && (
                    <span className="ml-1.5 text-xs font-normal text-slate-400">{view.fundCode}</span>
                  )}
                </>
              }
              className="text-sm font-semibold text-slate-900 hover:text-blue-700 hover:underline"
            />
          </div>
          <p className="mt-1 text-xs text-slate-500">
            市场：<span className="text-slate-700">{marketLabel(view.market)}</span>
            <span className="mx-1.5 text-slate-300">·</span>
            <KnowledgeLink slug="quantile">分位数</KnowledgeLink>：
            <span className="tabular-nums text-slate-700">{fmtPercentile(view.percentile)}</span>
            <span className="mx-1.5 text-slate-300">·</span>
            <KnowledgeLink slug="weight-caps">目标权重</KnowledgeLink>：
            <span className="tabular-nums text-slate-700">{fmtWeight(view.targetWeight)}</span>
          </p>
        </div>
        {view.asOf && <p className="shrink-0 text-xs text-slate-400">{fmtDate(view.asOf)}</p>}
      </div>

      {/* 分数刻度 */}
      <div className="mt-3 flex items-center gap-1">
        {SCORE_ORDER.map((s) => (
          <span
            key={s}
            className={`h-1.5 flex-1 rounded-full ${
              SCORE_META[s].order <= SCORE_META[view.score].order && view.score !== 0
                ? SCORE_META[view.score].bar
                : s === 0 && view.score === 0
                  ? SCORE_META[0].bar
                  : "bg-slate-100"
            }`}
          />
        ))}
      </div>

      {(view.positiveFactors.length > 0 || view.negativeFactors.length > 0) && (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {view.positiveFactors.length > 0 && (
            <div className="rounded-lg bg-rose-50/70 px-3 py-2.5">
              <p className="text-xs font-medium text-rose-700">正贡献因子</p>
              <ul className="mt-1 space-y-0.5">
                {view.positiveFactors.slice(0, 5).map((f) => (
                  <li key={f.key} className="text-xs leading-relaxed text-slate-700">
                    {f.label}
                    {f.contribution !== null && (
                      <span className="ml-1 tabular-nums text-rose-600">
                        {f.contribution >= 0 ? "+" : ""}
                        {f.contribution.toFixed(2)}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {view.negativeFactors.length > 0 && (
            <div className="rounded-lg bg-emerald-50/70 px-3 py-2.5">
              <p className="text-xs font-medium text-emerald-700">负贡献因子</p>
              <ul className="mt-1 space-y-0.5">
                {view.negativeFactors.slice(0, 5).map((f) => (
                  <li key={f.key} className="text-xs leading-relaxed text-slate-700">
                    {f.label}
                    {f.contribution !== null && (
                      <span className="ml-1 tabular-nums text-emerald-600">
                        {f.contribution >= 0 ? "+" : ""}
                        {f.contribution.toFixed(2)}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {view.reasons.length > 0 && (
        <div className="mt-3 rounded-lg bg-slate-50 px-3 py-2.5">
          <p className="text-xs font-medium text-slate-500">评分原因</p>
          <ul className="mt-1 list-inside list-disc space-y-0.5">
            {view.reasons.slice(0, 4).map((r, i) => (
              <li key={i} className="text-xs leading-relaxed text-slate-700">
                {r}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ScreenerPanel({
  views,
  raws,
  meta,
  loading,
  error,
  onRetry,
  mode,
}: {
  views: ScreenerSignalView[];
  raws: ScreenerSignal[];
  meta: ScreenerMeta | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  mode: "composite" | "tiers";
}) {
  const [directionFilter, setDirectionFilter] = useState<ScreenerDirection | "all">("all");
  const [tierFilter, setTierFilter] = useState<number | "all">("all");
  const [marketFilter, setMarketFilter] = useState<string | "all">("all");
  const [targetFilter, setTargetFilter] = useState<"all" | "target" | "analysis">("all");
  const [keyword, setKeyword] = useState("");

  // 市场过滤基于代码（cn/us_nasdaq/...），与后端 market 字段一致
  const marketOptions = useMemo(() => {
    const present = new Set(views.map((v) => v.market));
    return Object.entries(MARKET_LABELS).filter(([code]) => present.has(code));
  }, [views]);

  const filtered = useMemo(() => {
    const q = keyword.trim().toLowerCase();
    return views.filter(
      (v) =>
        (directionFilter === "all" || v.direction === directionFilter) &&
        (marketFilter === "all" || v.market === marketFilter) &&
        (targetFilter === "all" ||
          (targetFilter === "target" ? v.inTarget : !v.inTarget)) &&
        (q === "" ||
          v.fundName.toLowerCase().includes(q) ||
          v.fundCode.toLowerCase().includes(q))
    );
  }, [views, directionFilter, marketFilter, targetFilter, keyword]);

  const groups = useMemo(() => {
    const g = new Map<ScreenerScore, ScreenerSignalView[]>();
    for (const s of SCORE_ORDER) g.set(s, []);
    for (const v of filtered) g.get(v.score)?.push(v);
    return SCORE_ORDER.map((score) => ({
      score,
      meta: SCORE_META[score],
      items: (g.get(score) ?? []).sort(
        (a, b) => (b.percentile ?? Number.NEGATIVE_INFINITY) - (a.percentile ?? Number.NEGATIVE_INFINITY)
      ),
    }));
  }, [filtered]);

  // 五档模型同样作用于过滤后的集合：raw 与 view 按键对齐
  const filteredRaws = useMemo(() => {
    const keys = new Set(filtered.map((v) => v.key));
    return raws.filter((r, i) => keys.has(String(r.fund_code ?? r.code ?? i)));
  }, [raws, filtered]);

  const allTiers = useMemo(
    () => buildTierModel(filtered, filteredRaws),
    [filtered, filteredRaws]
  );

  const tiers = useMemo(() => {
    if (tierFilter === "all") return allTiers;
    const t = allTiers.find((x) => x.tier === tierFilter);
    return t ? [t] : [];
  }, [allTiers, tierFilter]);

  const targetCount = useMemo(() => views.filter((v) => v.inTarget).length, [views]);

  if (loading) {
    return (
      <Card>
        <Spinner label="正在加载规则模型信号…" />
      </Card>
    );
  }
  if (error) {
    return (
      <Card>
        <ErrorState message={error} onRetry={onRetry} />
      </Card>
    );
  }
  if (views.length === 0) {
    return (
      <Card>
        <EmptyState
          title="暂无规则模型信号"
          hint="GET /api/quant/screener/signals 返回为空。模型按固定规则对候选标的打分。"
          action={
            <button
              type="button"
              onClick={onRetry}
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              刷新
            </button>
          }
        />
      </Card>
    );
  }

  const filterButtonClass = (active: boolean) =>
    `rounded-full border px-3.5 py-1.5 text-sm font-medium transition-colors ${
      active
        ? "border-slate-900 bg-slate-900 text-white"
        : "border-slate-200 bg-white text-slate-600 hover:border-slate-400"
    }`;

  const filtersBar = (
    <>
      {/* 基金名/代码搜索 + 市场过滤 + 组合归属过滤 */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          type="search"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="搜索基金名称或代码…"
          className="w-full rounded-lg border border-slate-200 bg-white px-3.5 py-1.5 text-sm text-slate-700 placeholder:text-slate-400 focus:border-slate-400 focus:outline-none sm:w-64"
        />
        {marketOptions.length > 1 && (
          <select
            value={marketFilter}
            onChange={(e) => setMarketFilter(e.target.value)}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-700 focus:border-slate-400 focus:outline-none"
          >
            <option value="all">全部市场</option>
            {marketOptions.map(([code, label]) => (
              <option key={code} value={code}>
                {label}
              </option>
            ))}
          </select>
        )}
        {(
          [
            ["all", `全部（${views.length}）`],
            ["target", `目标组合（${targetCount}）`],
            ["analysis", `仅分析（${views.length - targetCount}）`],
          ] as ["all" | "target" | "analysis", string][]
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTargetFilter(key)}
            className={filterButtonClass(targetFilter === key)}
          >
            {label}
          </button>
        ))}
      </div>
      {meta && (
        <p className="mb-5 text-xs text-slate-400">
          全部分析 {meta.selectedCount ?? views.length} 只 · 目标组合{" "}
          {meta.allocationCount ?? targetCount} 只
          {meta.excludedCount ? ` · 样本不足剔除 ${meta.excludedCount} 只` : ""}
          {meta.observeCount ? ` · 观察池 ${meta.observeCount} 只` : ""}
          {meta.asOf ? ` · 数据基准日 ${fmtDate(meta.asOf)}` : ""}
        </p>
      )}
    </>
  );

  if (mode === "composite") {
    return (
      <>
        {filtersBar}
        <div className="mb-5 flex flex-wrap gap-2">
          {DIRECTION_FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              onClick={() => setDirectionFilter(f.key)}
              className={filterButtonClass(directionFilter === f.key)}
            >
              {f.label}
              {f.key !== "all" && (
                <span className="ml-1 text-xs opacity-70">
                  {views.filter((v) => v.direction === f.key).length}
                </span>
              )}
            </button>
          ))}
        </div>

        {filtered.length === 0 ? (
          <Card>
            <EmptyState title="没有符合过滤条件的信号" hint="调整上方搜索词或过滤条件后重试。" />
          </Card>
        ) : (
          <div className="space-y-6">
            {groups
              .filter((g) => g.items.length > 0)
              .map((g) => (
                <section key={g.score}>
                  <div className="mb-3 flex items-center gap-2">
                    <span className={`h-2.5 w-2.5 rounded-full ${g.meta.bar}`} />
                    <h2 className="text-sm font-semibold text-slate-800">{g.meta.label}</h2>
                    <span className="text-xs text-slate-400">{g.items.length} 只</span>
                  </div>
                  <div className="grid gap-4 lg:grid-cols-2">
                    {g.items.map((v) => (
                      <ScreenerCard key={v.key} view={v} />
                    ))}
                  </div>
                </section>
              ))}
          </div>
        )}
      </>
    );
  }

  /* 五档模型 */
  return (
    <>
      {filtersBar}
      <div className="mb-5 flex flex-wrap gap-2">
        {(
          [
            ["all", "全部档位"],
            [5, "五档"],
            [4, "四档"],
            [3, "三档"],
            [2, "二档"],
            [1, "一档"],
          ] as [number | "all", string][]
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTierFilter(key)}
            className={filterButtonClass(tierFilter === key)}
          >
            {label}
            {key !== "all" && (
              <span className="ml-1 text-xs opacity-70">
                {allTiers.find((t) => t.tier === key)?.items.length ?? 0}
              </span>
            )}
          </button>
        ))}
      </div>

      <div className="space-y-5">
        {tiers.map((t) => {
          const totalWeight = t.items.reduce((acc, x) => acc + (x.targetWeight ?? 0), 0);
          return (
            <Card key={t.tier} className="overflow-hidden">
              <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-100 px-4 py-3.5 sm:px-5">
                <div>
                  <h2 className="text-sm font-semibold text-slate-800">{t.label}</h2>
                  <p className="mt-0.5 text-xs text-slate-400">
                    {t.weightHint}
                    {t.items.length > 0 && totalWeight > 0
                      ? ` · 合计目标权重 ${fmtWeight(Math.abs(totalWeight) <= 1 ? totalWeight : totalWeight / 100)}`
                      : ""}
                  </p>
                </div>
                <span className="text-xs text-slate-400">{t.items.length} 只</span>
              </div>
              {t.items.length === 0 ? (
                <p className="px-4 py-6 text-center text-xs text-slate-400 sm:px-5">
                  该档位暂无标的
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[640px] text-sm">
                    <thead>
                      <tr className="bg-slate-50 text-left text-xs text-slate-500">
                        <th className="px-4 py-2.5 font-medium sm:px-5">标的</th>
                        <th className="px-4 py-2.5 font-medium">市场</th>
                        <th className="px-4 py-2.5 text-center font-medium">
                          <KnowledgeLink slug="tier-score">分数</KnowledgeLink>
                        </th>
                        <th className="px-4 py-2.5 text-right font-medium">
                          <KnowledgeLink slug="quantile">分位数</KnowledgeLink>
                        </th>
                        <th className="px-4 py-2.5 text-center font-medium">组合</th>
                        <th className="px-4 py-2.5 text-right font-medium sm:px-5">
                          <KnowledgeLink slug="weight-caps">目标权重</KnowledgeLink>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {t.items.map((x) => (
                        <tr key={x.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                          <td className="px-4 py-3 sm:px-5">
                            <FundLink
                              code={x.fundCode}
                              name={x.fundName}
                              className="block font-medium text-slate-800 hover:text-blue-700 hover:underline"
                            />
                            <p className="text-xs text-slate-400">{x.fundCode}</p>
                          </td>
                          <td className="px-4 py-3 text-slate-600">{marketLabel(x.market)}</td>
                          <td className="px-4 py-3 text-center">
                            <ScoreBadge score={x.score} />
                          </td>
                          <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                            {fmtPercentile(x.percentile)}
                          </td>
                          <td className="px-4 py-3 text-center">
                            <TargetBadge inTarget={x.inTarget} />
                          </td>
                          <td className="px-4 py-3 text-right tabular-nums text-slate-800 sm:px-5">
                            {fmtWeight(x.targetWeight)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          );
        })}
      </div>
    </>
  );
}

/* ==================== 页面 ==================== */

type PageTab = "rules" | "composite" | "tiers";

const PAGE_TABS: { key: PageTab; label: string; desc: string }[] = [
  { key: "rules", label: "规则信号", desc: "GET /api/quant/signals" },
  { key: "composite", label: "综合信号", desc: "GET /api/quant/screener/signals" },
  { key: "tiers", label: "五档模型", desc: "按目标权重分档" },
];

export default function SignalsPage() {
  const [tab, setTab] = useState<PageTab>("rules");

  // 综合信号 / 五档模型共享同一份 screener 数据
  const [screenerViews, setScreenerViews] = useState<ScreenerSignalView[]>([]);
  const [screenerRaws, setScreenerRaws] = useState<ScreenerSignal[]>([]);
  const [screenerMeta, setScreenerMeta] = useState<ScreenerMeta | null>(null);
  const [screenerLoading, setScreenerLoading] = useState(false);
  const [screenerError, setScreenerError] = useState<string | null>(null);
  const [screenerLoaded, setScreenerLoaded] = useState(false);

  const loadScreener = useCallback(async () => {
    setScreenerLoading(true);
    setScreenerError(null);
    try {
      const raw = await api.screenerSignals();
      setScreenerRaws(raw.items);
      setScreenerViews(normalizeScreenerSignals(raw.items));
      setScreenerMeta(raw.meta);
      setScreenerLoaded(true);
    } catch (e) {
      setScreenerError(
        e instanceof ApiError
          ? `${e.message}。${SCREENER_API_DOWN_HINT}`
          : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      setScreenerLoading(false);
    }
  }, []);

  useEffect(() => {
    if ((tab === "composite" || tab === "tiers") && !screenerLoaded && !screenerLoading) {
      void loadScreener();
    }
  }, [tab, screenerLoaded, screenerLoading, loadScreener]);

  return (
    <>
      <PageHeader
        title="研究信号"
        description="规则信号、规则模型综合评分（-2 ~ +2）与五档配置模型"
      />

      {/* 页面级 Tab */}
      <div className="mb-6 flex flex-wrap gap-1 rounded-xl border border-slate-200 bg-white p-1 shadow-sm sm:inline-flex">
        {PAGE_TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            title={t.desc}
            className={`flex-1 rounded-lg px-4 py-2 text-sm font-medium transition-colors sm:flex-none ${
              tab === t.key
                ? "bg-slate-900 text-white"
                : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "rules" ? (
        <RuleSignalsTab />
      ) : (
        <ScreenerPanel
          views={screenerViews}
          raws={screenerRaws}
          meta={screenerMeta}
          loading={screenerLoading}
          error={screenerError}
          onRetry={loadScreener}
          mode={tab}
        />
      )}

      <p className="mt-6 text-xs text-slate-400">
        信号与评分由可解释规则生成，仅描述已发生的事实条件，不构成投资建议。
      </p>
    </>
  );
}
