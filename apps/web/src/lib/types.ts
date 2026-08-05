/**
 * API 数据类型定义。
 * 字段使用宽松的可选类型，以兼容 FastAPI 后端响应结构可能出现的差异；
 * 展示层通过 normalize.ts 中的归一化函数兜底取值。
 */

export interface PortfolioSummary {
  total_market_value?: number | string | null;
  total_cost?: number | string | null;
  total_profit?: number | string | null;
  total_return_rate?: number | string | null;
  position_count?: number | string | null;
  fund_count?: number | string | null;
  as_of?: string | null;
  snapshot_date?: string | null;
  estimated_return?: number | string | null;
  estimated_return_rate?: number | string | null;
  year_return?: number | string | null;
  previous_year_return?: number | string | null;
  [key: string]: unknown;
}

export interface Position {
  id?: number | string;
  fund_code?: string | null;
  fund_name?: string | null;
  shares?: number | string | null;
  cost_price?: number | string | null;
  nav?: number | string | null;
  market_value?: number | string | null;
  profit?: number | string | null;
  return_rate?: number | string | null;
  profit_available?: boolean | null;
  cost_coverage_rate?: number | string | null;
  [key: string]: unknown;
}

export interface Transaction {
  id?: number | string;
  fund_code?: string | null;
  fund_name?: string | null;
  type?: string | null;
  transaction_type?: string | null;
  amount?: number | string | null;
  shares?: number | string | null;
  nav?: number | string | null;
  fee?: number | string | null;
  date?: string | null;
  transaction_date?: string | null;
  status?: string | null;
  [key: string]: unknown;
}

export interface PortfolioSnapshot {
  snapshot_date?: string | null;
  total_cost?: number | string | null;
  total_market_value?: number | string | null;
  total_profit?: number | string | null;
  [key: string]: unknown;
}

export interface FundNavHistoryItem {
  nav_date?: string | null;
  unit_nav?: number | string | null;
  accumulated_nav?: number | string | null;
  daily_growth_rate?: number | string | null;
  [key: string]: unknown;
}

export interface FundTradePoint {
  trade_date?: string | null;
  type?: string | null;
  amount?: number | string | null;
  shares?: number | string | null;
  [key: string]: unknown;
}

export interface FundNavHistoryResponse {
  fund_code?: string | null;
  fund_name?: string | null;
  items?: FundNavHistoryItem[] | null;
  trades?: FundTradePoint[] | null;
  total?: number | string | null;
  [key: string]: unknown;
}

export interface ImportPreview {
  import_id?: number | string;
  id?: number | string;
  file_name?: string | null;
  filename?: string | null;
  snapshot_date?: string | null;
  as_of?: string | null;
  positions?: Position[] | null;
  transactions?: Transaction[] | null;
  summary?: PortfolioSummary | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

export interface CommitResult {
  ok?: boolean;
  message?: string | null;
  positions_written?: number | null;
  transactions_written?: number | null;
  [key: string]: unknown;
}

/* ==================== 量化分析 ==================== */

/** 组合量化指标（GET /api/quant/portfolio） */
export interface QuantHoldingMetric {
  code?: string | null;
  name?: string | null;
  market_value?: number | string | null;
  weight?: number | string | null;
  trend_signal?: string | null;
  return_20d?: number | string | null;
  return_60d?: number | string | null;
  max_drawdown?: number | string | null;
  [key: string]: unknown;
}

export interface QuantPortfolio {
  total_market_value?: number | string | null;
  total_cost?: number | string | null;
  total_profit?: number | string | null;
  total_return_rate?: number | string | null;
  annualized_return?: number | string | null;
  annualized_volatility?: number | string | null;
  max_drawdown?: number | string | null;
  sharpe_ratio?: number | string | null;
  methodology?: string | null;
  calmar_ratio?: number | string | null;
  win_rate?: number | string | null;
  benchmark_return?: number | string | null;
  excess_return?: number | string | null;
  position_count?: number | string | null;
  as_of?: string | null;
  snapshot_date?: string | null;
  concentration_top1?: number | string | null;
  concentration_top3?: number | string | null;
  hhi?: number | string | null;
  holdings?: QuantHoldingMetric[] | null;
  signals?: QuantSignal[] | null;
  [key: string]: unknown;
}

/** 单只基金量化指标（GET /api/quant/funds 列表项） */
export interface QuantFundMetrics {
  fund_code?: string | null;
  fund_name?: string | null;
  code?: string | null;
  name?: string | null;
  annualized_return?: number | string | null;
  annual_volatility?: number | string | null;
  return_20d?: number | string | null;
  return_60d?: number | string | null;
  return_250d?: number | string | null;
  return_1y?: number | string | null;
  sharpe?: number | string | null;
  trend_signal?: string | null;
  annualized_volatility?: number | string | null;
  max_drawdown?: number | string | null;
  sharpe_ratio?: number | string | null;
  win_rate?: number | string | null;
  return_rate?: number | string | null;
  market_value?: number | string | null;
  advice?: FundAdvice | null;
  [key: string]: unknown;
}

/** 回测曲线上的单个净值点 */
export interface BacktestPoint {
  date?: string | null;
  nav?: number | string | null;
  value?: number | string | null;
  benchmark?: number | string | null;
  [key: string]: unknown;
}

/** 回测请求体（POST /api/quant/backtest） */
export interface BacktestRequest {
  fund_code?: string;
  fund_codes?: string[];
  code?: string;
  strategy?: string;
  start_date?: string;
  end_date?: string;
  [key: string]: unknown;
}

/** 回测汇总指标 */
export interface BacktestSummary {
  total_return?: number | string | null;
  annualized_return?: number | string | null;
  annualized_volatility?: number | string | null;
  max_drawdown?: number | string | null;
  sharpe_ratio?: number | string | null;
  benchmark_return?: number | string | null;
  excess_return?: number | string | null;
  trades?: number | string | null;
  [key: string]: unknown;
}

/** 回测响应（POST /api/quant/backtest） */
export interface BacktestResult {
  fund_code?: string | null;
  fund_name?: string | null;
  strategy?: string | null;
  strategy_name?: string | null;
  name?: string | null;
  initial_capital?: number | string | null;
  final_value?: number | string | null;
  total_return?: number | string | null;
  annual_return?: number | string | null;
  max_drawdown?: number | string | null;
  sharpe?: number | string | null;
  trade_count?: number | string | null;
  start_date?: string | null;
  end_date?: string | null;
  summary?: BacktestSummary | null;
  curve?: BacktestPoint[] | null;
  points?: BacktestPoint[] | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

/* ==================== 研究信号 ==================== */

/** 单条可解释信号（GET /api/quant/signals 列表项） */
export interface QuantSignal {
  id?: number | string;
  fund_code?: string | null;
  fund_name?: string | null;
  signal?: string | null;
  direction?: string | null;
  severity?: string | null;
  level?: string | null;
  category?: string | null;
  message?: string | null;
  source?: string | null;
  related_codes?: string[] | null;
  evidence?: Record<string, unknown> | null;
  as_of?: string | null;
  rule?: string | null;
  rule_name?: string | null;
  reason?: string | null;
  description?: string | null;
  triggered_at?: string | null;
  date?: string | null;
  metrics?: Record<string, unknown> | null;
  [key: string]: unknown;
}

/* ==================== 规则模型：综合信号 / 五档模型 ==================== */

/** 综合信号因子明细 */
export interface ScreenerFactor {
  name?: string | null;
  label?: string | null;
  contribution?: number | string | null;
  score?: number | string | null;
  value?: number | string | null;
  reason?: string | null;
  description?: string | null;
  [key: string]: unknown;
}

/** 规则模型信号（GET /api/quant/screener/signals 列表项） */
export interface ScreenerSignal {
  fund_code?: string | null;
  fund_name?: string | null;
  code?: string | null;
  name?: string | null;
  /** 原始横截面综合分；五档方向优先读取 tier。 */
  score?: number | string | null;
  composite_score?: number | string | null;
  /** 分数在候选池中的分位数，0-1 或 0-100 */
  percentile?: number | string | null;
  score_percentile?: number | string | null;
  quantile?: number | string | null;
  market?: string | null;
  direction?: string | null;
  stance?: string | null;
  tier?: number | string | null;
  target_weight?: number | string | null;
  /** 是否进入目标组合（综合分前 top_n 只分配权重；其余仅参与分析） */
  in_target?: boolean | null;
  positive_factors?: (ScreenerFactor | string)[] | null;
  negative_factors?: (ScreenerFactor | string)[] | null;
  factors?: (ScreenerFactor | string)[] | null;
  reasons?: (string | null)[] | null;
  reason?: string | null;
  as_of?: string | null;
  date?: string | null;
  [key: string]: unknown;
}

/** 规则模型筛选响应元信息（GET /api/quant/screener/signals 非数组形态） */
export interface ScreenerMeta {
  asOf: string | null;
  /** 参与分析的基金数（全部样本满足的候选） */
  selectedCount: number | null;
  /** 进入目标组合的基金数（综合分前 top_n 只） */
  allocationCount: number | null;
  /** 样本满足的候选基金数（权益市场，参与横截面排名） */
  candidateCount: number | null;
  /** 因样本不足被剔除的基金数 */
  excludedCount: number | null;
  /** 观察池（黄金/债券/货币/其他海外）基金数 */
  observeCount: number | null;
  warnings: string[];
}

/** Walk-Forward 请求体（POST /api/quant/walkforward） */
export interface WalkForwardRequest {
  train_window?: number;
  test_window?: number;
  train?: number;
  test?: number;
  train_size?: number;
  test_size?: number;
  top_n?: number;
  rebalance?: string;
  [key: string]: unknown;
}

/** Walk-Forward 曲线上的单个净值点 */
export interface WalkForwardPoint {
  date?: string | null;
  nav?: number | string | null;
  value?: number | string | null;
  strategy?: number | string | null;
  benchmark?: number | string | null;
  benchmark_nav?: number | string | null;
  equal_weight?: number | string | null;
  [key: string]: unknown;
}

/** Walk-Forward 汇总指标 */
export interface WalkForwardSummary {
  annualized_return?: number | string | null;
  annual_return?: number | string | null;
  max_drawdown?: number | string | null;
  sharpe_ratio?: number | string | null;
  sharpe?: number | string | null;
  win_rate?: number | string | null;
  turnover?: number | string | null;
  turnover_rate?: number | string | null;
  excess_return?: number | string | null;
  benchmark_annualized_return?: number | string | null;
  benchmark_return?: number | string | null;
  total_return?: number | string | null;
  [key: string]: unknown;
}

/** Walk-Forward 单个滚动窗口段 */
export interface WalkForwardSegment {
  index?: number | string | null;
  train_start?: string | null;
  train_end?: string | null;
  test_start?: string | null;
  test_end?: string | null;
  annualized_return?: number | string | null;
  annual_return?: number | string | null;
  return_rate?: number | string | null;
  test_return?: number | string | null;
  benchmark_return?: number | string | null;
  excess_return?: number | string | null;
  max_drawdown?: number | string | null;
  sharpe_ratio?: number | string | null;
  sharpe?: number | string | null;
  win_rate?: number | string | null;
  turnover?: number | string | null;
  holdings?: string[] | Record<string, number | string> | null;
  [key: string]: unknown;
}

/** Walk-Forward 响应（POST /api/quant/walkforward） */
export interface WalkForwardResult {
  summary?: WalkForwardSummary | null;
  strategy?: WalkForwardSummary | null;
  benchmark?: WalkForwardSummary | null;
  excess_return?: number | string | null;
  turnover?: number | string | null;
  metrics?: WalkForwardSummary | null;
  curve?: WalkForwardPoint[] | null;
  points?: WalkForwardPoint[] | null;
  equity_curve?: WalkForwardPoint[] | null;
  segments?: WalkForwardSegment[] | null;
  train_window?: number | string | null;
  test_window?: number | string | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

/* ==================== 稳健组合策略 V2（/api/quant/v2/*） ==================== */

/** V2 费用模型配置（默认零费用，接口预留） */
export interface FeeModelV2 {
  buy_fee_rate?: number | string | null;
  sell_fee_rate?: number | string | null;
  slippage_rate?: number | string | null;
  min_fee?: number | string | null;
  [key: string]: unknown;
}

/** V2 回测请求体（POST /api/quant/v2/backtest） */
export interface BacktestV2Request {
  candidate_codes?: string[] | null;
  start_date?: string | null;
  end_date?: string | null;
  initial_capital?: number;
  top_n?: number;
  rebalance_interval_months?: number;
  target_vol?: number | null;
  max_fund_weight?: number | null;
  max_family_weight?: number | null;
  max_qdii_weight?: number | null;
  fee_model?: FeeModelV2;
  [key: string]: unknown;
}

/** V2 回测曲线点：策略与基准同日期对齐（初始净值均为 1） */
export interface BacktestV2CurvePoint {
  date?: string | null;
  strategy?: number | string | null;
  benchmark?: number | string | null;
  nav?: number | string | null;
  value?: number | string | null;
  [key: string]: unknown;
}

/** V2 单笔成交记录（T+1 / QDII T+2） */
export interface TradeV2 {
  signal_date?: string | null;
  fill_date?: string | null;
  code?: string | null;
  name?: string | null;
  action?: string | null;
  weight_change?: number | string | null;
  amount?: number | string | null;
  fee?: number | string | null;
  price?: number | string | null;
  settle_lag?: number | string | null;
  reason?: string | null;
  [key: string]: unknown;
}

/** V2 一次月频调仓明细 */
export interface RebalanceV2 {
  index?: number | string | null;
  signal_date?: string | null;
  fill_date?: string | null;
  holdings?: Record<string, number | string> | null;
  cash_weight?: number | string | null;
  turnover?: number | string | null;
  frozen?: boolean | null;
  /** hrp / inverse_vol / equal_weight / frozen / 多市场组合如 "equal_weight+hrp" */
  allocation_method?: string | null;
  realized_vol?: number | string | null;
  vol_scalar?: number | string | null;
  reason?: string | null;
  [key: string]: unknown;
}

/** V2 策略/基准汇总指标 */
export interface BacktestV2Summary {
  total_return?: number | string | null;
  annual_return?: number | string | null;
  annualized_return?: number | string | null;
  annual_volatility?: number | string | null;
  annualized_volatility?: number | string | null;
  max_drawdown?: number | string | null;
  sharpe?: number | string | null;
  sharpe_ratio?: number | string | null;
  win_rate?: number | string | null;
  [key: string]: unknown;
}

/** V2 回测响应（POST /api/quant/v2/backtest） */
export interface BacktestV2Result {
  params?: Record<string, unknown> | null;
  start_date?: string | null;
  end_date?: string | null;
  initial_capital?: number | string | null;
  strategy?: BacktestV2Summary | null;
  benchmark?: BacktestV2Summary | null;
  excess_return?: number | string | null;
  avg_turnover?: number | string | null;
  turnover?: number | string | null;
  rebalance_count?: number | string | null;
  frozen_count?: number | string | null;
  total_fees?: number | string | null;
  curve?: BacktestV2CurvePoint[] | null;
  points?: BacktestV2CurvePoint[] | null;
  rebalances?: RebalanceV2[] | null;
  trades?: TradeV2[] | null;
  methodology?: string | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

/** V2 当期入选基金信号（GET /api/quant/v2/signals 列表项） */
export interface SignalV2Item {
  code?: string | null;
  fund_code?: string | null;
  name?: string | null;
  fund_name?: string | null;
  market?: string | null;
  family?: string | null;
  momentum_12_1?: number | string | null;
  momentum?: number | string | null;
  rank_in_market?: number | string | null;
  market_candidates?: number | string | null;
  weight?: number | string | null;
  reasons?: (string | null)[] | null;
  [key: string]: unknown;
}

/** V2 当期信号响应（GET /api/quant/v2/signals） */
export interface SignalsV2Response {
  as_of?: string | null;
  trade_date?: string | null;
  methodology?: string | null;
  candidate_count?: number | string | null;
  eligible_count?: number | string | null;
  selected?: SignalV2Item[] | null;
  signals?: SignalV2Item[] | null;
  items?: SignalV2Item[] | null;
  cash_weight?: number | string | null;
  realized_vol?: number | string | null;
  vol_scalar?: number | string | null;
  frozen?: boolean | null;
  freeze_reason?: string | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

/* ==================== 统计验证（/api/quant/validation 与 /api/quant/snapshot） ==================== */

/** 验证请求的费用模型（缺省：买 0.15%、卖 0.5%、7 日内 1.5%） */
export interface ValidationCostModel {
  buy_fee_rate?: number;
  sell_fee_rate?: number;
  short_term_sell_fee_rate?: number;
  short_term_days?: number;
  [key: string]: unknown;
}

/** 验证请求体（POST /api/quant/validation） */
export interface ValidationRequest {
  candidate_codes?: string[] | null;
  as_of?: string | null;
  window?: { train_window?: number; test_window?: number; step?: number };
  top_n?: number;
  rebalance_interval?: number;
  include_costs?: boolean;
  cost_model?: ValidationCostModel;
  trial_count?: number;
  bootstrap_resamples?: number;
  block_length?: number | null;
  seed?: number;
  [key: string]: unknown;
}

/** 样本外风险与收益指标（策略或基准） */
export interface ValidationRiskMetrics {
  total_return?: number | string | null;
  annual_return?: number | string | null;
  sharpe?: number | string | null;
  max_drawdown?: number | string | null;
  cvar95?: number | string | null;
  calmar?: number | string | null;
  win_rate?: number | string | null;
  [key: string]: unknown;
}

/** 因子预测有效性：Rank IC 与五档收益单调性 */
export interface ValidationPredictiveness {
  rank_ic_mean?: number | string | null;
  rank_ic_count?: number | string | null;
  quintile_returns?: (number | string | null)[] | null;
  quintile_spread?: number | string | null;
  quintile_kendall_tau?: number | string | null;
  quintile_monotonic?: boolean | null;
  [key: string]: unknown;
}

/** 多重检验与抽样稳健性（DSR / White Reality Check） */
export interface ValidationRobustness {
  trial_count?: number | string | null;
  skew?: number | string | null;
  kurtosis?: number | string | null;
  sharpe_std?: number | string | null;
  expected_max_sharpe?: number | string | null;
  deflated_sharpe?: number | string | null;
  reality_check_p?: number | string | null;
  reality_check_stat?: number | string | null;
  reality_check_null_mean?: number | string | null;
  bootstrap_resamples?: number | string | null;
  block_length?: number | string | null;
  [key: string]: unknown;
}

/** 参数邻域稳定性 */
export interface ValidationNeighborhood {
  center_sharpe?: number | string | null;
  neighborhood_quantile?: number | string | null;
  band_low?: number | string | null;
  band_high?: number | string | null;
  neighbor_count?: number | string | null;
  neighbors?: Record<string, number | string | null> | null;
  [key: string]: unknown;
}

/** 费用口径与实际扣费摘要 */
export interface ValidationCostSummary {
  include_costs?: boolean | null;
  buy_fee_rate?: number | string | null;
  sell_fee_rate?: number | string | null;
  short_term_sell_fee_rate?: number | string | null;
  short_term_days?: number | string | null;
  total_fee_ratio?: number | string | null;
  trade_days?: number | string | null;
  sell_fee_basis?: string | null;
  [key: string]: unknown;
}

/** 验证中一只基金的数据可用性（as_of 视角） */
export interface ValidationFundSnapshot {
  code?: string | null;
  name?: string | null;
  is_qdii?: boolean | null;
  lag_days?: number | string | null;
  latest_nav_date?: string | null;
  effective_date?: string | null;
  [key: string]: unknown;
}

/** 验证响应（POST /api/quant/validation） */
export interface ValidationResponse {
  as_of?: string | null;
  candidate_codes?: string[] | null;
  start_date?: string | null;
  end_date?: string | null;
  sample_count?: number | string | null;
  oos_count?: number | string | null;
  strategy?: ValidationRiskMetrics | null;
  benchmark?: ValidationRiskMetrics | null;
  information_ratio?: number | string | null;
  excess_return?: number | string | null;
  predictiveness?: ValidationPredictiveness | null;
  robustness?: ValidationRobustness | null;
  neighborhood?: ValidationNeighborhood | null;
  costs?: ValidationCostSummary | null;
  fund_snapshots?: ValidationFundSnapshot[] | null;
  methodology?: string | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

/** as_of 快照中一只基金的可用日期信息 */
export interface SnapshotFundInfo {
  code?: string | null;
  name?: string | null;
  is_qdii?: boolean | null;
  lag_days?: number | string | null;
  first_nav_date?: string | null;
  latest_nav_date?: string | null;
  nav_count?: number | string | null;
  effective_date?: string | null;
  [key: string]: unknown;
}

/** as_of 可用日期快照响应（GET /api/quant/snapshot） */
export interface SnapshotResponse {
  as_of?: string | null;
  trade_days?: string[] | null;
  trade_day_count?: number | string | null;
  truncated?: boolean | null;
  funds?: SnapshotFundInfo[] | null;
  [key: string]: unknown;
}

/* ==================== 每日资讯 ==================== */

export type NewsScope = "related" | "market";

/** 单条资讯（GET /api/news?scope=... 列表项） */
export interface NewsItem {
  id?: number | string;
  title?: string | null;
  summary?: string | null;
  source?: string | null;
  url?: string | null;
  published_at?: string | null;
  sentiment?: string | null;
  related_funds?: string[] | null;
  related_codes?: string[] | null;
  tags?: string[] | null;
  [key: string]: unknown;
}

/* ==================== 模拟交易（虚拟盘，非真实交易） ==================== */

/** 模拟账户汇总（GET /api/paper/summary） */
export interface PaperSummary {
  strategy?: {
    version_id?: number | string | null;
    name?: string | null;
    status?: string | null;
    initial_capital?: number | string | null;
    rebalance_interval?: number | string | null;
    fee_rate?: number | string | null;
    top_n?: number | string | null;
  } | null;
  initial_capital?: number | string | null;
  total_value?: number | string | null;
  total_market_value?: number | string | null;
  equity?: number | string | null;
  cash?: number | string | null;
  cash_available?: number | string | null;
  market_value?: number | string | null;
  position_value?: number | string | null;
  total_profit?: number | string | null;
  profit?: number | string | null;
  total_return?: number | string | null;
  total_return_rate?: number | string | null;
  return_rate?: number | string | null;
  daily_profit?: number | string | null;
  today_profit?: number | string | null;
  day_profit?: number | string | null;
  daily_return?: number | string | null;
  daily_return_rate?: number | string | null;
  today_return?: number | string | null;
  position_count?: number | string | null;
  trade_count?: number | string | null;
  benchmark_return?: number | string | null;
  benchmark_return_rate?: number | string | null;
  as_of?: string | null;
  date?: string | null;
  updated_at?: string | null;
  last_run_date?: string | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

/** 模拟净值曲线上的单个点（GET /api/paper/history 列表项） */
export interface PaperHistoryPoint {
  date?: string | null;
  as_of?: string | null;
  total_value?: number | string | null;
  value?: number | string | null;
  equity?: number | string | null;
  nav?: number | string | null;
  cash?: number | string | null;
  benchmark?: number | string | null;
  benchmark_value?: number | string | null;
  equal_weight?: number | string | null;
  benchmark_nav?: number | string | null;
  [key: string]: unknown;
}

/** 当前虚拟持仓（GET /api/paper/positions 列表项） */
export interface PaperPosition {
  id?: number | string;
  fund_code?: string | null;
  code?: string | null;
  fund_name?: string | null;
  name?: string | null;
  shares?: number | string | null;
  quantity?: number | string | null;
  cost_price?: number | string | null;
  avg_cost?: number | string | null;
  nav?: number | string | null;
  price?: number | string | null;
  market_value?: number | string | null;
  value?: number | string | null;
  weight?: number | string | null;
  profit?: number | string | null;
  return_rate?: number | string | null;
  daily_profit?: number | string | null;
  today_profit?: number | string | null;
  [key: string]: unknown;
}

/** 虚拟成交记录（GET /api/paper/trades 列表项） */
export interface PaperTrade {
  id?: number | string;
  fund_code?: string | null;
  code?: string | null;
  fund_name?: string | null;
  name?: string | null;
  side?: string | null;
  direction?: string | null;
  type?: string | null;
  action?: string | null;
  shares?: number | string | null;
  quantity?: number | string | null;
  price?: number | string | null;
  nav?: number | string | null;
  amount?: number | string | null;
  fee?: number | string | null;
  reason?: string | null;
  signal?: string | null;
  date?: string | null;
  trade_date?: string | null;
  executed_at?: string | null;
  created_at?: string | null;
  [key: string]: unknown;
}

/** 最新模拟信号（GET /api/paper/signals 列表项，复用五档/综合信号结构） */
export interface PaperSignal {
  id?: number | string;
  fund_code?: string | null;
  code?: string | null;
  fund_name?: string | null;
  name?: string | null;
  signal?: string | null;
  direction?: string | null;
  stance?: string | null;
  tier?: number | string | null;
  score?: number | string | null;
  composite_score?: number | string | null;
  target_weight?: number | string | null;
  percentile?: number | string | null;
  score_percentile?: number | string | null;
  reason?: string | null;
  reasons?: (string | null)[] | null;
  message?: string | null;
  as_of?: string | null;
  date?: string | null;
  [key: string]: unknown;
}

/** 手动运行模拟交易的响应（POST /api/paper/run） */
export interface PaperRunResult {
  ok?: boolean;
  success?: boolean;
  message?: string | null;
  trades?: PaperTrade[] | null;
  trade_count?: number | string | null;
  signals?: PaperSignal[] | null;
  as_of?: string | null;
  date?: string | null;
  summary?: PaperSummary | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

/* ==================== 组合区间收益（GET /api/portfolio/returns） ==================== */

/** 收益窗口标识：今日 / 近一周 / 近一月(30天) / 近三月 */
export type ReturnWindowKey = "1d" | "1w" | "1m" | "3m";

/** 单只基金在一个窗口内的收益（按当前份额估算） */
export interface FundReturnItem {
  instrument_id?: number | string | null;
  instrument_code?: string | null;
  instrument_name?: string | null;
  is_qdii?: boolean | null;
  shares?: number | string | null;
  return_amount?: number | string | null;
  /** 收益率（小数） */
  return_rate?: number | string | null;
  /** 实际起点净值日期 */
  start_date?: string | null;
  /** 实际终点净值日期（该基金最新净值日期，QDII 通常更旧） */
  end_date?: string | null;
  start_nav?: number | string | null;
  end_nav?: number | string | null;
  rate_basis?: string | null;
  /** available / stale / approximate */
  status?: string | null;
  stale_reason?: string | null;
  has_flows?: boolean | null;
  weight?: number | string | null;
  [key: string]: unknown;
}

/** 一个窗口的组合收益（按各基金期末金额加权） */
export interface PortfolioReturnWindow {
  window?: string | null;
  target_start_date?: string | null;
  return_amount?: number | string | null;
  return_rate?: number | string | null;
  /** 参与加权的金额占比 0~1 */
  coverage?: number | string | null;
  available_count?: number | string | null;
  approximate_count?: number | string | null;
  stale_count?: number | string | null;
  /** 参与加权基金的最晚净值日期 */
  as_of_end_date?: string | null;
  items?: FundReturnItem[] | null;
  [key: string]: unknown;
}

/** 组合区间收益响应：windows 为 窗口标识 -> 窗口收益 的字典 */
export interface PortfolioReturnsResponse {
  windows?: Record<string, PortfolioReturnWindow | null> | null;
  [key: string]: unknown;
}

/* ==================== 基金详情（GET /api/funds/{code}/detail、POST /api/funds/{code}/refresh） ==================== */

/** 基金介绍/基本概况（详情响应的 profile 子对象） */
export interface FundProfile {
  code?: string | null;
  short_name?: string | null;
  full_name?: string | null;
  inception_date?: string | null;
  latest_scale?: string | null;
  company?: string | null;
  manager?: string | null;
  custodian?: string | null;
  fund_type?: string | null;
  rating_agency?: string | null;
  rating?: string | null;
  investment_objective?: string | null;
  investment_strategy?: string | null;
  benchmark?: string | null;
  management_fee?: string | null;
  custody_fee?: string | null;
  source?: string | null;
  fetched_at?: string | null;
  [key: string]: unknown;
}

/** 基金季度重仓股（详情响应的 holdings 列表项） */
export interface FundDetailHolding {
  rank?: number | string | null;
  stock_code?: string | null;
  code?: string | null;
  stock_name?: string | null;
  name?: string | null;
  weight?: number | string | null;
  shares?: number | string | null;
  market_value?: number | string | null;
  report_date?: string | null;
  [key: string]: unknown;
}

/** 基金行业配置（详情响应的 industries 列表项） */
export interface FundDetailIndustry {
  industry?: string | null;
  name?: string | null;
  weight?: number | string | null;
  market_value?: number | string | null;
  report_date?: string | null;
  [key: string]: unknown;
}

export interface FundAdvice {
  action?: "add" | "hold" | "watch" | "reduce" | "reduce_more" | null;
  label?: string | null;
  score?: number | string | null;
  confidence?: "high" | "medium" | "low" | null;
  horizon?: string | null;
  summary?: string | null;
  reasons?: string[] | null;
  risks?: string[] | null;
  invalidation?: string | null;
}

export interface FundNewsEvent {
  id?: number | string | null;
  title?: string | null;
  summary?: string | null;
  direction?: "positive" | "neutral" | "negative" | string | null;
  impact_level?: "low" | "medium" | "high" | string | null;
  relation_type?: string | null;
  reason?: string | null;
  score?: number | string | null;
  published_at?: string | null;
  source_count?: number | string | null;
  analysis_method?: string | null;
}

export interface FundAnalysisSummary {
  quant_score?: number | string | null;
  news_score?: number | string | null;
  combined_score?: number | string | null;
  quant_view?: string | null;
  news_view?: string | null;
  portfolio_view?: string | null;
  conclusion?: string | null;
  conflict_note?: string | null;
  as_of?: string | null;
  news_event_count?: number | string | null;
  news_analysis_method?: string | null;
  key_events?: FundNewsEvent[] | null;
}

/** 基金详情聚合响应（字段宽松可选，兼容后端结构差异） */
export interface FundDetailResponse {
  code?: string | null;
  fund_code?: string | null;
  name?: string | null;
  fund_name?: string | null;
  fund_type?: string | null;
  type?: string | null;
  market?: string | null;
  family?: string | null;
  share_class?: string | null;
  active?: boolean | null;
  profile?: FundProfile | null;
  metrics?: Record<string, unknown> | null;
  metrics_as_of?: string | null;
  metrics_basis?: string | null;
  advice?: FundAdvice | null;
  analysis?: FundAnalysisSummary | null;
  holdings?: FundDetailHolding[] | null;
  industries?: FundDetailIndustry[] | null;
  report_date?: string | null;
  warnings?: (string | null)[] | null;
  refreshed?: boolean | null;
  [key: string]: unknown;
}

/* ==================== 全市场基金发现（/api/discovery/* 与 /api/discovery-quant/*） ==================== */

/** 目录中的单个基金条目（GET /api/discovery/catalog/list 列表项） */
export interface DiscoveryCatalogFund {
  code?: string | null;
  fund_code?: string | null;
  name?: string | null;
  fund_name?: string | null;
  fund_type?: string | null;
  type?: string | null;
  category?: string | null;
  market?: string | null;
  family?: string | null;
  company?: string | null;
  manager?: string | null;
  inception_date?: string | null;
  establish_date?: string | null;
  latest_nav_date?: string | null;
  nav_count?: number | string | null;
  history_days?: number | string | null;
  first_nav_date?: string | null;
  [key: string]: unknown;
}

/** 目录列表响应（GET /api/discovery/catalog/list） */
export interface DiscoveryCatalogListResponse {
  total?: number | string | null;
  offset?: number | string | null;
  limit?: number | string | null;
  items?: DiscoveryCatalogFund[] | null;
  funds?: DiscoveryCatalogFund[] | null;
  results?: DiscoveryCatalogFund[] | null;
  [key: string]: unknown;
}

/** 目录统计：单一分类维度计数 */
export interface DiscoveryCatalogBreakdown {
  label?: string | null;
  key?: string | null;
  name?: string | null;
  type?: string | null;
  market?: string | null;
  count?: number | string | null;
  fund_count?: number | string | null;
  [key: string]: unknown;
}

/** 目录统计响应（GET /api/discovery/catalog/stats） */
export interface DiscoveryCatalogStats {
  total?: number | string | null;
  total_count?: number | string | null;
  fund_count?: number | string | null;
  by_type?: DiscoveryCatalogBreakdown[] | Record<string, number | string> | null;
  by_market?: DiscoveryCatalogBreakdown[] | Record<string, number | string> | null;
  by_category?: DiscoveryCatalogBreakdown[] | Record<string, number | string> | null;
  types?: DiscoveryCatalogBreakdown[] | Record<string, number | string> | null;
  markets?: DiscoveryCatalogBreakdown[] | Record<string, number | string> | null;
  updated_at?: string | null;
  as_of?: string | null;
  [key: string]: unknown;
}

/** 目录同步结果（POST /api/discovery/catalog/sync） */
export interface DiscoveryCatalogSyncResult {
  ok?: boolean;
  success?: boolean;
  status?: string | null;
  message?: string | null;
  total?: number | string | null;
  updated?: number | string | null;
  inserted?: number | string | null;
  failed?: number | string | null;
  [key: string]: unknown;
}

/** 候选池摘要（GET /api/discovery/pools 列表项） */
export interface DiscoveryPool {
  id?: number | string | null;
  pool_id?: number | string | null;
  name?: string | null;
  description?: string | null;
  member_count?: number | string | null;
  size?: number | string | null;
  fund_count?: number | string | null;
  config?: Record<string, unknown> | null;
  created_at?: string | null;
  updated_at?: string | null;
  as_of?: string | null;
  [key: string]: unknown;
}

/** 候选池成员（池详情 members 列表项） */
export interface DiscoveryPoolMember {
  code?: string | null;
  fund_code?: string | null;
  name?: string | null;
  fund_name?: string | null;
  fund_type?: string | null;
  type?: string | null;
  market?: string | null;
  family?: string | null;
  weight?: number | string | null;
  rank?: number | string | null;
  score?: number | string | null;
  first_nav_date?: string | null;
  latest_nav_date?: string | null;
  nav_count?: number | string | null;
  nav_samples?: number | string | null;
  nav_ready?: boolean | null;
  history_days?: number | string | null;
  coverage?: number | string | null;
  expected_days?: number | string | null;
  [key: string]: unknown;
}

/** 成员历史覆盖进度（池详情 coverage 字段） */
export interface DiscoveryPoolCoverage {
  member_count?: number | string | null;
  covered_count?: number | string | null;
  full_count?: number | string | null;
  avg_coverage?: number | string | null;
  coverage?: number | string | null;
  progress?: number | string | null;
  earliest_nav_date?: string | null;
  latest_nav_date?: string | null;
  total_nav_count?: number | string | null;
  [key: string]: unknown;
}

/** 候选池详情（GET /api/discovery/pools/{id}） */
export interface DiscoveryPoolDetail extends DiscoveryPool {
  members?: DiscoveryPoolMember[] | null;
  funds?: DiscoveryPoolMember[] | null;
  items?: DiscoveryPoolMember[] | null;
  coverage?: DiscoveryPoolCoverage | null;
  history_coverage?: DiscoveryPoolCoverage | null;
  summary?: {
    nav_ready_count?: number | string | null;
    tier_counts?: Record<string, number | string> | null;
    market_counts?: Record<string, number | string> | null;
  } | null;
  [key: string]: unknown;
}

/** 候选池构建配置（POST /api/discovery/pools/build 请求体） */
export interface DiscoveryPoolBuildRequest {
  name?: string | null;
  fund_types?: string[] | null;
  markets?: string[] | null;
  min_history_days?: number | null;
  max_size?: number;
  exclude_codes?: string[] | null;
  [key: string]: unknown;
}

/** 候选池构建结果（POST /api/discovery/pools/build） */
export interface DiscoveryPoolBuildResult {
  ok?: boolean;
  success?: boolean;
  pool_id?: number | string | null;
  id?: number | string | null;
  name?: string | null;
  member_count?: number | string | null;
  candidate_count?: number | string | null;
  excluded_count?: number | string | null;
  message?: string | null;
  pool?: DiscoveryPoolDetail | null;
  detail?: DiscoveryPoolDetail | null;
  members?: DiscoveryPoolMember[] | null;
  coverage?: DiscoveryPoolCoverage | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

/**
 * 单只基金的横截面指标快照（GET /api/discovery-quant/pools/{id}/factors 列表项）。
 * 与后端 FactorBoardItem 对齐：收益为区间收益（小数），波动/夏普/索提诺为年化口径，
 * CVaR95 为最差 5% 日收益均值，quantile 为同类（同市场层）内动量分位数 ∈[0,1]。
 */
export interface DiscoveryFactorItem {
  rank?: number | string | null;
  code?: string | null;
  fund_code?: string | null;
  name?: string | null;
  fund_name?: string | null;
  fund_type?: string | null;
  type?: string | null;
  market?: string | null;
  market_label?: string | null;
  family?: string | null;
  /** 参与计算的净值样本数 */
  sample_count?: number | string | null;
  /** 近 21 个交易日收益（小数） */
  return_1m?: number | string | null;
  /** 近 63 个交易日收益（小数） */
  return_3m?: number | string | null;
  /** 近 252 个交易日收益（小数） */
  return_1y?: number | string | null;
  /** 近 756 个交易日收益（小数） */
  return_3y?: number | string | null;
  /** 年化波动率（小数） */
  annual_volatility?: number | string | null;
  annualized_volatility?: number | string | null;
  volatility?: number | string | null;
  /** 窗口内最大回撤（负数小数） */
  max_drawdown?: number | string | null;
  /** 夏普比率（年化，无风险利率 2%） */
  sharpe?: number | string | null;
  sharpe_ratio?: number | string | null;
  /** 索提诺比率（年化，下行偏差口径） */
  sortino?: number | string | null;
  /** Calmar：年化收益 / |最大回撤| */
  calmar?: number | string | null;
  calmar_ratio?: number | string | null;
  /** CVaR95：最差 5% 日收益均值（小数） */
  cvar95?: number | string | null;
  /** 绝对动量 12-1（t-21 对 t-252 区间收益，小数） */
  momentum_12_1?: number | string | null;
  momentum?: number | string | null;
  /** 同类（同市场层）内动量分位数 ∈[0,1] */
  quantile?: number | string | null;
  [key: string]: unknown;
}

/** 因子榜响应（GET /api/discovery-quant/pools/{id}/factors），与后端 FactorBoardResponse 对齐 */
export interface DiscoveryFactorsResponse {
  pool_id?: number | string | null;
  /** 因子基准日（最新共同净值日） */
  as_of?: string | null;
  /** 有效候选总数（分页前） */
  total?: number | string | null;
  limit?: number | string | null;
  methodology?: string | null;
  items?: DiscoveryFactorItem[] | null;
  factors?: DiscoveryFactorItem[] | null;
  funds?: DiscoveryFactorItem[] | null;
  results?: DiscoveryFactorItem[] | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

/** 双动量排名条目（GET /api/discovery-quant/pools/{id}/dual-momentum 列表项） */
export interface DiscoveryDualMomentumItem {
  code?: string | null;
  fund_code?: string | null;
  name?: string | null;
  fund_name?: string | null;
  market?: string | null;
  family?: string | null;
  /** 绝对动量（12-1 区间收益，>0 通过） */
  absolute_momentum?: number | string | null;
  momentum_12_1?: number | string | null;
  momentum?: number | string | null;
  /** 相对动量：在候选池内的排名（1 为最强） */
  relative_rank?: number | string | null;
  rank?: number | string | null;
  /** 相对动量分位数 0-1 或 0-100 */
  relative_percentile?: number | string | null;
  percentile?: number | string | null;
  /** 是否通过绝对动量过滤 */
  pass?: boolean | null;
  eligible?: boolean | null;
  selected?: boolean | null;
  [key: string]: unknown;
}

/** 双动量响应（GET /api/discovery-quant/pools/{id}/dual-momentum） */
export interface DiscoveryDualMomentumResponse {
  pool_id?: number | string | null;
  as_of?: string | null;
  candidate_count?: number | string | null;
  eligible_count?: number | string | null;
  methodology?: string | null;
  items?: DiscoveryDualMomentumItem[] | null;
  rankings?: DiscoveryDualMomentumItem[] | null;
  results?: DiscoveryDualMomentumItem[] | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

/** 当期入选信号条目（GET /api/discovery-quant/pools/{id}/signals 列表项） */
export interface DiscoverySignalItem {
  code?: string | null;
  fund_code?: string | null;
  name?: string | null;
  fund_name?: string | null;
  market?: string | null;
  family?: string | null;
  weight?: number | string | null;
  momentum_12_1?: number | string | null;
  momentum?: number | string | null;
  rank_in_market?: number | string | null;
  rank?: number | string | null;
  reasons?: (string | null)[] | null;
  reason?: string | null;
  [key: string]: unknown;
}

/** 当期入选信号响应（GET /api/discovery-quant/pools/{id}/signals） */
export interface DiscoverySignalsResponse {
  pool_id?: number | string | null;
  as_of?: string | null;
  trade_date?: string | null;
  candidate_count?: number | string | null;
  eligible_count?: number | string | null;
  selected?: DiscoverySignalItem[] | null;
  signals?: DiscoverySignalItem[] | null;
  items?: DiscoverySignalItem[] | null;
  cash_weight?: number | string | null;
  frozen?: boolean | null;
  freeze_reason?: string | null;
  methodology?: string | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

/** 候选池 V2 回测请求体（POST /api/discovery-quant/pools/{id}/backtest） */
export interface DiscoveryBacktestRequest {
  start_date?: string | null;
  end_date?: string | null;
  top_n?: number;
  rebalance_interval_months?: number;
  initial_capital?: number;
  [key: string]: unknown;
}

/** 候选池统计验证请求体（POST /api/discovery-quant/pools/{id}/validation） */
export interface DiscoveryValidationRequest {
  as_of?: string | null;
  window?: {
    train_window?: number;
    test_window?: number;
    step?: number;
  };
  top_n?: number;
  trial_count?: number;
  include_costs?: boolean;
  [key: string]: unknown;
}

/* ==================== 同步任务状态（GET /api/sync/status） ==================== */

/** 单次同步任务运行记录 */
export interface SyncRunItem {
  id?: number | string | null;
  job_name?: string | null;
  /** running / success / failed */
  status?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | string | null;
  total?: number | string | null;
  updated?: number | string | null;
  failed?: number | string | null;
  data_date?: string | null;
  error?: string | null;
  [key: string]: unknown;
}

/** 同步状态汇总响应（不带 job_name 时） */
export interface SyncStatusResponse {
  /** 服务器当前时间（北京时间 ISO 8601） */
  server_time?: string | null;
  job_name?: string | null;
  runs?: SyncRunItem[] | null;
  /** 各任务下次计划运行时间（北京时间 ISO 8601） */
  next_runs?: Record<string, string | null> | null;
  alerts?: Array<{
    type?: string | null;
    severity?: string | null;
    message?: string | null;
    code?: string | null;
    correlation_id?: string | null;
  }> | null;
  [key: string]: unknown;
}

/* ==================== 股票研究（/api/stocks/*，后端可能尚未上线） ==================== */

/** 股票基础主数据（GET /api/stocks/master 列表项） */
export interface StockMasterItem {
  code?: string | null;
  symbol?: string | null;
  ticker?: string | null;
  name?: string | null;
  exchange?: string | null;
  market?: string | null;
  industry?: string | null;
  industry_sw?: string | null;
  sector?: string | null;
  list_date?: string | null;
  is_st?: boolean | null;
  [key: string]: unknown;
}

/** 股票主数据响应（GET /api/stocks/master，可能直接为数组） */
export interface StockMasterResponse {
  items?: StockMasterItem[] | null;
  stocks?: StockMasterItem[] | null;
  industries?: string[] | null;
  total?: number | string | null;
  as_of?: string | null;
  [key: string]: unknown;
}

/** 股票宇宙/范围（GET /api/stocks/universe 列表项） */
export interface StockUniverseItem {
  code?: string | null;
  stock_code?: string | null;
  name?: string | null;
  stock_name?: string | null;
  industry?: string | null;
  sector?: string | null;
  market?: string | null;
  weight?: number | string | null;
  [key: string]: unknown;
}

/** 股票宇宙响应（GET /api/stocks/universe，可能直接为数组） */
export interface StockUniverseResponse {
  name?: string | null;
  universe?: string | null;
  items?: StockUniverseItem[] | null;
  stocks?: StockUniverseItem[] | null;
  members?: StockUniverseItem[] | null;
  codes?: string[] | null;
  industries?: string[] | null;
  total?: number | string | null;
  as_of?: string | null;
  [key: string]: unknown;
}

/** 单类股票数据的可用性（GET /api/stocks/data/status 子项） */
export interface StockDataSourceStatus {
  /** quotes / financials / factors / signals / universe / master 等 */
  available?: boolean | null;
  available_at?: string | null;
  last_updated?: string | null;
  updated_at?: string | null;
  rows?: number | string | null;
  count?: number | string | null;
  message?: string | null;
  [key: string]: unknown;
}

/** 股票数据可用性总览（GET /api/stocks/data/status） */
export interface StockDataStatus {
  as_of?: string | null;
  server_time?: string | null;
  /** 顶层标记各数据集是否可用（后端可能给出该结构） */
  quotes?: boolean | StockDataSourceStatus | null;
  financials?: boolean | StockDataSourceStatus | null;
  factors?: boolean | StockDataSourceStatus | null;
  signals?: boolean | StockDataSourceStatus | null;
  universe?: boolean | StockDataSourceStatus | null;
  master?: boolean | StockDataSourceStatus | null;
  /** 或以数组/字典给出各数据源明细 */
  sources?: (StockDataSourceStatus & { name?: string | null })[] | Record<string, StockDataSourceStatus> | null;
  datasets?: Record<string, StockDataSourceStatus | boolean | null> | null;
  [key: string]: unknown;
}

/** 行情快照（GET /api/stocks/{code}/quote 等，可能嵌套在 master 响应中） */
export interface StockQuote {
  code?: string | null;
  name?: string | null;
  price?: number | string | null;
  close?: number | string | null;
  prev_close?: number | string | null;
  open?: number | string | null;
  high?: number | string | null;
  low?: number | string | null;
  volume?: number | string | null;
  turnover?: number | string | null;
  amount?: number | string | null;
  change?: number | string | null;
  change_pct?: number | string | null;
  pct_change?: number | string | null;
  date?: string | null;
  trade_date?: string | null;
  available_at?: string | null;
  [key: string]: unknown;
}

/** 单个交易日 K 线/收盘点 */
export interface StockPricePoint {
  date?: string | null;
  trade_date?: string | null;
  close?: number | string | null;
  open?: number | string | null;
  high?: number | string | null;
  low?: number | string | null;
  volume?: number | string | null;
  pct_change?: number | string | null;
  change_pct?: number | string | null;
  [key: string]: unknown;
}

/** 财务与估值指标（GET /api/stocks/{code}/financials，可能嵌套在 master 响应中） */
export interface StockFinancials {
  code?: string | null;
  pe?: number | string | null;
  pe_ttm?: number | string | null;
  pb?: number | string | null;
  ps?: number | string | null;
  pcf?: number | string | null;
  market_cap?: number | string | null;
  total_market_cap?: number | string | null;
  float_market_cap?: number | string | null;
  revenue?: number | string | null;
  net_profit?: number | string | null;
  revenue_yoy?: number | string | null;
  profit_yoy?: number | string | null;
  net_profit_yoy?: number | string | null;
  roe?: number | string | null;
  roa?: number | string | null;
  gross_margin?: number | string | null;
  net_margin?: number | string | null;
  debt_ratio?: number | string | null;
  eps?: number | string | null;
  bps?: number | string | null;
  dividend_yield?: number | string | null;
  report_date?: string | null;
  period?: string | null;
  available_at?: string | null;
  [key: string]: unknown;
}

/** 单只股票的因子值（GET /api/stocks/research/factors 列表项） */
export interface StockFactorItem {
  code?: string | null;
  symbol?: string | null;
  name?: string | null;
  industry?: string | null;
  sector?: string | null;
  market?: string | null;
  composite_score?: number | string | null;
  composite?: number | string | null;
  score?: number | string | null;
  rank?: number | string | null;
  percentile?: number | string | null;
  /** 因子明细：字典或数组两种形态均兼容 */
  factors?: Record<string, number | string | null> | { name?: string | null; label?: string | null; value?: number | string | null; score?: number | string | null }[] | null;
  momentum?: number | string | null;
  value?: number | string | null;
  quality?: number | string | null;
  growth?: number | string | null;
  volatility?: number | string | null;
  size?: number | string | null;
  pe?: number | string | null;
  pb?: number | string | null;
  roe?: number | string | null;
  return_20d?: number | string | null;
  return_60d?: number | string | null;
  as_of?: string | null;
  [key: string]: unknown;
}

/** 因子响应（GET /api/stocks/research/factors，可能直接为数组） */
export interface StockFactorsResponse {
  items?: StockFactorItem[] | null;
  factors?: StockFactorItem[] | null;
  results?: StockFactorItem[] | null;
  rows?: StockFactorItem[] | null;
  as_of?: string | null;
  available_at?: string | null;
  total?: number | string | null;
  warnings?: string[] | null;
  factor_diagnostics?: Record<string, unknown> | null;
  [key: string]: unknown;
}

/** 股票研究信号（GET /api/stocks/research/signals 列表项） */
export interface StockSignalItem {
  code?: string | null;
  symbol?: string | null;
  name?: string | null;
  signal?: string | null;
  direction?: string | null;
  stance?: string | null;
  strength?: number | string | null;
  score?: number | string | null;
  tier?: number | string | null;
  reason?: string | null;
  reasons?: (string | null)[] | null;
  message?: string | null;
  industry?: string | null;
  as_of?: string | null;
  date?: string | null;
  [key: string]: unknown;
}

/** 股票信号响应（GET /api/stocks/research/signals，可能直接为数组） */
export interface StockSignalsResponse {
  items?: StockSignalItem[] | null;
  signals?: StockSignalItem[] | null;
  results?: StockSignalItem[] | null;
  as_of?: string | null;
  available_at?: string | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

/** 白话技术趋势摘要（GET /api/stocks/{code}/technical） */
export interface StockTechnicalResponse {
  code: string;
  as_of?: string | null;
  sufficient: boolean;
  sample_size: number;
  trend: "strong_bullish" | "bullish" | "neutral" | "bearish" | "strong_bearish" | "insufficient";
  score: number;
  summary: string;
  indicators: {
    close?: number | null;
    ma5?: number | null;
    ma10?: number | null;
    ma20?: number | null;
    ma60?: number | null;
    macd_dif?: number | null;
    macd_dea?: number | null;
    macd_histogram?: number | null;
    rsi6?: number | null;
    rsi12?: number | null;
    rsi24?: number | null;
    kdj_k?: number | null;
    kdj_d?: number | null;
    kdj_j?: number | null;
    boll_upper?: number | null;
    boll_middle?: number | null;
    boll_lower?: number | null;
    atr14?: number | null;
    atr_pct?: number | null;
    support20?: number | null;
    resistance20?: number | null;
    volume_ratio?: number | null;
  };
  signals: string[];
  risks: string[];
  methodology: string;
}

/** 股票研究回测请求体（POST /api/stocks/research/backtest） */
export interface StockBacktestRequest {
  codes?: string[];
  universe?: string;
  top_n?: number;
  start_date?: string;
  end_date?: string;
  strategy?: string;
  rebalance?: string;
  [key: string]: unknown;
}

/** 股票研究回测响应（POST /api/stocks/research/backtest） */
export interface StockBacktestResult {
  summary?: BacktestSummary | null;
  strategy?: BacktestSummary | null;
  benchmark?: BacktestSummary | null;
  curve?: BacktestPoint[] | null;
  points?: BacktestPoint[] | null;
  equity_curve?: BacktestPoint[] | null;
  holdings?: Record<string, number | string> | string[] | null;
  start_date?: string | null;
  end_date?: string | null;
  methodology?: string | null;
  warnings?: string[] | null;
  attribution?: Record<string, number> | null;
  [key: string]: unknown;
}

/** A股规则策略两个月前向模拟。 */
export interface StockPaperReadiness {
  ready: boolean;
  status: string;
  universe_count: number;
  daily_ready_count: number;
  industry_ready_count: number;
  financial_ready_count: number;
  valuation_ready_count: number;
  latest_data_date?: string | null;
  source_health: Record<string, {
    status?: string | null;
    finished_at?: string | null;
    updated?: number | null;
    failed?: number | null;
    detail?: string | null;
  }>;
  blockers: string[];
  warnings: string[];
}

export interface StockPaperMetrics {
  total_return?: number | null;
  benchmark_return?: number | null;
  excess_return?: number | null;
  annual_return?: number | null;
  annual_volatility?: number | null;
  max_drawdown?: number | null;
  sharpe?: number | null;
  win_rate?: number | null;
  information_ratio?: number | null;
  trading_days: number;
  rebalance_count: number;
  trade_count: number;
  total_fees: number;
}

export interface StockPaperStrategy {
  version_id: number;
  name: string;
  status: string;
  trial_start: string;
  trial_end: string;
  calendar_days_elapsed: number;
  calendar_days_remaining: number;
  observation_progress: number;
  candidate_count: number;
  validation_scope: string;
  investment_approval_eligible: boolean;
  mandate_version: string;
  mandate_sha256: string;
  result_interpretation: string;
  approval_blocker?: string | null;
  params: Record<string, unknown>;
}

export interface StockPaperPosition {
  code: string;
  name: string;
  industry: string;
  shares: number;
  cost: number;
  price?: number | null;
  market_value?: number | null;
  weight?: number | null;
  pnl?: number | null;
}

export interface StockPaperSignal {
  id: number;
  signal_date: string;
  execute_on?: string | null;
  status: string;
  universe_count: number;
  selected_count: number;
  invested_weight: number;
  items: Array<{
    code: string;
    name: string;
    industry: string;
    rank: number;
    composite: number;
    weight: number;
    quality?: number | null;
    value?: number | null;
    momentum?: number | null;
    trend?: number | null;
    lowvol?: number | null;
  }>;
  warnings: string[];
}

export interface StockPaperHistoryPoint {
  date: string;
  nav: number;
  benchmark_nav: number;
  total_value: number;
  daily_return?: number | null;
  benchmark_daily_return?: number | null;
  rebalanced: boolean;
}

export interface StockPaperSummary {
  started: boolean;
  account_id?: number | null;
  account_name?: string | null;
  as_of?: string | null;
  initial_capital: number;
  cash: number;
  market_value: number;
  total_value: number;
  nav: number;
  benchmark_nav: number;
  strategy?: StockPaperStrategy | null;
  readiness: StockPaperReadiness;
  metrics: StockPaperMetrics;
  positions: StockPaperPosition[];
  latest_signal?: StockPaperSignal | null;
  history: StockPaperHistoryPoint[];
  warnings: string[];
}

export interface StockPaperTrade {
  id: number;
  trade_date: string;
  signal_date: string;
  code: string;
  name: string;
  side: string;
  shares: number;
  price: number;
  amount: number;
  fee: number;
  target_weight: number;
  reason: string;
}

export interface StockPaperRunResult {
  account_id: number;
  run_date: string;
  skipped: boolean;
  status: string;
  signal_generated: boolean;
  rebalanced: boolean;
  trade_count: number;
  total_value: number;
  nav: number;
  benchmark_nav: number;
  warnings: string[];
}

/** 单只股票聚合详情（GET /api/stocks/{code}/master 等详情端点的宽松结构） */
export interface StockDetail {
  code?: string | null;
  symbol?: string | null;
  name?: string | null;
  industry?: string | null;
  sector?: string | null;
  market?: string | null;
  exchange?: string | null;
  quote?: StockQuote | null;
  price?: number | string | null;
  change_pct?: number | string | null;
  history?: StockPricePoint[] | null;
  prices?: StockPricePoint[] | null;
  candles?: StockPricePoint[] | null;
  financials?: StockFinancials | null;
  factors?: StockFactorItem | Record<string, number | string | null> | null;
  available_at?: string | null;
  as_of?: string | null;
  [key: string]: unknown;
}

/** 研究组合中的单个持仓（基金发现或股票组合条目） */
export interface ResearchPortfolioHolding {
  code?: string | null;
  fund_code?: string | null;
  name?: string | null;
  fund_name?: string | null;
  weight?: number | string | null;
  score?: number | string | null;
  reason?: string | null;
  reasons?: (string | null)[] | null;
  industry?: string | null;
  market?: string | null;
  [key: string]: unknown;
}

/** 单个研究组合 */
export interface ResearchPortfolio {
  id?: number | string;
  name?: string | null;
  title?: string | null;
  kind?: string | null;
  type?: string | null;
  description?: string | null;
  methodology?: string | null;
  holdings?: ResearchPortfolioHolding[] | null;
  items?: ResearchPortfolioHolding[] | null;
  constituents?: ResearchPortfolioHolding[] | null;
  as_of?: string | null;
  updated_at?: string | null;
  [key: string]: unknown;
}

/** 研究组合列表响应（GET /api/research/portfolios 等，可能直接为数组） */
export interface ResearchPortfoliosResponse {
  items?: ResearchPortfolio[] | null;
  portfolios?: ResearchPortfolio[] | null;
  results?: ResearchPortfolio[] | null;
  as_of?: string | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}
