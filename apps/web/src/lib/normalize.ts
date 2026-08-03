import type {
  BacktestPoint,
  BacktestResult,
  BacktestV2CurvePoint,
  BacktestV2Result,
  BacktestV2Summary,
  DiscoveryCatalogBreakdown,
  DiscoveryCatalogFund,
  DiscoveryCatalogListResponse,
  DiscoveryCatalogStats,
  DiscoveryDualMomentumItem,
  DiscoveryDualMomentumResponse,
  DiscoveryFactorItem,
  DiscoveryFactorsResponse,
  DiscoveryPool,
  DiscoveryPoolCoverage,
  DiscoveryPoolDetail,
  DiscoveryPoolMember,
  DiscoverySignalItem,
  DiscoverySignalsResponse,
  FundReturnItem,
  ImportPreview,
  NewsItem,
  PaperHistoryPoint,
  PaperPosition,
  PaperSignal,
  PaperSummary,
  PaperTrade,
  PortfolioReturnsResponse,
  PortfolioReturnWindow,
  PortfolioSummary,
  Position,
  QuantFundMetrics,
  QuantPortfolio,
  QuantSignal,
  ReturnWindowKey,
  ScreenerFactor,
  ScreenerSignal,
  SignalV2Item,
  SignalsV2Response,
  SnapshotFundInfo,
  SnapshotResponse,
  StockDataSourceStatus,
  StockDataStatus,
  StockDetail,
  StockFactorItem,
  StockFactorsResponse,
  StockFinancials,
  StockMasterItem,
  StockMasterResponse,
  StockPricePoint,
  StockQuote,
  StockSignalItem,
  StockSignalsResponse,
  StockUniverseItem,
  StockUniverseResponse,
  ResearchPortfolio,
  ResearchPortfolioHolding,
  ResearchPortfoliosResponse,
  SyncRunItem,
  SyncStatusResponse,
  TradeV2,
  Transaction,
  RebalanceV2,
  ValidationFundSnapshot,
  ValidationResponse,
  WalkForwardPoint,
  WalkForwardResult,
  WalkForwardSegment,
  WalkForwardSummary,
} from "./types";

/**
 * 归一化层：把后端宽松结构的响应收敛为页面直接可用的视图模型。
 */

export function toNumber(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "string" ? Number(v.replace(/[,¥%]/g, "")) : Number(v);
  return Number.isFinite(n) ? n : null;
}

export function fmtMoney(v: unknown, digits = 2): string {
  const n = toNumber(v);
  if (n === null) return "—";
  return n.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function fmtShares(v: unknown): string {
  const n = toNumber(v);
  if (n === null) return "—";
  return n.toLocaleString("zh-CN", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
}

export function fmtPercent(v: unknown): string {
  const n = toNumber(v);
  if (n === null) return "—";
  const pct = Math.abs(n) <= 1 ? n * 100 : n; // 兼容小数/百分数两种返回
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
}

export function fmtDate(v: unknown): string {
  if (typeof v !== "string" || !v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  return d.toLocaleDateString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
}

export function signClass(v: unknown): string {
  const n = toNumber(v);
  if (n === null || n === 0) return "text-slate-600";
  return n > 0 ? "text-rose-600" : "text-emerald-600"; // 国内习惯：涨红跌绿
}

export interface SummaryView {
  totalMarketValue: number | string | null | undefined;
  totalCost: number | string | null | undefined;
  totalProfit: number | string | null | undefined;
  totalReturnRate: number | string | null | undefined;
  estimatedReturn: number | string | null | undefined;
  estimatedReturnRate: number | string | null | undefined;
  yearReturn: number | string | null | undefined;
  previousYearReturn: number | string | null | undefined;
  positionCount: number | string | null | undefined;
  asOf: string | null | undefined;
}

export function normalizeSummary(s: PortfolioSummary | null): SummaryView {
  if (!s) {
    return {
      totalMarketValue: null,
      totalCost: null,
      totalProfit: null,
      totalReturnRate: null,
      estimatedReturn: null,
      estimatedReturnRate: null,
      yearReturn: null,
      previousYearReturn: null,
      positionCount: null,
      asOf: null,
    };
  }
  return {
    totalMarketValue: s.total_market_value ?? (s.market_value as number | undefined) ?? null,
    totalCost: s.total_cost ?? (s.cost as number | undefined) ?? null,
    totalProfit: s.total_profit ?? (s.profit as number | undefined) ?? null,
    totalReturnRate:
      s.total_return_rate ?? (s.return_rate as number | undefined) ?? null,
    estimatedReturn: s.estimated_return ?? null,
    estimatedReturnRate: s.estimated_return_rate ?? null,
    yearReturn: s.year_return ?? null,
    previousYearReturn: s.previous_year_return ?? null,
    positionCount: s.position_count ?? s.fund_count ?? null,
    asOf: s.as_of ?? s.snapshot_date ?? null,
  };
}

export interface PositionView {
  key: string;
  code: string;
  name: string;
  shares: unknown;
  costPrice: unknown;
  nav: unknown;
  marketValue: unknown;
  profit: unknown;
  returnRate: unknown;
  profitAvailable: boolean;
  costCoverageRate: unknown;
  weight: number | null; // 0-100，用于纯 CSS 占比条
}

export function normalizePositions(
  list: Position[] | null | undefined
): PositionView[] {
  const arr = Array.isArray(list) ? list : [];
  const values = arr.map((p) => toNumber(p.market_value) ?? 0);
  const total = values.reduce((a, b) => a + b, 0);
  return arr.map((p, i) => {
    const mv = toNumber(p.market_value) ?? 0;
    return {
      key: String(p.id ?? p.fund_code ?? i),
      code: p.fund_code ?? (p.instrument_code as string | undefined) ?? "—",
      name:
        p.fund_name ??
        (p.instrument_name as string | undefined) ??
        p.fund_code ??
        (p.instrument_code as string | undefined) ??
        "未命名基金",
      shares: p.shares,
      costPrice: p.cost_price,
      nav: p.nav,
      marketValue: p.market_value,
      profit: p.profit_available === false ? null : p.profit,
      returnRate: p.profit_available === false ? null : p.return_rate,
      profitAvailable: p.profit_available === true,
      costCoverageRate: p.cost_coverage_rate,
      weight: total > 0 ? (mv / total) * 100 : null,
    };
  });
}

export interface TransactionView {
  key: string;
  date: unknown;
  code: string;
  name: string;
  type: string;
  amount: unknown;
  shares: unknown;
  nav: unknown;
  fee: unknown;
  status: string;
}

export function normalizeTransactions(
  list: Transaction[] | null | undefined
): TransactionView[] {
  const arr = Array.isArray(list) ? list : [];
  return arr.map((t, i) => ({
    key: String(t.id ?? i),
    date: t.transaction_date ?? t.date,
    code: t.fund_code ?? (t.instrument_code as string | undefined) ?? "—",
    name:
      t.fund_name ??
      (t.instrument_name as string | undefined) ??
      t.fund_code ??
      (t.instrument_code as string | undefined) ??
      "未命名基金",
    type: t.transaction_type ?? t.type ?? "—",
    amount: t.amount,
    shares: t.shares,
    nav: t.nav,
    fee: t.fee,
    status: t.status ?? "—",
  }));
}

export interface PreviewView {
  importId: string | number | null;
  fileName: string;
  snapshotDate: string | null;
  summary: SummaryView | null;
  positions: PositionView[];
  transactions: TransactionView[];
  warnings: string[];
}

export function normalizePreview(
  raw: ImportPreview,
  fallbackFileName: string
): PreviewView {
  return {
    importId: raw.import_id ?? raw.id ?? null,
    fileName: raw.file_name ?? raw.filename ?? fallbackFileName,
    snapshotDate: raw.snapshot_date ?? raw.as_of ?? null,
    summary: raw.summary ? normalizeSummary(raw.summary) : null,
    positions: normalizePositions(raw.positions),
    transactions: normalizeTransactions(raw.transactions),
    warnings: Array.isArray(raw.warnings)
      ? raw.warnings.filter((w): w is string => typeof w === "string")
      : [],
  };
}

/* ==================== 量化分析 ==================== */

export interface QuantPortfolioView {
  totalMarketValue: unknown;
  totalReturnRate: unknown;
  annualizedReturn: unknown;
  annualizedVolatility: unknown;
  maxDrawdown: unknown;
  sharpeRatio: unknown;
  calmarRatio: unknown;
  winRate: unknown;
  benchmarkReturn: unknown;
  excessReturn: unknown;
  positionCount: unknown;
  asOf: string | null;
  concentrationTop1: unknown;
  concentrationTop3: unknown;
  hhi: unknown;
  methodology: string;
}

export function normalizeQuantPortfolio(
  raw: QuantPortfolio | null | undefined
): QuantPortfolioView | null {
  if (!raw || typeof raw !== "object") return null;
  return {
    totalMarketValue: raw.total_market_value ?? null,
    totalReturnRate: raw.total_return_rate ?? null,
    annualizedReturn: raw.annualized_return ?? null,
    annualizedVolatility: raw.annualized_volatility ?? null,
    maxDrawdown: raw.max_drawdown ?? null,
    sharpeRatio: raw.sharpe_ratio ?? null,
    calmarRatio: raw.calmar_ratio ?? null,
    winRate: raw.win_rate ?? null,
    benchmarkReturn: raw.benchmark_return ?? null,
    excessReturn: raw.excess_return ?? null,
    positionCount: raw.position_count ?? null,
    asOf: raw.as_of ?? raw.snapshot_date ?? null,
    concentrationTop1: raw.concentration_top1 ?? null,
    concentrationTop3: raw.concentration_top3 ?? null,
    hhi: raw.hhi ?? null,
    methodology: typeof raw.methodology === "string" ? raw.methodology : "",
  };
}

export interface QuantFundView {
  key: string;
  code: string;
  name: string;
  annualizedReturn: unknown;
  annualizedVolatility: unknown;
  maxDrawdown: unknown;
  sharpeRatio: unknown;
  winRate: unknown;
  returnRate: unknown;
  marketValue: unknown;
}

export function normalizeQuantFunds(
  list: QuantFundMetrics[] | null | undefined
): QuantFundView[] {
  const arr = Array.isArray(list) ? list : [];
  return arr.map((f, i) => ({
    key: String(f.fund_code ?? f.code ?? i),
    code: f.fund_code ?? f.code ?? "—",
    name: f.fund_name ?? f.name ?? f.fund_code ?? f.code ?? "未命名基金",
    annualizedReturn: f.annualized_return ?? f.return_250d ?? null,
    annualizedVolatility: f.annualized_volatility ?? f.annual_volatility ?? null,
    maxDrawdown: f.max_drawdown ?? null,
    sharpeRatio: f.sharpe_ratio ?? f.sharpe ?? null,
    winRate: f.win_rate ?? null,
    returnRate: f.return_rate ?? f.return_60d ?? null,
    marketValue: f.market_value ?? null,
  }));
}

export interface BacktestView {
  fundCode: string;
  fundName: string;
  strategyName: string;
  startDate: string | null;
  endDate: string | null;
  summary: {
    totalReturn: unknown;
    annualizedReturn: unknown;
    annualizedVolatility: unknown;
    maxDrawdown: unknown;
    sharpeRatio: unknown;
    benchmarkReturn: unknown;
    excessReturn: unknown;
    trades: unknown;
  };
  points: { date: string; nav: number; benchmark: number | null }[];
  warnings: string[];
}

export function normalizeBacktest(raw: BacktestResult): BacktestView {
  const rawPoints = Array.isArray(raw.curve)
    ? raw.curve
    : Array.isArray(raw.points)
      ? raw.points
      : [];
  const points = rawPoints
    .map((p: BacktestPoint) => ({
      date: p.date ?? "",
      nav: toNumber(p.nav ?? p.value) ?? Number.NaN,
      benchmark: toNumber(p.benchmark),
    }))
    .filter((p) => p.date && Number.isFinite(p.nav))
    .sort((a, b) => a.date.localeCompare(b.date));
  const summary = raw.summary ?? raw;
  return {
    fundCode: raw.fund_code ?? (raw as { code?: string }).code ?? "",
    fundName: raw.fund_name ?? raw.name ?? raw.fund_code ?? (raw as { code?: string }).code ?? "",
    strategyName: raw.strategy_name ?? raw.strategy ?? "",
    startDate: raw.start_date ?? null,
    endDate: raw.end_date ?? null,
    summary: {
      totalReturn: summary.total_return ?? null,
      annualizedReturn: summary.annualized_return ?? (summary as { annual_return?: unknown }).annual_return ?? null,
      annualizedVolatility: summary.annualized_volatility ?? null,
      maxDrawdown: summary.max_drawdown ?? null,
      sharpeRatio: summary.sharpe_ratio ?? (summary as { sharpe?: unknown }).sharpe ?? null,
      benchmarkReturn: summary.benchmark_return ?? null,
      excessReturn: summary.excess_return ?? null,
      trades: summary.trades ?? (summary as { trade_count?: unknown }).trade_count ?? null,
    },
    points,
    warnings: Array.isArray(raw.warnings)
      ? raw.warnings.filter((w): w is string => typeof w === "string")
      : [],
  };
}

/* ==================== 研究信号 ==================== */

export type SignalSeverity = "high" | "medium" | "low";

export function normalizeSeverity(v: unknown): SignalSeverity {
  const s = typeof v === "string" ? v.toLowerCase() : "";
  if (s === "high" || s === "critical" || s === "strong" || s === "risk" || s === "高") return "high";
  if (s === "medium" || s === "moderate" || s === "mid" || s === "warning" || s === "中") return "medium";
  return "low";
}

export interface SignalView {
  key: string;
  fundCode: string;
  fundName: string;
  signal: string;
  severity: SignalSeverity;
  rule: string;
  reason: string;
  date: string | null;
  metrics: Record<string, unknown> | null;
}

export function normalizeSignals(
  list: QuantSignal[] | null | undefined
): SignalView[] {
  const arr = Array.isArray(list) ? list : [];
  return arr.map((s, i) => ({
    key: String(s.id ?? `${s.fund_code ?? "x"}-${i}`),
    fundCode: s.fund_code ?? "",
    fundName: s.fund_name ?? s.fund_code ?? "组合研究信号",
    signal: s.signal ?? s.direction ?? (s.category as string | undefined) ?? "研究提示",
    severity: normalizeSeverity(s.severity ?? (s.level as string | undefined)),
    rule: s.rule_name ?? s.rule ?? (s.category as string | undefined) ?? "组合规则",
    reason: s.reason ?? s.description ?? (s.message as string | undefined) ?? "",
    date: s.triggered_at ?? s.date ?? s.as_of ?? null,
    metrics:
      s.evidence && typeof s.evidence === "object" && !Array.isArray(s.evidence)
        ? (s.evidence as Record<string, unknown>)
        : s.metrics && typeof s.metrics === "object" && !Array.isArray(s.metrics)
          ? (s.metrics as Record<string, unknown>)
          : null,
  }));
}

/* ==================== 规则模型：综合信号 / 五档模型 ==================== */

/** 综合分数档位：-2 ~ +2 */
export type ScreenerScore = -2 | -1 | 0 | 1 | 2;

/** 把宽松输入钳制到 -2 ~ +2 的整数档位 */
export function clampScore(v: unknown): ScreenerScore {
  const n = toNumber(v);
  if (n === null) return 0;
  const r = Math.round(n);
  if (r <= -2) return -2;
  if (r >= 2) return 2;
  return r as ScreenerScore;
}

/** 综合信号的方向（做多 / 做空 / 中性），用于方向过滤 */
export type ScreenerDirection = "long" | "short" | "neutral";

export function normalizeDirection(v: unknown, score: ScreenerScore): ScreenerDirection {
  const s = typeof v === "string" ? v.toLowerCase() : "";
  if (["long", "buy", "bullish", "positive", "多", "做多", "积极", "加仓"].some((k) => s.includes(k))) {
    return "long";
  }
  if (["short", "sell", "bearish", "negative", "空", "做空", "谨慎", "减仓"].some((k) => s.includes(k))) {
    return "short";
  }
  if (["neutral", "flat", "中性", "none"].some((k) => s.includes(k))) return "neutral";
  if (score > 0) return "long";
  if (score < 0) return "short";
  return "neutral";
}

export interface ScreenerFactorView {
  key: string;
  label: string;
  contribution: number | null;
  reason: string | null;
}

function normalizeFactor(f: ScreenerFactor | string, i: number): ScreenerFactorView {
  if (typeof f === "string") {
    return { key: `f-${i}`, label: f, contribution: null, reason: null };
  }
  const obj = f && typeof f === "object" ? f : {};
  return {
    key: `f-${i}`,
    label: obj.label ?? obj.name ?? "因子",
    contribution: toNumber(obj.contribution ?? obj.score ?? obj.value),
    reason: obj.reason ?? obj.description ?? null,
  };
}

export interface ScreenerSignalView {
  key: string;
  fundCode: string;
  fundName: string;
  score: ScreenerScore;
  /** 归一化到 0-100 的分位数，未知为 null */
  percentile: number | null;
  market: string;
  direction: ScreenerDirection;
  targetWeight: number | null;
  /** 是否进入目标组合；缺省按 targetWeight > 0 推断 */
  inTarget: boolean;
  positiveFactors: ScreenerFactorView[];
  negativeFactors: ScreenerFactorView[];
  reasons: string[];
  asOf: string | null;
}

export function normalizeScreenerSignals(
  list: ScreenerSignal[] | null | undefined
): ScreenerSignalView[] {
  const arr = Array.isArray(list) ? list : [];
  return arr.map((s, i) => {
    const tierRaw = toNumber(s.tier);
    const score = tierRaw !== null && tierRaw >= -2 && tierRaw <= 2
      ? Math.round(tierRaw) as ScreenerScore
      : clampScore(s.score ?? s.composite_score);
    const percentileRaw = toNumber(s.percentile ?? s.score_percentile ?? (s as { quantile?: unknown }).quantile);
    const percentile =
      percentileRaw === null
        ? null
        : Math.abs(percentileRaw) <= 1
          ? percentileRaw * 100
          : percentileRaw;
    const reasons = [
      ...(Array.isArray(s.reasons)
        ? s.reasons.filter((r): r is string => typeof r === "string" && r.length > 0)
        : []),
      ...(typeof s.reason === "string" && s.reason ? [s.reason] : []),
    ];
    const factorPool = [
      ...(Array.isArray(s.factors) ? s.factors : []),
    ];
    const targetWeight = toNumber(s.target_weight);
    return {
      key: String(s.fund_code ?? s.code ?? i),
      fundCode: s.fund_code ?? s.code ?? "—",
      fundName: s.fund_name ?? s.name ?? s.fund_code ?? s.code ?? "未命名标的",
      score,
      percentile,
      market: s.market ?? "—",
      direction: normalizeDirection(s.direction ?? s.stance, score),
      targetWeight,
      inTarget:
        typeof s.in_target === "boolean" ? s.in_target : (targetWeight ?? 0) > 0,
      positiveFactors: (Array.isArray(s.positive_factors) ? s.positive_factors : []).map(
        normalizeFactor
      ),
      negativeFactors: (Array.isArray(s.negative_factors) ? s.negative_factors : []).map(
        normalizeFactor
      ),
      reasons:
        reasons.length > 0
          ? reasons
          : factorPool
              .map((f) => (typeof f === "string" ? f : (f.reason ?? f.description ?? null)))
              .filter((r): r is string => typeof r === "string" && r.length > 0),
      asOf: s.as_of ?? s.date ?? null,
    };
  });
}

/** 五档模型的一档（按目标权重分组后的单个标的） */
export interface TierItemView {
  key: string;
  fundCode: string;
  fundName: string;
  market: string;
  score: ScreenerScore;
  percentile: number | null;
  targetWeight: number | null;
  /** 是否进入目标组合（前 top_n 只分配权重；其余仅参与分析） */
  inTarget: boolean;
}

/** 五档模型视图：tier 1（最低）~ tier 5（最高） */
export interface TierView {
  tier: number;
  label: string;
  weightHint: string;
  items: TierItemView[];
}

const TIER_META: { tier: number; label: string; weightHint: string }[] = [
  { tier: 5, label: "五档 · 核心配置", weightHint: "目标权重最高" },
  { tier: 4, label: "四档 · 积极配置", weightHint: "目标权重较高" },
  { tier: 3, label: "三档 · 标准配置", weightHint: "目标权重中性" },
  { tier: 2, label: "二档 · 低配观察", weightHint: "目标权重较低" },
  { tier: 1, label: "一档 · 回避/减仓", weightHint: "目标权重最低" },
];

/** 由综合信号推导五档模型：优先使用后端 tier 字段，否则按分数映射（+2→5 … -2→1） */
export function buildTierModel(views: ScreenerSignalView[], raws?: ScreenerSignal[]): TierView[] {
  const tiers: TierView[] = TIER_META.map((m) => ({ ...m, items: [] }));
  views.forEach((v, i) => {
    const rawTier = raws?.[i] ? toNumber(raws[i].tier) : null;
    const tier =
      rawTier !== null && rawTier >= -2 && rawTier <= 2
        ? Math.round(rawTier) + 3
        : rawTier !== null && rawTier >= 1 && rawTier <= 5
          ? Math.round(rawTier)
          : v.score + 3; // -2..+2 → 1..5
    const bucket = tiers.find((t) => t.tier === tier) ?? tiers[tiers.length - 1];
    bucket.items.push({
      key: v.key,
      fundCode: v.fundCode,
      fundName: v.fundName,
      market: v.market,
      score: v.score,
      percentile: v.percentile,
      targetWeight: v.targetWeight,
      inTarget: v.inTarget,
    });
  });
  for (const t of tiers) {
    t.items.sort(
      (a, b) =>
        (b.targetWeight ?? Number.NEGATIVE_INFINITY) - (a.targetWeight ?? Number.NEGATIVE_INFINITY)
    );
  }
  return tiers;
}

/* ==================== Walk-Forward 滚动回测 ==================== */

export interface WalkForwardView {
  summary: {
    annualizedReturn: unknown;
    maxDrawdown: unknown;
    sharpeRatio: unknown;
    winRate: unknown;
    turnover: unknown;
    excessReturn: unknown;
    benchmarkAnnualizedReturn: unknown;
  };
  points: { date: string; nav: number; benchmark: number | null }[];
  segments: {
    key: string;
    index: string;
    trainStart: string | null;
    trainEnd: string | null;
    testStart: string | null;
    testEnd: string | null;
    testReturn: unknown;
    benchmarkReturn: unknown;
    excessReturn: unknown;
    sharpeRatio: unknown;
    holdings: string[];
  }[];
  trainWindow: number | null;
  testWindow: number | null;
  warnings: string[];
}

function pickSummary(raw: WalkForwardResult): WalkForwardSummary {
  if (raw.summary && typeof raw.summary === "object") return raw.summary;
  if (raw.metrics && typeof raw.metrics === "object") return raw.metrics;
  return raw as WalkForwardSummary;
}

export function normalizeWalkForward(raw: WalkForwardResult): WalkForwardView {
  const summary = pickSummary(raw);
  const strategySummary = raw.strategy && typeof raw.strategy === "object" ? raw.strategy : summary;
  const benchmarkSummary = raw.benchmark && typeof raw.benchmark === "object" ? raw.benchmark : {};
  const rawPoints = Array.isArray(raw.curve)
    ? raw.curve
    : Array.isArray(raw.points)
      ? raw.points
      : Array.isArray(raw.equity_curve)
        ? raw.equity_curve
        : [];
  const points = rawPoints
    .map((p: WalkForwardPoint) => ({
      date: p.date ?? "",
      nav: toNumber(p.nav ?? p.value ?? p.strategy) ?? Number.NaN,
      benchmark: toNumber(p.benchmark ?? p.benchmark_nav ?? p.equal_weight),
    }))
    .filter((p) => p.date && Number.isFinite(p.nav))
    .sort((a, b) => a.date.localeCompare(b.date));

  const rawSegments = Array.isArray(raw.segments) ? raw.segments : [];
  const segments = rawSegments.map((s: WalkForwardSegment, i: number) => ({
    key: String(s.index ?? i),
    index: s.index !== null && s.index !== undefined ? String(s.index) : String(i + 1),
    trainStart: s.train_start ?? null,
    trainEnd: s.train_end ?? null,
    testStart: s.test_start ?? null,
    testEnd: s.test_end ?? null,
    testReturn: s.test_return ?? s.segment_return ?? s.return_rate ?? s.annualized_return ?? s.annual_return ?? null,
    benchmarkReturn: s.benchmark_return ?? null,
    excessReturn: s.excess_return ?? null,
    sharpeRatio: s.sharpe_ratio ?? s.sharpe ?? null,
    holdings: Array.isArray(s.holdings)
      ? s.holdings.filter((h): h is string => typeof h === "string")
      : s.holdings && typeof s.holdings === "object"
        ? Object.entries(s.holdings).map(([code, weight]) => `${code} ${(toNumber(weight) ?? 0) * 100}%`)
        : [],
  }));

  return {
    summary: {
      annualizedReturn: strategySummary.annualized_return ?? strategySummary.annual_return ?? null,
      maxDrawdown: strategySummary.max_drawdown ?? null,
      sharpeRatio: strategySummary.sharpe_ratio ?? strategySummary.sharpe ?? null,
      winRate: strategySummary.win_rate ?? null,
      turnover: raw.turnover ?? summary.turnover ?? summary.turnover_rate ?? null,
      excessReturn: raw.excess_return ?? summary.excess_return ?? null,
      benchmarkAnnualizedReturn:
        benchmarkSummary.annualized_return ?? benchmarkSummary.annual_return ?? summary.benchmark_annualized_return ?? summary.benchmark_return ?? null,
    },
    points,
    segments,
    trainWindow: toNumber(raw.train_window),
    testWindow: toNumber(raw.test_window),
    warnings: Array.isArray(raw.warnings)
      ? raw.warnings.filter((w): w is string => typeof w === "string")
      : [],
  };
}

/* ==================== 稳健组合策略 V2（/api/quant/v2/*） ==================== */

export interface BacktestV2CurvePointView {
  date: string;
  strategy: number;
  benchmark: number | null;
}

export interface TradeV2View {
  key: string;
  signalDate: string | null;
  fillDate: string | null;
  code: string;
  name: string;
  action: "buy" | "sell" | "other";
  amount: number | null;
  fee: number | null;
  price: number | null;
  settleLag: number | null;
  reason: string;
}

export interface RebalanceV2View {
  key: string;
  index: string;
  signalDate: string | null;
  fillDate: string | null;
  holdings: { code: string; weight: number }[];
  cashWeight: number | null;
  turnover: number | null;
  frozen: boolean;
  /** 归一化后的配置方法：hrp / inverse_vol / equal_weight / frozen / 组合（+ 连接） */
  allocationMethod: string;
  realizedVol: number | null;
  volScalar: number | null;
  reason: string;
}

export interface BacktestV2SummaryView {
  totalReturn: number | null;
  annualReturn: number | null;
  annualVolatility: number | null;
  maxDrawdown: number | null;
  sharpe: number | null;
  winRate: number | null;
}

export interface BacktestV2View {
  startDate: string | null;
  endDate: string | null;
  initialCapital: number | null;
  strategy: BacktestV2SummaryView;
  benchmark: BacktestV2SummaryView;
  excessReturn: number | null;
  avgTurnover: number | null;
  rebalanceCount: number | null;
  frozenCount: number | null;
  totalFees: number | null;
  curve: BacktestV2CurvePointView[];
  rebalances: RebalanceV2View[];
  trades: TradeV2View[];
  methodology: string;
  warnings: string[];
}

function normalizeBacktestV2Summary(
  raw: BacktestV2Summary | null | undefined
): BacktestV2SummaryView {
  const s = raw && typeof raw === "object" ? raw : {};
  return {
    totalReturn: toNumber(s.total_return),
    annualReturn: toNumber(s.annual_return ?? s.annualized_return),
    annualVolatility: toNumber(s.annual_volatility ?? s.annualized_volatility),
    maxDrawdown: toNumber(s.max_drawdown),
    sharpe: toNumber(s.sharpe ?? s.sharpe_ratio),
    winRate: toNumber(s.win_rate),
  };
}

export function normalizeBacktestV2(raw: BacktestV2Result | null | undefined): BacktestV2View {
  const obj = raw && typeof raw === "object" ? raw : {};
  const rawPoints = Array.isArray(obj.curve)
    ? obj.curve
    : Array.isArray(obj.points)
      ? obj.points
      : [];
  const curve = rawPoints
    .map((p: BacktestV2CurvePoint) => ({
      date: p.date ?? "",
      strategy: toNumber(p.strategy ?? p.nav ?? p.value) ?? Number.NaN,
      benchmark: toNumber(p.benchmark),
    }))
    .filter((p) => p.date && Number.isFinite(p.strategy))
    .sort((a, b) => a.date.localeCompare(b.date));

  const rebalances = (Array.isArray(obj.rebalances) ? obj.rebalances : []).map(
    (r: RebalanceV2, i: number): RebalanceV2View => ({
      key: String(r.index ?? i),
      index: r.index !== null && r.index !== undefined ? String(r.index) : String(i + 1),
      signalDate: r.signal_date ?? null,
      fillDate: r.fill_date ?? null,
      holdings:
        r.holdings && typeof r.holdings === "object" && !Array.isArray(r.holdings)
          ? Object.entries(r.holdings)
              .map(([code, w]) => ({ code, weight: toNumber(w) ?? 0 }))
              .sort((a, b) => b.weight - a.weight)
          : [],
      cashWeight: toNumber(r.cash_weight),
      turnover: toNumber(r.turnover),
      frozen: r.frozen === true || r.allocation_method === "frozen",
      allocationMethod: r.allocation_method ?? "—",
      realizedVol: toNumber(r.realized_vol),
      volScalar: toNumber(r.vol_scalar),
      reason: r.reason ?? "",
    })
  );

  const trades = (Array.isArray(obj.trades) ? obj.trades : []).map(
    (t: TradeV2, i: number): TradeV2View => {
      const { side } = normalizePaperSide(t.action);
      return {
        key: `${t.code ?? "x"}-${t.fill_date ?? i}-${i}`,
        signalDate: t.signal_date ?? null,
        fillDate: t.fill_date ?? null,
        code: t.code ?? "—",
        name: t.name ?? t.code ?? "未命名标的",
        action: side,
        amount: toNumber(t.amount),
        fee: toNumber(t.fee),
        price: toNumber(t.price),
        settleLag: toNumber(t.settle_lag),
        reason: t.reason ?? "",
      };
    }
  );

  return {
    startDate: obj.start_date ?? null,
    endDate: obj.end_date ?? null,
    initialCapital: toNumber(obj.initial_capital),
    strategy: normalizeBacktestV2Summary(obj.strategy),
    benchmark: normalizeBacktestV2Summary(obj.benchmark),
    excessReturn: toNumber(obj.excess_return),
    avgTurnover: toNumber(obj.avg_turnover ?? obj.turnover),
    rebalanceCount: toNumber(obj.rebalance_count),
    frozenCount: toNumber(obj.frozen_count),
    totalFees: toNumber(obj.total_fees),
    curve,
    rebalances,
    trades,
    methodology: typeof obj.methodology === "string" ? obj.methodology : "",
    warnings: Array.isArray(obj.warnings)
      ? obj.warnings.filter((w): w is string => typeof w === "string" && w.length > 0)
      : [],
  };
}

export interface SignalV2ItemView {
  key: string;
  code: string;
  name: string;
  market: string;
  family: string;
  momentum121: number | null;
  rankInMarket: number | null;
  marketCandidates: number | null;
  weight: number | null;
  reasons: string[];
}

export interface SignalsV2View {
  asOf: string | null;
  tradeDate: string | null;
  methodology: string;
  candidateCount: number | null;
  eligibleCount: number | null;
  selected: SignalV2ItemView[];
  cashWeight: number | null;
  realizedVol: number | null;
  volScalar: number | null;
  frozen: boolean;
  freezeReason: string | null;
  warnings: string[];
}

export function normalizeSignalsV2(
  raw: SignalsV2Response | null | undefined
): SignalsV2View {
  const obj = raw && typeof raw === "object" ? raw : {};
  const rawSelected = Array.isArray(obj.selected)
    ? obj.selected
    : Array.isArray(obj.signals)
      ? obj.signals
      : Array.isArray(obj.items)
        ? obj.items
        : [];
  const selected = rawSelected.map((s: SignalV2Item, i: number): SignalV2ItemView => {
    const code = s.code ?? s.fund_code ?? "—";
    return {
      key: String(s.code ?? s.fund_code ?? i),
      code,
      name: s.name ?? s.fund_name ?? code,
      market: s.market ?? "—",
      family: s.family ?? "",
      momentum121: toNumber(s.momentum_12_1 ?? s.momentum),
      rankInMarket: toNumber(s.rank_in_market),
      marketCandidates: toNumber(s.market_candidates),
      weight: toNumber(s.weight),
      reasons: Array.isArray(s.reasons)
        ? s.reasons.filter((r): r is string => typeof r === "string" && r.length > 0)
        : [],
    };
  });
  return {
    asOf: obj.as_of ?? null,
    tradeDate: obj.trade_date ?? null,
    methodology: typeof obj.methodology === "string" ? obj.methodology : "",
    candidateCount: toNumber(obj.candidate_count),
    eligibleCount: toNumber(obj.eligible_count),
    selected,
    cashWeight: toNumber(obj.cash_weight),
    realizedVol: toNumber(obj.realized_vol),
    volScalar: toNumber(obj.vol_scalar),
    frozen: obj.frozen === true,
    freezeReason: typeof obj.freeze_reason === "string" ? obj.freeze_reason : null,
    warnings: Array.isArray(obj.warnings)
      ? obj.warnings.filter((w): w is string => typeof w === "string" && w.length > 0)
      : [],
  };
}

/* ==================== 统计验证（/api/quant/validation 与 /api/quant/snapshot） ==================== */

export interface ValidationRiskView {
  totalReturn: number | null;
  annualReturn: number | null;
  sharpe: number | null;
  maxDrawdown: number | null;
  cvar95: number | null;
  calmar: number | null;
  winRate: number | null;
}

export interface ValidationPredictivenessView {
  rankIcMean: number | null;
  rankIcCount: number | null;
  /** 五档（低→高）平均前瞻收益；长度不保证为 5，元素可为 null */
  quintileReturns: (number | null)[];
  quintileSpread: number | null;
  quintileKendallTau: number | null;
  quintileMonotonic: boolean;
}

export interface ValidationRobustnessView {
  trialCount: number | null;
  skew: number | null;
  kurtosis: number | null;
  sharpeStd: number | null;
  expectedMaxSharpe: number | null;
  deflatedSharpe: number | null;
  realityCheckP: number | null;
  realityCheckStat: number | null;
  realityCheckNullMean: number | null;
  bootstrapResamples: number | null;
  blockLength: number | null;
}

export interface ValidationNeighborhoodView {
  centerSharpe: number | null;
  neighborhoodQuantile: number | null;
  bandLow: number | null;
  bandHigh: number | null;
  neighborCount: number | null;
  neighbors: { label: string; sharpe: number | null }[];
}

export interface ValidationCostsView {
  includeCosts: boolean;
  buyFeeRate: number | null;
  sellFeeRate: number | null;
  shortTermSellFeeRate: number | null;
  shortTermDays: number | null;
  totalFeeRatio: number | null;
  tradeDays: number | null;
  sellFeeBasis: string | null;
}

export interface ValidationFundSnapshotView {
  key: string;
  code: string;
  name: string;
  isQdii: boolean;
  lagDays: number | null;
  latestNavDate: string | null;
  effectiveDate: string | null;
}

export interface ValidationView {
  asOf: string | null;
  candidateCodes: string[];
  startDate: string | null;
  endDate: string | null;
  sampleCount: number | null;
  oosCount: number | null;
  strategy: ValidationRiskView;
  benchmark: ValidationRiskView;
  informationRatio: number | null;
  excessReturn: number | null;
  predictiveness: ValidationPredictivenessView;
  robustness: ValidationRobustnessView;
  neighborhood: ValidationNeighborhoodView;
  costs: ValidationCostsView;
  fundSnapshots: ValidationFundSnapshotView[];
  methodology: string;
  warnings: string[];
}

function normalizeValidationRisk(
  raw: ValidationResponse["strategy"]
): ValidationRiskView {
  const s = raw && typeof raw === "object" ? raw : {};
  return {
    totalReturn: toNumber(s.total_return),
    annualReturn: toNumber(s.annual_return),
    sharpe: toNumber(s.sharpe),
    maxDrawdown: toNumber(s.max_drawdown),
    cvar95: toNumber(s.cvar95),
    calmar: toNumber(s.calmar),
    winRate: toNumber(s.win_rate),
  };
}

function normalizeFundSnapshots(
  list: ValidationFundSnapshot[] | null | undefined
): ValidationFundSnapshotView[] {
  const arr = Array.isArray(list) ? list : [];
  return arr.map((f, i) => ({
    key: String(f.code ?? i),
    code: f.code ?? "—",
    name: f.name ?? f.code ?? "未命名基金",
    isQdii: f.is_qdii === true,
    lagDays: toNumber(f.lag_days),
    latestNavDate: f.latest_nav_date ?? null,
    effectiveDate: f.effective_date ?? null,
  }));
}

export function normalizeValidation(
  raw: ValidationResponse | null | undefined
): ValidationView {
  const obj = raw && typeof raw === "object" ? raw : {};
  const p = obj.predictiveness && typeof obj.predictiveness === "object" ? obj.predictiveness : {};
  const r = obj.robustness && typeof obj.robustness === "object" ? obj.robustness : {};
  const n = obj.neighborhood && typeof obj.neighborhood === "object" ? obj.neighborhood : {};
  const c = obj.costs && typeof obj.costs === "object" ? obj.costs : {};

  const quintileReturns = Array.isArray(p.quintile_returns)
    ? p.quintile_returns.map((v) => toNumber(v))
    : [];
  const neighbors =
    n.neighbors && typeof n.neighbors === "object" && !Array.isArray(n.neighbors)
      ? Object.entries(n.neighbors)
          .map(([label, sharpe]) => ({ label, sharpe: toNumber(sharpe) }))
          .sort((a, b) => (a.sharpe ?? Number.NEGATIVE_INFINITY) - (b.sharpe ?? Number.NEGATIVE_INFINITY))
      : [];

  return {
    asOf: obj.as_of ?? null,
    candidateCodes: Array.isArray(obj.candidate_codes)
      ? obj.candidate_codes.filter((x): x is string => typeof x === "string")
      : [],
    startDate: obj.start_date ?? null,
    endDate: obj.end_date ?? null,
    sampleCount: toNumber(obj.sample_count),
    oosCount: toNumber(obj.oos_count),
    strategy: normalizeValidationRisk(obj.strategy),
    benchmark: normalizeValidationRisk(obj.benchmark),
    informationRatio: toNumber(obj.information_ratio),
    excessReturn: toNumber(obj.excess_return),
    predictiveness: {
      rankIcMean: toNumber(p.rank_ic_mean),
      rankIcCount: toNumber(p.rank_ic_count),
      quintileReturns,
      quintileSpread: toNumber(p.quintile_spread),
      quintileKendallTau: toNumber(p.quintile_kendall_tau),
      quintileMonotonic: p.quintile_monotonic === true,
    },
    robustness: {
      trialCount: toNumber(r.trial_count),
      skew: toNumber(r.skew),
      kurtosis: toNumber(r.kurtosis),
      sharpeStd: toNumber(r.sharpe_std),
      expectedMaxSharpe: toNumber(r.expected_max_sharpe),
      deflatedSharpe: toNumber(r.deflated_sharpe),
      realityCheckP: toNumber(r.reality_check_p),
      realityCheckStat: toNumber(r.reality_check_stat),
      realityCheckNullMean: toNumber(r.reality_check_null_mean),
      bootstrapResamples: toNumber(r.bootstrap_resamples),
      blockLength: toNumber(r.block_length),
    },
    neighborhood: {
      centerSharpe: toNumber(n.center_sharpe),
      neighborhoodQuantile: toNumber(n.neighborhood_quantile),
      bandLow: toNumber(n.band_low),
      bandHigh: toNumber(n.band_high),
      neighborCount: toNumber(n.neighbor_count),
      neighbors,
    },
    costs: {
      includeCosts: c.include_costs === true,
      buyFeeRate: toNumber(c.buy_fee_rate),
      sellFeeRate: toNumber(c.sell_fee_rate),
      shortTermSellFeeRate: toNumber(c.short_term_sell_fee_rate),
      shortTermDays: toNumber(c.short_term_days),
      totalFeeRatio: toNumber(c.total_fee_ratio),
      tradeDays: toNumber(c.trade_days),
      sellFeeBasis: typeof c.sell_fee_basis === "string" ? c.sell_fee_basis : null,
    },
    fundSnapshots: normalizeFundSnapshots(obj.fund_snapshots),
    methodology: typeof obj.methodology === "string" ? obj.methodology : "",
    warnings: Array.isArray(obj.warnings)
      ? obj.warnings.filter((w): w is string => typeof w === "string" && w.length > 0)
      : [],
  };
}

export interface SnapshotFundView {
  key: string;
  code: string;
  name: string;
  isQdii: boolean;
  lagDays: number | null;
  firstNavDate: string | null;
  latestNavDate: string | null;
  navCount: number | null;
  effectiveDate: string | null;
}

export interface SnapshotView {
  asOf: string | null;
  tradeDayCount: number | null;
  truncated: boolean;
  funds: SnapshotFundView[];
}

export function normalizeSnapshot(raw: SnapshotResponse | null | undefined): SnapshotView {
  const obj = raw && typeof raw === "object" ? raw : {};
  const funds = (Array.isArray(obj.funds) ? obj.funds : []).map(
    (f: SnapshotFundInfo, i: number): SnapshotFundView => ({
      key: String(f.code ?? i),
      code: f.code ?? "—",
      name: f.name ?? f.code ?? "未命名基金",
      isQdii: f.is_qdii === true,
      lagDays: toNumber(f.lag_days),
      firstNavDate: f.first_nav_date ?? null,
      latestNavDate: f.latest_nav_date ?? null,
      navCount: toNumber(f.nav_count),
      effectiveDate: f.effective_date ?? null,
    })
  );
  return {
    asOf: obj.as_of ?? null,
    tradeDayCount: toNumber(obj.trade_day_count ?? (Array.isArray(obj.trade_days) ? obj.trade_days.length : null)),
    truncated: obj.truncated === true,
    funds,
  };
}

/* ==================== 每日资讯 ==================== */

export interface NewsItemView {
  key: string;
  title: string;
  summary: string;
  source: string;
  url: string | null;
  publishedAt: string | null;
  sentiment: string | null;
  tags: string[];
}

export function normalizeNews(
  list: NewsItem[] | null | undefined
): NewsItemView[] {
  const arr = Array.isArray(list) ? list : [];
  return arr.map((n, i) => {
    const tags = [
      ...(Array.isArray(n.related_funds)
        ? n.related_funds.filter((t): t is string => typeof t === "string")
        : []),
      ...(Array.isArray(n.related_codes)
        ? n.related_codes.filter((t): t is string => typeof t === "string")
        : []),
      ...(Array.isArray(n.tags)
        ? n.tags.filter((t): t is string => typeof t === "string")
        : []),
    ];
    return {
      key: String(n.id ?? i),
      title: n.title ?? "（无标题）",
      summary: n.summary ?? "",
      source: n.source ?? "",
      url: typeof n.url === "string" && n.url ? n.url : null,
      publishedAt: n.published_at ?? null,
      sentiment: typeof n.sentiment === "string" ? n.sentiment : null,
      tags: [...new Set(tags)],
    };
  });
}

/* ==================== 模拟交易（虚拟盘，非真实交易） ==================== */

export interface PaperSummaryView {
  initialCapital: number | null;
  totalValue: number | null;
  cash: number | null;
  marketValue: number | null;
  totalProfit: number | null;
  totalReturnRate: unknown;
  dailyProfit: number | null;
  dailyReturnRate: unknown;
  positionCount: number | null;
  tradeCount: number | null;
  benchmarkReturn: unknown;
  asOf: string | null;
}

export function normalizePaperSummary(
  raw: PaperSummary | null | undefined
): PaperSummaryView | null {
  if (!raw || typeof raw !== "object") return null;
  const totalValue = toNumber(
    raw.total_value ?? raw.total_market_value ?? raw.equity ?? raw.market_value
  );
  const marketValue = toNumber(raw.market_value ?? raw.position_value);
  const cashRaw = toNumber(raw.cash ?? raw.cash_available);
  const initialCapital = toNumber(raw.initial_capital);
  const totalProfit = toNumber(raw.total_profit ?? raw.profit);
  return {
    initialCapital,
    totalValue,
    cash: cashRaw ?? (totalValue !== null && marketValue !== null ? totalValue - marketValue : null),
    marketValue: marketValue ?? (totalValue !== null && cashRaw !== null ? totalValue - cashRaw : null),
    totalProfit:
      totalProfit ??
      (totalValue !== null && initialCapital !== null ? totalValue - initialCapital : null),
    totalReturnRate: raw.total_return_rate ?? raw.total_return ?? raw.return_rate ?? null,
    dailyProfit: toNumber(raw.daily_profit ?? raw.today_profit ?? raw.day_profit),
    dailyReturnRate: raw.daily_return_rate ?? raw.daily_return ?? raw.today_return ?? null,
    positionCount: toNumber(raw.position_count),
    tradeCount: toNumber(raw.trade_count),
    benchmarkReturn: raw.benchmark_return_rate ?? raw.benchmark_return ?? null,
    asOf: raw.as_of ?? raw.date ?? raw.updated_at ?? null,
  };
}

export interface PaperCurvePoint {
  date: string;
  value: number;
  benchmark: number | null;
}

export function normalizePaperHistory(
  list: PaperHistoryPoint[] | null | undefined
): PaperCurvePoint[] {
  const arr = Array.isArray(list) ? list : [];
  return arr
    .map((p) => ({
      date: p.date ?? p.as_of ?? "",
      value: toNumber(p.total_value ?? p.value ?? p.equity ?? p.nav) ?? Number.NaN,
      benchmark: toNumber(p.benchmark ?? p.benchmark_value ?? p.equal_weight ?? p.benchmark_nav),
    }))
    .filter((p) => p.date && Number.isFinite(p.value))
    .sort((a, b) => a.date.localeCompare(b.date));
}

export interface PaperPositionView {
  key: string;
  code: string;
  name: string;
  shares: unknown;
  costPrice: unknown;
  nav: unknown;
  marketValue: number | null;
  weight: number | null; // 0-100
  profit: unknown;
  returnRate: unknown;
  dailyProfit: number | null;
}

export function normalizePaperPositions(
  list: PaperPosition[] | null | undefined
): PaperPositionView[] {
  const arr = Array.isArray(list) ? list : [];
  const values = arr.map((p) => toNumber(p.market_value ?? p.value) ?? 0);
  const total = values.reduce((a, b) => a + b, 0);
  return arr.map((p, i) => {
    const mv = toNumber(p.market_value ?? p.value);
    const weightRaw = toNumber(p.weight);
    const weight =
      weightRaw !== null
        ? Math.abs(weightRaw) <= 1
          ? weightRaw * 100
          : weightRaw
        : total > 0 && mv !== null
          ? (mv / total) * 100
          : null;
    return {
      key: String(p.id ?? p.fund_code ?? p.code ?? i),
      code: p.fund_code ?? p.code ?? "—",
      name: p.fund_name ?? p.name ?? p.fund_code ?? p.code ?? "未命名标的",
      shares: p.shares ?? p.quantity ?? null,
      costPrice: p.cost_price ?? p.avg_cost ?? null,
      nav: p.nav ?? p.price ?? null,
      marketValue: mv,
      weight,
      profit: p.profit ?? null,
      returnRate: p.return_rate ?? null,
      dailyProfit: toNumber(p.daily_profit ?? p.today_profit),
    };
  });
}

export interface PaperTradeView {
  key: string;
  date: string | null;
  code: string;
  name: string;
  side: "buy" | "sell" | "other";
  sideLabel: string;
  shares: unknown;
  price: unknown;
  amount: unknown;
  fee: unknown;
  reason: string;
}

export function normalizePaperSide(v: unknown): { side: "buy" | "sell" | "other"; label: string } {
  const s = typeof v === "string" ? v.toLowerCase() : "";
  if (["buy", "long", "b", "买入", "买", "加仓", "申购"].some((k) => s.includes(k))) {
    return { side: "buy", label: "买入" };
  }
  if (["sell", "short", "s", "卖出", "卖", "减仓", "赎回"].some((k) => s.includes(k))) {
    return { side: "sell", label: "卖出" };
  }
  return { side: "other", label: typeof v === "string" && v ? v : "—" };
}

export function normalizePaperTrades(
  list: PaperTrade[] | null | undefined
): PaperTradeView[] {
  const arr = Array.isArray(list) ? list : [];
  return arr.map((t, i) => {
    const { side, label } = normalizePaperSide(t.side ?? t.direction ?? t.type ?? t.action);
    return {
      key: String(t.id ?? i),
      date: t.date ?? t.trade_date ?? t.executed_at ?? t.created_at ?? null,
      code: t.fund_code ?? t.code ?? "—",
      name: t.fund_name ?? t.name ?? t.fund_code ?? t.code ?? "未命名标的",
      side,
      sideLabel: label,
      shares: t.shares ?? t.quantity ?? null,
      price: t.price ?? t.nav ?? null,
      amount: t.amount ?? null,
      fee: t.fee ?? null,
      reason: t.reason ?? t.signal ?? "",
    };
  });
}

export interface PaperSignalView {
  key: string;
  code: string;
  name: string;
  signal: string;
  direction: ScreenerDirection;
  tier: number | null;
  score: number | null;
  targetWeight: number | null;
  reason: string;
  asOf: string | null;
}

export function normalizePaperSignals(
  list: PaperSignal[] | null | undefined
): PaperSignalView[] {
  const arr = Array.isArray(list) ? list : [];
  return arr.map((s, i) => {
    const score = clampScore(s.score ?? s.composite_score);
    const direction = normalizeDirection(s.direction ?? s.stance ?? s.signal, score);
    const tierRaw = toNumber(s.tier);
    const tier =
      tierRaw !== null && tierRaw >= 1 && tierRaw <= 5
        ? Math.round(tierRaw)
        : tierRaw !== null && tierRaw >= -2 && tierRaw <= 2
          ? Math.round(tierRaw) + 3
          : null;
    const reasons = [
      ...(Array.isArray(s.reasons)
        ? s.reasons.filter((r): r is string => typeof r === "string" && r.length > 0)
        : []),
      ...(typeof s.reason === "string" && s.reason ? [s.reason] : []),
      ...(typeof s.message === "string" && s.message ? [s.message] : []),
    ];
    return {
      key: String(s.id ?? s.fund_code ?? s.code ?? i),
      code: s.fund_code ?? s.code ?? "—",
      name: s.fund_name ?? s.name ?? s.fund_code ?? s.code ?? "未命名标的",
      signal: s.signal ?? s.stance ?? s.direction ?? "模拟信号",
      direction,
      tier,
      score: toNumber(s.score ?? s.composite_score),
      targetWeight: toNumber(s.target_weight),
      reason: reasons.join("；"),
      asOf: s.as_of ?? s.date ?? null,
    };
  });
}

/* ==================== 组合区间收益（GET /api/portfolio/returns） ==================== */

/** 收益窗口元信息：顺序即展示顺序 */
export const RETURN_WINDOWS: { key: ReturnWindowKey; label: string }[] = [
  { key: "1d", label: "今日" },
  { key: "1w", label: "近一周" },
  { key: "1m", label: "近30天" },
  { key: "3m", label: "近3月" },
];

/** 基金在窗口内的收益状态 */
export type FundReturnStatus = "available" | "approximate" | "stale";

export function normalizeReturnStatus(v: unknown): FundReturnStatus {
  const s = typeof v === "string" ? v.toLowerCase() : "";
  if (s === "stale" || s === "missing" || s === "unavailable") return "stale";
  if (s === "approximate" || s === "approx" || s === "estimated") return "approximate";
  return "available";
}

export interface FundReturnItemView {
  key: string;
  code: string;
  name: string;
  isQdii: boolean;
  /** 窗口收益金额，无数据为 null */
  returnAmount: number | null;
  /** 窗口收益率（小数），无数据为 null */
  returnRate: number | null;
  /** 实际起点净值日期 */
  startDate: string | null;
  /** 实际终点净值日期（该基金最新净值日期） */
  endDate: string | null;
  status: FundReturnStatus;
  staleReason: string | null;
  hasFlows: boolean;
  weight: number | null;
}

export function normalizeFundReturnItems(
  list: FundReturnItem[] | null | undefined
): FundReturnItemView[] {
  const arr = Array.isArray(list) ? list : [];
  return arr.map((it, i) => {
    const code = it.instrument_code ?? (it as { fund_code?: string }).fund_code ?? "—";
    return {
      key: String(it.instrument_id ?? code ?? i),
      code,
      name:
        it.instrument_name ??
        (it as { fund_name?: string }).fund_name ??
        code ??
        "未命名基金",
      isQdii: it.is_qdii === true,
      returnAmount: toNumber(it.return_amount),
      returnRate: toNumber(it.return_rate),
      startDate: it.start_date ?? null,
      endDate: it.end_date ?? null,
      status: normalizeReturnStatus(it.status),
      staleReason: it.stale_reason ?? null,
      hasFlows: it.has_flows === true,
      weight: toNumber(it.weight),
    };
  });
}

export interface ReturnWindowView {
  key: ReturnWindowKey;
  label: string;
  /** 组合窗口收益金额，全部 stale 时为 null */
  returnAmount: number | null;
  /** 组合窗口收益率（小数），无可用基金时为 null */
  returnRate: number | null;
  /** 参与加权的金额占比 0~1 */
  coverage: number | null;
  availableCount: number | null;
  approximateCount: number | null;
  staleCount: number | null;
  /** 参与加权基金的最晚净值日期 */
  asOfEndDate: string | null;
  targetStartDate: string | null;
  items: FundReturnItemView[];
}

/** 归一化组合区间收益响应：按固定窗口顺序输出，缺失窗口容错为空 */
export function normalizePortfolioReturns(
  raw: PortfolioReturnsResponse | null | undefined
): ReturnWindowView[] {
  const windows =
    raw && typeof raw === "object" && raw.windows && typeof raw.windows === "object"
      ? raw.windows
      : {};
  return RETURN_WINDOWS.map((meta) => {
    const w: PortfolioReturnWindow | null | undefined = windows[meta.key];
    if (!w || typeof w !== "object") {
      return {
        key: meta.key,
        label: meta.label,
        returnAmount: null,
        returnRate: null,
        coverage: null,
        availableCount: null,
        approximateCount: null,
        staleCount: null,
        asOfEndDate: null,
        targetStartDate: null,
        items: [],
      };
    }
    const coverage = toNumber(w.coverage);
    return {
      key: meta.key,
      label: meta.label,
      returnAmount: toNumber(w.return_amount),
      returnRate: toNumber(w.return_rate),
      coverage:
        coverage === null ? null : Math.abs(coverage) > 1 ? coverage / 100 : coverage,
      availableCount: toNumber(w.available_count),
      approximateCount: toNumber(w.approximate_count),
      staleCount: toNumber(w.stale_count),
      asOfEndDate: w.as_of_end_date ?? null,
      targetStartDate: w.target_start_date ?? null,
      items: normalizeFundReturnItems(w.items),
    };
  });
}

/* ==================== 同步任务状态（GET /api/sync/status） ==================== */

/** 同步任务元信息：job_name -> 展示名（order 即展示顺序） */
export const SYNC_JOB_META: { job: string; label: string }[] = [
  { job: "fund_nav", label: "基金净值" },
  { job: "indices", label: "A股/港股指数" },
  { job: "us_indices", label: "美股指数" },
  { job: "news", label: "每日资讯" },
  { job: "holdings", label: "季度成分" },
  { job: "fund_catalog", label: "基金目录" },
  { job: "candidate_pool_nav", label: "候选池历史" },
  { job: "stock_daily", label: "A股日线" },
  { job: "paper", label: "模拟交易" },
];

export type SyncJobStatus = "success" | "partial" | "failed" | "paused" | "running" | "unknown";

export function normalizeSyncJobStatus(v: unknown): SyncJobStatus {
  const s = typeof v === "string" ? v.toLowerCase() : "";
  if (s === "success" || s === "ok" || s === "done") return "success";
  if (s === "partial" || s === "warning") return "partial";
  if (s === "failed" || s === "error" || s === "failure") return "failed";
  if (s === "paused" || s === "suspended") return "paused";
  if (s === "running" || s === "pending" || s === "in_progress") return "running";
  return "unknown";
}

export interface SyncJobView {
  job: string;
  label: string;
  status: SyncJobStatus;
  /** 最近一次开始时间（北京时间 ISO） */
  startedAt: string | null;
  /** 最近一次完成时间（北京时间 ISO） */
  finishedAt: string | null;
  dataDate: string | null;
  updated: number | null;
  total: number | null;
  failedCount: number | null;
  error: string | null;
  /** 下次计划运行时间（北京时间 ISO） */
  nextRun: string | null;
}

export interface SyncStatusView {
  /** 服务器当前时间（北京时间 ISO） */
  serverTime: string | null;
  jobs: SyncJobView[];
}

/** 归一化同步状态：按固定任务顺序输出，接口缺失/字段缺失时宽松容错 */
export function normalizeSyncStatus(
  raw: SyncStatusResponse | null | undefined
): SyncStatusView {
  const runs: SyncRunItem[] =
    raw && typeof raw === "object" && Array.isArray(raw.runs) ? raw.runs : [];
  const byJob = new Map<string, SyncRunItem>();
  for (const run of runs) {
    if (!run || typeof run !== "object") continue;
    const job = typeof run.job_name === "string" ? run.job_name : null;
    if (job && !byJob.has(job)) byJob.set(job, run);
  }
  const nextRuns =
    raw && typeof raw === "object" && raw.next_runs && typeof raw.next_runs === "object"
      ? raw.next_runs
      : {};

  // 已知任务按固定顺序输出；未知任务追加在末尾
  const jobs: SyncJobView[] = SYNC_JOB_META.map((meta) =>
    buildSyncJobView(meta.job, meta.label, byJob.get(meta.job), nextRuns[meta.job])
  );
  for (const [job, run] of byJob) {
    if (!SYNC_JOB_META.some((m) => m.job === job)) {
      jobs.push(buildSyncJobView(job, job, run, nextRuns[job]));
    }
  }
  return {
    serverTime:
      raw && typeof raw === "object" && typeof raw.server_time === "string"
        ? raw.server_time
        : null,
    jobs,
  };
}

function buildSyncJobView(
  job: string,
  label: string,
  run: SyncRunItem | undefined,
  nextRun: string | null | undefined
): SyncJobView {
  return {
    job,
    label,
    status: run ? normalizeSyncJobStatus(run.status) : "unknown",
    startedAt: run?.started_at ?? null,
    finishedAt: run?.finished_at ?? null,
    dataDate: run?.data_date ?? null,
    updated: run ? toNumber(run.updated) : null,
    total: run ? toNumber(run.total) : null,
    failedCount: run ? toNumber(run.failed) : null,
    error: run?.error ?? null,
    nextRun: typeof nextRun === "string" && nextRun ? nextRun : null,
  };
}

/** 北京时间格式化（YYYY-MM-DD HH:mm），用于同步状态等带时区的时间戳 */
export function fmtBeijingTime(v: unknown): string {
  if (typeof v !== "string" || !v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  try {
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(d);
    const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
    return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`;
  } catch {
    return d.toLocaleString("zh-CN", { hour12: false });
  }
}

/* ==================== 全市场基金发现（/api/discovery/* 与 /api/discovery-quant/*） ==================== */

export function fmtInt(v: unknown): string {
  const n = toNumber(v);
  if (n === null) return "—";
  return Math.round(n).toLocaleString("zh-CN");
}

/** 分类维度计数条目（目录类型/市场统计） */
export interface DiscoveryBreakdownItem {
  key: string;
  label: string;
  count: number | null;
}

function normalizeBreakdown(
  raw: DiscoveryCatalogBreakdown[] | Record<string, number | string> | null | undefined
): DiscoveryBreakdownItem[] {
  const items: DiscoveryBreakdownItem[] = [];
  if (Array.isArray(raw)) {
    raw.forEach((it, i) => {
      if (!it || typeof it !== "object") return;
      const label = it.label ?? it.name ?? it.type ?? it.market ?? it.key ?? `分类 ${i + 1}`;
      items.push({ key: `${label}-${i}`, label: String(label), count: toNumber(it.count ?? it.fund_count) });
    });
  } else if (raw && typeof raw === "object") {
    Object.entries(raw).forEach(([label, count]) => {
      items.push({ key: label, label, count: toNumber(count) });
    });
  }
  return items.sort((a, b) => (b.count ?? 0) - (a.count ?? 0));
}

export interface DiscoveryCatalogStatsView {
  total: number | null;
  byType: DiscoveryBreakdownItem[];
  byMarket: DiscoveryBreakdownItem[];
  updatedAt: string | null;
}

/** 归一化目录统计：兼容数组/字典两种分类形态，缺失字段容错为空 */
export function normalizeCatalogStats(
  raw: DiscoveryCatalogStats | null | undefined
): DiscoveryCatalogStatsView {
  const obj = raw && typeof raw === "object" ? raw : {};
  return {
    total: toNumber(obj.total ?? obj.total_count ?? obj.fund_count),
    byType: normalizeBreakdown(obj.by_type ?? obj.by_category ?? obj.types),
    byMarket: normalizeBreakdown(obj.by_market ?? obj.markets),
    updatedAt: obj.updated_at ?? obj.as_of ?? null,
  };
}

export interface DiscoveryCatalogFundView {
  key: string;
  code: string;
  name: string;
  fundType: string;
  market: string;
  latestNavDate: string | null;
  navCount: number | null;
}

export interface DiscoveryCatalogListView {
  total: number | null;
  items: DiscoveryCatalogFundView[];
}

/** 归一化目录列表：兼容 items/funds/results 三种包裹形态 */
export function normalizeCatalogList(
  raw: DiscoveryCatalogListResponse | DiscoveryCatalogFund[] | null | undefined
): DiscoveryCatalogListView {
  if (Array.isArray(raw)) {
    return { total: raw.length, items: normalizeCatalogFunds(raw) };
  }
  const obj = raw && typeof raw === "object" ? raw : {};
  const list = Array.isArray(obj.items)
    ? obj.items
    : Array.isArray(obj.funds)
      ? obj.funds
      : Array.isArray(obj.results)
        ? obj.results
        : [];
  return { total: toNumber(obj.total) ?? list.length, items: normalizeCatalogFunds(list) };
}

function normalizeCatalogFunds(list: DiscoveryCatalogFund[]): DiscoveryCatalogFundView[] {
  return list.map((f, i) => {
    const code = f.code ?? f.fund_code ?? "—";
    return {
      key: String(f.code ?? f.fund_code ?? i),
      code,
      name: f.name ?? f.fund_name ?? code,
      fundType: f.fund_type ?? f.type ?? f.category ?? "—",
      market: f.market ?? "—",
      latestNavDate: f.latest_nav_date ?? null,
      navCount: toNumber(f.nav_count ?? f.history_days),
    };
  });
}

export interface DiscoveryPoolView {
  key: string;
  id: string;
  name: string;
  description: string;
  memberCount: number | null;
  updatedAt: string | null;
}

export function normalizePoolId(raw: DiscoveryPool | null | undefined): string | null {
  if (!raw || typeof raw !== "object") return null;
  const id = raw.id ?? raw.pool_id;
  if (id === null || id === undefined || id === "") return null;
  return String(id);
}

/** 归一化候选池列表：兼容数组/items/pools 形态 */
export function normalizePools(
  raw: DiscoveryPool[] | { items?: DiscoveryPool[]; pools?: DiscoveryPool[] } | null | undefined
): DiscoveryPoolView[] {
  const list = Array.isArray(raw)
    ? raw
    : raw && typeof raw === "object"
      ? Array.isArray(raw.items)
        ? raw.items
        : Array.isArray(raw.pools)
          ? raw.pools
          : []
      : [];
  return list
    .map((p, i) => {
      const id = normalizePoolId(p) ?? `pool-${i}`;
      return {
        key: id,
        id,
        name: p.name ?? `候选池 ${id}`,
        description: p.description ?? "",
        memberCount: toNumber(p.member_count ?? p.size ?? p.fund_count),
        updatedAt: p.updated_at ?? p.as_of ?? p.created_at ?? null,
      };
    })
    .filter((p) => p.id !== "");
}

export interface DiscoveryPoolCoverageView {
  memberCount: number | null;
  coveredCount: number | null;
  /** 覆盖率 0-1，未知为 null */
  ratio: number | null;
  earliestNavDate: string | null;
  latestNavDate: string | null;
}

export interface DiscoveryPoolMemberView {
  key: string;
  code: string;
  name: string;
  fundType: string;
  market: string;
  firstNavDate: string | null;
  latestNavDate: string | null;
  navCount: number | null;
  navReady: boolean;
  /** 单只基金历史覆盖率 0-1，未知为 null */
  coverage: number | null;
}

export interface DiscoveryPoolDetailView {
  id: string | null;
  name: string;
  memberCount: number | null;
  members: DiscoveryPoolMemberView[];
  coverage: DiscoveryPoolCoverageView;
}

function normalizeRatio(v: unknown): number | null {
  const n = toNumber(v);
  if (n === null) return null;
  if (n < 0) return 0;
  if (n > 1 && n <= 100) return n / 100;
  return n > 1 ? 1 : n;
}

function normalizeCoverage(
  raw: DiscoveryPoolCoverage | null | undefined,
  members: DiscoveryPoolMemberView[],
  memberCount: number | null,
  summaryReady?: unknown
): DiscoveryPoolCoverageView {
  const obj = raw && typeof raw === "object" ? raw : {};
  const covered =
    toNumber(obj.covered_count ?? obj.full_count ?? summaryReady) ??
    (members.length > 0 ? members.filter((member) => member.navReady).length : null);
  const total = toNumber(obj.member_count) ?? memberCount ?? (members.length > 0 ? members.length : null);
  let ratio = normalizeRatio(obj.avg_coverage ?? obj.coverage ?? obj.progress);
  if (ratio === null && covered !== null && total !== null && total > 0) {
    ratio = covered / total;
  }
  if (ratio === null && members.length > 0) {
    const known = members.filter((m) => m.coverage !== null);
    if (known.length > 0) {
      ratio = known.reduce((acc, m) => acc + (m.coverage ?? 0), 0) / known.length;
    }
  }
  return {
    memberCount: total,
    coveredCount: covered,
    ratio,
    earliestNavDate: obj.earliest_nav_date ?? null,
    latestNavDate: obj.latest_nav_date ?? null,
  };
}

/** 归一化候选池详情：members/coverage 缺失时宽松容错 */
export function normalizePoolDetail(
  raw: DiscoveryPoolDetail | null | undefined,
  fallbackId?: string | null
): DiscoveryPoolDetailView {
  const obj = raw && typeof raw === "object" ? raw : {};
  const list = Array.isArray(obj.members)
    ? obj.members
    : Array.isArray(obj.funds)
      ? obj.funds
      : Array.isArray(obj.items)
        ? obj.items
        : [];
  const members: DiscoveryPoolMemberView[] = list.map((m: DiscoveryPoolMember, i: number) => {
    const code = m.code ?? m.fund_code ?? "—";
    return {
      key: String(m.code ?? m.fund_code ?? i),
      code,
      name: m.name ?? m.fund_name ?? code,
      fundType: m.fund_type ?? m.type ?? "—",
      market: m.market ?? "—",
      firstNavDate: m.first_nav_date ?? null,
      latestNavDate: m.latest_nav_date ?? null,
      navCount: toNumber(m.nav_count ?? m.nav_samples ?? m.history_days),
      navReady: m.nav_ready === true,
      coverage: normalizeRatio(m.coverage),
    };
  });
  const memberCount = toNumber(obj.member_count ?? obj.size ?? obj.fund_count) ?? (members.length > 0 ? members.length : null);
  return {
    id: normalizePoolId(obj) ?? fallbackId ?? null,
    name: obj.name ?? (fallbackId ? `候选池 ${fallbackId}` : "候选池"),
    memberCount,
    members,
    coverage: normalizeCoverage(
      obj.coverage ?? obj.history_coverage,
      members,
      memberCount,
      obj.summary?.nav_ready_count
    ),
  };
}

/* ---------- 发现 · 因子榜 ---------- */

export interface DiscoveryFactorView {
  key: string;
  code: string;
  name: string;
  fundType: string;
  market: string;
  family: string;
  /** 按排序因子在有效候选中的名次（1 起） */
  rank: number | null;
  /** 参与计算的净值样本数 */
  sampleCount: number | null;
  /** 近 21 个交易日收益（小数） */
  return1m: number | null;
  /** 近 63 个交易日收益（小数） */
  return3m: number | null;
  /** 近 252 个交易日收益（小数） */
  return1y: number | null;
  /** 近 756 个交易日收益（小数） */
  return3y: number | null;
  /** 绝对动量 12-1（小数） */
  momentum121: number | null;
  /** 同类（同市场层）内动量分位数 ∈[0,1] */
  quantile: number | null;
  annualVolatility: number | null;
  maxDrawdown: number | null;
  sharpe: number | null;
  sortino: number | null;
  calmar: number | null;
  cvar95: number | null;
}

export interface DiscoveryFactorsView {
  asOf: string | null;
  methodology: string;
  items: DiscoveryFactorView[];
  warnings: string[];
}

/** 归一化因子榜：兼容 items/factors/funds/results 形态 */
export function normalizeDiscoveryFactors(
  raw: DiscoveryFactorsResponse | DiscoveryFactorItem[] | null | undefined
): DiscoveryFactorsView {
  if (Array.isArray(raw)) {
    return { asOf: null, methodology: "", items: raw.map(normalizeFactorItem), warnings: [] };
  }
  const obj = raw && typeof raw === "object" ? raw : {};
  const list = Array.isArray(obj.items)
    ? obj.items
    : Array.isArray(obj.factors)
      ? obj.factors
      : Array.isArray(obj.funds)
        ? obj.funds
        : Array.isArray(obj.results)
          ? obj.results
          : [];
  return {
    asOf: obj.as_of ?? null,
    methodology: typeof obj.methodology === "string" ? obj.methodology : "",
    items: list.map(normalizeFactorItem),
    warnings: Array.isArray(obj.warnings)
      ? obj.warnings.filter((w): w is string => typeof w === "string" && w.length > 0)
      : [],
  };
}

function normalizeFactorItem(f: DiscoveryFactorItem, i: number): DiscoveryFactorView {
  const code = f.code ?? f.fund_code ?? "—";
  return {
    key: String(f.code ?? f.fund_code ?? i),
    code,
    name: f.name ?? f.fund_name ?? code,
    fundType: f.fund_type ?? f.type ?? f.market_label ?? "—",
    market: f.market ?? "—",
    family: f.family ?? "",
    rank: toNumber(f.rank),
    sampleCount: toNumber(f.sample_count),
    return1m: toNumber(f.return_1m),
    return3m: toNumber(f.return_3m),
    return1y: toNumber(f.return_1y),
    return3y: toNumber(f.return_3y),
    momentum121: toNumber(f.momentum_12_1 ?? f.momentum),
    quantile: toNumber(f.quantile),
    annualVolatility: toNumber(f.annual_volatility ?? f.annualized_volatility ?? f.volatility),
    maxDrawdown: toNumber(f.max_drawdown),
    sharpe: toNumber(f.sharpe ?? f.sharpe_ratio),
    sortino: toNumber(f.sortino),
    calmar: toNumber(f.calmar ?? f.calmar_ratio),
    cvar95: toNumber(f.cvar95),
  };
}

/* ---------- 发现 · 双动量 ---------- */

export interface DiscoveryDualMomentumViewItem {
  key: string;
  code: string;
  name: string;
  market: string;
  absoluteMomentum: number | null;
  relativeRank: number | null;
  /** 归一化到 0-100 的相对动量分位，未知为 null */
  percentile: number | null;
  pass: boolean;
}

export interface DiscoveryDualMomentumView {
  asOf: string | null;
  candidateCount: number | null;
  eligibleCount: number | null;
  methodology: string;
  items: DiscoveryDualMomentumViewItem[];
  warnings: string[];
}

/** 归一化双动量：绝对动量缺省用 momentum_12_1，pass 缺省按绝对动量 > 0 推断 */
export function normalizeDualMomentum(
  raw: DiscoveryDualMomentumResponse | DiscoveryDualMomentumItem[] | null | undefined
): DiscoveryDualMomentumView {
  if (Array.isArray(raw)) {
    return {
      asOf: null,
      candidateCount: raw.length,
      eligibleCount: null,
      methodology: "",
      items: raw.map(normalizeDualMomentumItem),
      warnings: [],
    };
  }
  const obj = raw && typeof raw === "object" ? raw : {};
  const list = Array.isArray(obj.items)
    ? obj.items
    : Array.isArray(obj.rankings)
      ? obj.rankings
      : Array.isArray(obj.results)
        ? obj.results
        : [];
  return {
    asOf: obj.as_of ?? null,
    candidateCount: toNumber(obj.candidate_count) ?? (list.length > 0 ? list.length : null),
    eligibleCount: toNumber(obj.eligible_count),
    methodology: typeof obj.methodology === "string" ? obj.methodology : "",
    items: list.map(normalizeDualMomentumItem),
    warnings: Array.isArray(obj.warnings)
      ? obj.warnings.filter((w): w is string => typeof w === "string" && w.length > 0)
      : [],
  };
}

function normalizeDualMomentumItem(m: DiscoveryDualMomentumItem, i: number): DiscoveryDualMomentumViewItem {
  const code = m.code ?? m.fund_code ?? "—";
  const abs = toNumber(m.absolute_momentum ?? m.momentum_12_1 ?? m.momentum);
  const pctRaw = toNumber(m.relative_percentile ?? m.percentile);
  const percentile =
    pctRaw === null ? null : Math.abs(pctRaw) <= 1 ? pctRaw * 100 : pctRaw;
  const pass =
    typeof m.pass === "boolean"
      ? m.pass
      : typeof m.eligible === "boolean"
        ? m.eligible
        : typeof m.selected === "boolean"
          ? m.selected
          : abs !== null
            ? abs > 0
            : false;
  return {
    key: String(m.code ?? m.fund_code ?? i),
    code,
    name: m.name ?? m.fund_name ?? code,
    market: m.market ?? "—",
    absoluteMomentum: abs,
    relativeRank: toNumber(m.relative_rank ?? m.rank),
    percentile,
    pass,
  };
}

/* ---------- 发现 · 当期入选信号 ---------- */

export interface DiscoverySignalViewItem {
  key: string;
  code: string;
  name: string;
  market: string;
  weight: number | null;
  momentum121: number | null;
  rank: number | null;
  reasons: string[];
}

export interface DiscoverySignalsView {
  asOf: string | null;
  tradeDate: string | null;
  candidateCount: number | null;
  eligibleCount: number | null;
  cashWeight: number | null;
  frozen: boolean;
  freezeReason: string | null;
  selected: DiscoverySignalViewItem[];
  warnings: string[];
}

/** 归一化当期入选信号：兼容 selected/signals/items 形态 */
export function normalizeDiscoverySignals(
  raw: DiscoverySignalsResponse | DiscoverySignalItem[] | null | undefined
): DiscoverySignalsView {
  if (Array.isArray(raw)) {
    return {
      asOf: null,
      tradeDate: null,
      candidateCount: raw.length,
      eligibleCount: null,
      cashWeight: null,
      frozen: false,
      freezeReason: null,
      selected: raw.map(normalizeDiscoverySignalItem),
      warnings: [],
    };
  }
  const obj = raw && typeof raw === "object" ? raw : {};
  const list = Array.isArray(obj.selected)
    ? obj.selected
    : Array.isArray(obj.signals)
      ? obj.signals
      : Array.isArray(obj.items)
        ? obj.items
        : [];
  return {
    asOf: obj.as_of ?? null,
    tradeDate: obj.trade_date ?? null,
    candidateCount: toNumber(obj.candidate_count),
    eligibleCount: toNumber(obj.eligible_count),
    cashWeight: toNumber(obj.cash_weight),
    frozen: obj.frozen === true,
    freezeReason: typeof obj.freeze_reason === "string" ? obj.freeze_reason : null,
    selected: list.map(normalizeDiscoverySignalItem),
    warnings: Array.isArray(obj.warnings)
      ? obj.warnings.filter((w): w is string => typeof w === "string" && w.length > 0)
      : [],
  };
}

function normalizeDiscoverySignalItem(s: DiscoverySignalItem, i: number): DiscoverySignalViewItem {
  const code = s.code ?? s.fund_code ?? "—";
  const reasons = [
    ...(Array.isArray(s.reasons)
      ? s.reasons.filter((r): r is string => typeof r === "string" && r.length > 0)
      : []),
    ...(typeof s.reason === "string" && s.reason ? [s.reason] : []),
  ];
  return {
    key: String(s.code ?? s.fund_code ?? i),
    code,
    name: s.name ?? s.fund_name ?? code,
    market: s.market ?? "—",
    weight: toNumber(s.weight),
    momentum121: toNumber(s.momentum_12_1 ?? s.momentum),
    rank: toNumber(s.rank_in_market ?? s.rank),
    reasons,
  };
}

/* ==================== 股票研究（/api/stocks/*） ==================== */

/** 从数组/包裹对象中提取列表的通用辅助 */
function pickList<T>(raw: unknown, keys: string[]): T[] {
  if (Array.isArray(raw)) return raw as T[];
  if (raw && typeof raw === "object") {
    const obj = raw as Record<string, unknown>;
    for (const k of keys) {
      if (Array.isArray(obj[k])) return obj[k] as T[];
    }
  }
  return [];
}

/** 股票数据可用性单项视图 */
export interface StockDataSourceView {
  key: string;
  label: string;
  available: boolean;
  availableAt: string | null;
  rows: number | null;
  message: string | null;
}

export interface StockDataStatusView {
  asOf: string | null;
  sources: StockDataSourceView[];
}

const STOCK_SOURCE_LABELS: Record<string, string> = {
  quotes: "行情",
  financials: "财务估值",
  factors: "因子",
  signals: "信号",
  universe: "股票宇宙",
  master: "主数据",
};

function normalizeSourceStatus(key: string, raw: boolean | StockDataSourceStatus | null | undefined): StockDataSourceView {
  if (typeof raw === "boolean") {
    return { key, label: STOCK_SOURCE_LABELS[key] ?? key, available: raw, availableAt: null, rows: null, message: null };
  }
  const obj = raw && typeof raw === "object" ? raw : {};
  const available =
    typeof obj.available === "boolean"
      ? obj.available
      : typeof obj.available_at === "string" && obj.available_at
        ? true
        : toNumber(obj.rows ?? obj.count) !== null
          ? (toNumber(obj.rows ?? obj.count) ?? 0) > 0
          : false;
  return {
    key,
    label: STOCK_SOURCE_LABELS[key] ?? key,
    available,
    availableAt: obj.available_at ?? obj.last_updated ?? obj.updated_at ?? null,
    rows: toNumber(obj.rows ?? obj.count),
    message: typeof obj.message === "string" ? obj.message : null,
  };
}

/** 归一化 GET /api/stocks/data/status：兼容顶层布尔/对象、sources 数组/字典、datasets 字典 */
export function normalizeStockDataStatus(raw: StockDataStatus | null | undefined): StockDataStatusView {
  const obj = raw && typeof raw === "object" ? raw : {};
  const sources: StockDataSourceView[] = [];
  const seen = new Set<string>();
  for (const key of ["quotes", "financials", "factors", "signals", "universe", "master"]) {
    const v = obj[key];
    if (v !== null && v !== undefined) {
      sources.push(normalizeSourceStatus(key, v as boolean | StockDataSourceStatus));
      seen.add(key);
    }
  }
  const src = obj.sources;
  if (Array.isArray(src)) {
    src.forEach((s, i) => {
      if (!s || typeof s !== "object") return;
      const key = typeof s.name === "string" && s.name ? s.name : `source-${i}`;
      if (seen.has(key)) return;
      sources.push(normalizeSourceStatus(key, s));
      seen.add(key);
    });
  } else if (src && typeof src === "object") {
    for (const [key, v] of Object.entries(src)) {
      if (seen.has(key)) continue;
      sources.push(normalizeSourceStatus(key, v as StockDataSourceStatus));
      seen.add(key);
    }
  }
  const ds = obj.datasets;
  if (ds && typeof ds === "object" && !Array.isArray(ds)) {
    for (const [key, v] of Object.entries(ds)) {
      if (seen.has(key)) continue;
      sources.push(normalizeSourceStatus(key, v as boolean | StockDataSourceStatus | null));
      seen.add(key);
    }
  }
  return {
    asOf: obj.as_of ?? obj.server_time ?? null,
    sources,
  };
}

/** 股票列表项视图（master/universe 共用） */
export interface StockListItemView {
  key: string;
  code: string;
  name: string;
  industry: string;
  market: string;
}

export function normalizeStockMaster(
  raw: StockMasterResponse | StockMasterItem[] | null | undefined
): { items: StockListItemView[]; industries: string[]; asOf: string | null } {
  const list = pickList<StockMasterItem>(raw, ["items", "stocks"]);
  const items = list.map((s, i) => {
    const code = s.code ?? s.symbol ?? s.ticker ?? "—";
    return {
      key: String(s.code ?? s.symbol ?? s.ticker ?? i),
      code,
      name: s.name ?? code,
      industry: s.industry ?? s.industry_sw ?? s.sector ?? "—",
      market: s.market ?? s.exchange ?? "—",
    };
  });
  const obj = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const industries = new Set<string>();
  if (Array.isArray(obj.industries)) {
    obj.industries.forEach((x) => {
      if (typeof x === "string" && x) industries.add(x);
    });
  }
  items.forEach((it) => {
    if (it.industry && it.industry !== "—") industries.add(it.industry);
  });
  return {
    items,
    industries: [...industries].sort((a, b) => a.localeCompare(b, "zh-CN")),
    asOf: typeof obj.as_of === "string" ? obj.as_of : null,
  };
}

export function normalizeStockUniverse(
  raw: StockUniverseResponse | StockUniverseItem[] | null | undefined
): { name: string | null; items: StockListItemView[]; industries: string[]; asOf: string | null } {
  const obj = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const list = pickList<StockUniverseItem>(raw, ["items", "stocks"]);
  const codes = Array.isArray(obj.codes) ? obj.codes.filter((c): c is string => typeof c === "string") : [];
  const items: StockListItemView[] =
    list.length > 0
      ? list.map((s, i) => {
          const code = s.code ?? "—";
          return {
            key: String(s.code ?? i),
            code,
            name: s.name ?? code,
            industry: s.industry ?? s.sector ?? "—",
            market: s.market ?? "—",
          };
        })
      : codes.map((code) => ({ key: code, code, name: code, industry: "—", market: "—" }));
  const industries = new Set<string>();
  if (Array.isArray(obj.industries)) {
    obj.industries.forEach((x) => {
      if (typeof x === "string" && x) industries.add(x);
    });
  }
  items.forEach((it) => {
    if (it.industry && it.industry !== "—") industries.add(it.industry);
  });
  return {
    name: typeof obj.universe === "string" ? obj.universe : typeof obj.name === "string" ? obj.name : null,
    items,
    industries: [...industries].sort((a, b) => a.localeCompare(b, "zh-CN")),
    asOf: typeof obj.as_of === "string" ? obj.as_of : null,
  };
}

/** 股票因子表行视图 */
export interface StockFactorRowView {
  key: string;
  code: string;
  name: string;
  industry: string;
  market: string;
  compositeScore: number | null;
  rank: number | null;
  percentile: number | null;
  momentum: number | null;
  value: number | null;
  quality: number | null;
  growth: number | null;
  volatility: number | null;
  size: number | null;
  pe: number | null;
  pb: number | null;
  roe: number | null;
  return20d: number | null;
  return60d: number | null;
  /** 额外因子（字典形态展开，展示在明细中） */
  extraFactors: { label: string; value: number | null }[];
}

export interface StockFactorsView {
  asOf: string | null;
  availableAt: string | null;
  items: StockFactorRowView[];
  warnings: string[];
}

const STOCK_FACTOR_LABELS: Record<string, string> = {
  momentum: "动量",
  value: "价值",
  quality: "质量",
  growth: "成长",
  volatility: "波动率",
  size: "市值",
  pe: "PE",
  pb: "PB",
  roe: "ROE",
  return_20d: "20 日收益",
  return_60d: "60 日收益",
};

export function stockFactorLabel(key: string): string {
  return STOCK_FACTOR_LABELS[key] ?? key;
}

function normalizeStockFactorRow(f: StockFactorItem, i: number): StockFactorRowView {
  const code = f.code ?? f.symbol ?? "—";
  const pctRaw = toNumber(f.percentile);
  const knownKeys = new Set([
    "code", "symbol", "name", "industry", "sector", "market", "composite_score", "score",
    "rank", "percentile", "factors", "as_of",
  ]);
  const extraFactors: { label: string; value: number | null }[] = [];
  if (f.factors && typeof f.factors === "object") {
    if (Array.isArray(f.factors)) {
      f.factors.forEach((x) => {
        if (!x || typeof x !== "object") return;
        const label = x.label ?? x.name ?? "因子";
        extraFactors.push({ label, value: toNumber(x.value ?? x.score) });
      });
    } else {
      for (const [k, v] of Object.entries(f.factors)) {
        extraFactors.push({ label: stockFactorLabel(k), value: toNumber(v) });
      }
    }
  }
  return {
    key: String(f.code ?? f.symbol ?? i),
    code,
    name: f.name ?? code,
    industry: f.industry ?? f.sector ?? "—",
    market: f.market ?? "—",
    compositeScore: toNumber(f.composite_score ?? f.score),
    rank: toNumber(f.rank),
    percentile: pctRaw === null ? null : Math.abs(pctRaw) <= 1 ? pctRaw * 100 : pctRaw,
    momentum: toNumber(f.momentum),
    value: toNumber(f.value),
    quality: toNumber(f.quality),
    growth: toNumber(f.growth),
    volatility: toNumber(f.volatility),
    size: toNumber(f.size),
    pe: toNumber(f.pe),
    pb: toNumber(f.pb),
    roe: toNumber(f.roe),
    return20d: toNumber(f.return_20d),
    return60d: toNumber(f.return_60d),
    extraFactors: extraFactors.filter((x) => !knownKeys.has(x.label)),
  };
}

/** 归一化 GET /api/stocks/research/factors：兼容数组与 items/factors/results 包裹形态 */
export function normalizeStockFactors(
  raw: StockFactorsResponse | StockFactorItem[] | null | undefined
): StockFactorsView {
  const list = pickList<StockFactorItem>(raw, ["items", "factors", "results"]);
  const obj = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  return {
    asOf: typeof obj.as_of === "string" ? obj.as_of : null,
    availableAt: typeof obj.available_at === "string" ? obj.available_at : null,
    items: list.map(normalizeStockFactorRow),
    warnings: Array.isArray(obj.warnings)
      ? obj.warnings.filter((w): w is string => typeof w === "string" && w.length > 0)
      : [],
  };
}

/** 股票信号行视图 */
export interface StockSignalRowView {
  key: string;
  code: string;
  name: string;
  signal: string;
  direction: ScreenerDirection;
  strength: number | null;
  tier: number | null;
  reason: string;
  industry: string;
  asOf: string | null;
}

export interface StockSignalsView {
  asOf: string | null;
  availableAt: string | null;
  items: StockSignalRowView[];
  warnings: string[];
}

/** 归一化 GET /api/stocks/research/signals */
export function normalizeStockSignals(
  raw: StockSignalsResponse | StockSignalItem[] | null | undefined
): StockSignalsView {
  const list = pickList<StockSignalItem>(raw, ["items", "signals", "results"]);
  const obj = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const items = list.map((s, i): StockSignalRowView => {
    const code = s.code ?? s.symbol ?? "—";
    const strength = toNumber(s.strength ?? s.score);
    const direction = normalizeDirection(s.direction ?? s.stance ?? s.signal, clampScore(s.score));
    const tierRaw = toNumber(s.tier);
    const reasons = [
      ...(Array.isArray(s.reasons)
        ? s.reasons.filter((r): r is string => typeof r === "string" && r.length > 0)
        : []),
      ...(typeof s.reason === "string" && s.reason ? [s.reason] : []),
      ...(typeof s.message === "string" && s.message ? [s.message] : []),
    ];
    return {
      key: String(s.code ?? s.symbol ?? i),
      code,
      name: s.name ?? code,
      signal: s.signal ?? s.stance ?? s.direction ?? "研究信号",
      direction,
      strength,
      tier: tierRaw !== null ? Math.round(tierRaw) : null,
      reason: reasons.join("；"),
      industry: s.industry ?? "—",
      asOf: s.as_of ?? s.date ?? null,
    };
  });
  return {
    asOf: typeof obj.as_of === "string" ? obj.as_of : null,
    availableAt: typeof obj.available_at === "string" ? obj.available_at : null,
    items,
    warnings: Array.isArray(obj.warnings)
      ? obj.warnings.filter((w): w is string => typeof w === "string" && w.length > 0)
      : [],
  };
}

/** 行情快照视图 */
export interface StockQuoteView {
  price: number | null;
  changePct: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  prevClose: number | null;
  volume: number | null;
  turnover: number | null;
  date: string | null;
  availableAt: string | null;
}

export function normalizeStockQuote(raw: StockQuote | null | undefined): StockQuoteView | null {
  if (!raw || typeof raw !== "object") return null;
  const price = toNumber(raw.price ?? raw.close);
  if (price === null && toNumber(raw.open) === null) return null;
  return {
    price,
    changePct: toNumber(raw.change_pct ?? raw.pct_change),
    open: toNumber(raw.open),
    high: toNumber(raw.high),
    low: toNumber(raw.low),
    prevClose: toNumber(raw.prev_close),
    volume: toNumber(raw.volume),
    turnover: toNumber(raw.turnover ?? raw.amount),
    date: raw.trade_date ?? raw.date ?? null,
    availableAt: raw.available_at ?? null,
  };
}

/** 历史行情点（用于 SVG 走势图） */
export interface StockPricePointView {
  date: string;
  close: number;
}

export function normalizeStockHistory(
  raw: StockPricePoint[] | { items?: StockPricePoint[]; history?: StockPricePoint[]; prices?: StockPricePoint[] } | null | undefined
): StockPricePointView[] {
  const list = pickList<StockPricePoint>(raw, ["items", "history", "prices"]);
  return list
    .map((p) => ({
      date: p.trade_date ?? p.date ?? "",
      close: toNumber(p.close) ?? Number.NaN,
    }))
    .filter((p) => p.date && Number.isFinite(p.close))
    .sort((a, b) => a.date.localeCompare(b.date));
}

/** 财务估值视图：label + value 对，便于直接渲染 */
export interface StockFinancialsView {
  availableAt: string | null;
  reportDate: string | null;
  metrics: { key: string; label: string; value: number | null; format: "percent" | "number" | "money" }[];
}

const FINANCIAL_METRIC_META: { key: string; label: string; format: "percent" | "number" | "money" }[] = [
  { key: "pe_ttm", label: "PE（TTM）", format: "number" },
  { key: "pe", label: "PE", format: "number" },
  { key: "pb", label: "PB", format: "number" },
  { key: "ps", label: "PS", format: "number" },
  { key: "market_cap", label: "总市值", format: "money" },
  { key: "float_market_cap", label: "流通市值", format: "money" },
  { key: "roe", label: "ROE", format: "percent" },
  { key: "roa", label: "ROA", format: "percent" },
  { key: "gross_margin", label: "毛利率", format: "percent" },
  { key: "net_margin", label: "净利率", format: "percent" },
  { key: "debt_ratio", label: "资产负债率", format: "percent" },
  { key: "revenue_yoy", label: "营收同比", format: "percent" },
  { key: "profit_yoy", label: "净利同比", format: "percent" },
  { key: "dividend_yield", label: "股息率", format: "percent" },
  { key: "eps", label: "EPS", format: "number" },
];

export function normalizeStockFinancials(raw: StockFinancials | null | undefined): StockFinancialsView | null {
  if (!raw || typeof raw !== "object") return null;
  const metrics = FINANCIAL_METRIC_META.map((m) => ({
    ...m,
    value: toNumber(raw[m.key] ?? (m.key === "revenue_yoy" ? raw.revenue_yoy : m.key === "profit_yoy" ? (raw.profit_yoy ?? raw.net_profit_yoy) : undefined)),
  })).filter((m) => m.value !== null);
  if (metrics.length === 0 && !raw.available_at && !raw.report_date) return null;
  return {
    availableAt: raw.available_at ?? null,
    reportDate: raw.report_date ?? raw.period ?? null,
    metrics,
  };
}

/** 股票详情聚合视图 */
export interface StockDetailView {
  code: string;
  name: string;
  industry: string;
  market: string;
  exchange: string;
  quote: StockQuoteView | null;
  history: StockPricePointView[];
  financials: StockFinancialsView | null;
  factors: { label: string; value: number | null }[];
  availableAt: string | null;
}

export function normalizeStockDetail(raw: StockDetail | null | undefined, fallbackCode: string): StockDetailView {
  const obj = raw && typeof raw === "object" ? raw : ({} as StockDetail);
  const code = obj.code ?? obj.symbol ?? fallbackCode;
  const quote = normalizeStockQuote(obj.quote ?? null) ??
    (toNumber(obj.price) !== null
      ? normalizeStockQuote({ price: obj.price, change_pct: obj.change_pct } as StockQuote)
      : null);
  const history = normalizeStockHistory(
    Array.isArray(obj.history)
      ? obj.history
      : Array.isArray(obj.prices)
        ? obj.prices
        : Array.isArray(obj.candles)
          ? obj.candles
          : []
  );
  const factors: { label: string; value: number | null }[] = [];
  if (obj.factors && typeof obj.factors === "object" && !Array.isArray(obj.factors)) {
    for (const [k, v] of Object.entries(obj.factors as Record<string, unknown>)) {
      const n = toNumber(v);
      if (n !== null) factors.push({ label: stockFactorLabel(k), value: n });
    }
  }
  return {
    code,
    name: obj.name ?? code,
    industry: obj.industry ?? obj.sector ?? "—",
    market: obj.market ?? "—",
    exchange: obj.exchange ?? "—",
    quote,
    history,
    financials: normalizeStockFinancials(obj.financials ?? null),
    factors,
    availableAt: obj.available_at ?? obj.as_of ?? null,
  };
}

/** 研究组合持仓行视图 */
export interface ResearchHoldingView {
  key: string;
  code: string;
  name: string;
  weight: number | null;
  score: number | null;
  reason: string;
  industry: string;
  market: string;
}

export interface ResearchPortfolioView {
  key: string;
  name: string;
  kind: string;
  description: string;
  methodology: string;
  asOf: string | null;
  holdings: ResearchHoldingView[];
}

export interface ResearchPortfoliosView {
  asOf: string | null;
  portfolios: ResearchPortfolioView[];
  warnings: string[];
}

function normalizeResearchHolding(h: ResearchPortfolioHolding, i: number): ResearchHoldingView {
  const code = h.code ?? h.fund_code ?? "—";
  const reasons = [
    ...(Array.isArray(h.reasons)
      ? h.reasons.filter((r): r is string => typeof r === "string" && r.length > 0)
      : []),
    ...(typeof h.reason === "string" && h.reason ? [h.reason] : []),
  ];
  return {
    key: String(h.code ?? h.fund_code ?? i),
    code,
    name: h.name ?? h.fund_name ?? code,
    weight: toNumber(h.weight),
    score: toNumber(h.score),
    reason: reasons.join("；"),
    industry: h.industry ?? "—",
    market: h.market ?? "—",
  };
}

/** 归一化 GET /api/research/portfolios：兼容数组与 items/portfolios/results 包裹形态 */
export function normalizeResearchPortfolios(
  raw: ResearchPortfoliosResponse | ResearchPortfolio[] | null | undefined
): ResearchPortfoliosView {
  const list = pickList<ResearchPortfolio>(raw, ["items", "portfolios", "results"]);
  const obj = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const portfolios = list.map((p, i): ResearchPortfolioView => {
    const holdings = Array.isArray(p.holdings)
      ? p.holdings
      : Array.isArray(p.items)
        ? p.items
        : Array.isArray(p.constituents)
          ? p.constituents
          : [];
    return {
      key: String(p.id ?? p.name ?? i),
      name: p.name ?? p.title ?? `组合 ${i + 1}`,
      kind: p.kind ?? p.type ?? "",
      description: p.description ?? "",
      methodology: p.methodology ?? "",
      asOf: p.as_of ?? p.updated_at ?? null,
      holdings: holdings.map(normalizeResearchHolding),
    };
  });
  return {
    asOf: typeof obj.as_of === "string" ? obj.as_of : null,
    portfolios,
    warnings: Array.isArray(obj.warnings)
      ? obj.warnings.filter((w): w is string => typeof w === "string" && w.length > 0)
      : [],
  };
}
