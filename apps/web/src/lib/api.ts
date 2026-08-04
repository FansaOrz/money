import type {
  BacktestRequest,
  BacktestResult,
  BacktestV2Request,
  BacktestV2Result,
  CommitResult,
  DiscoveryBacktestRequest,
  DiscoveryCatalogListResponse,
  DiscoveryCatalogStats,
  DiscoveryCatalogSyncResult,
  DiscoveryDualMomentumResponse,
  DiscoveryFactorsResponse,
  DiscoveryPool,
  DiscoveryPoolBuildRequest,
  DiscoveryPoolBuildResult,
  DiscoveryPoolDetail,
  DiscoverySignalsResponse,
  DiscoveryValidationRequest,
  FundDetailResponse,
  FundNavHistoryResponse,
  ImportPreview,
  NewsItem,
  NewsScope,
  PaperHistoryPoint,
  PaperPosition,
  PaperRunResult,
  PaperSignal,
  PaperSummary,
  PaperTrade,
  PortfolioReturnsResponse,
  PortfolioSnapshot,
  PortfolioSummary,
  Position,
  QuantFundMetrics,
  QuantPortfolio,
  QuantSignal,
  ScreenerMeta,
  ScreenerSignal,
  SignalsV2Response,
  SnapshotResponse,
  StockBacktestRequest,
  StockBacktestResult,
  StockDataStatus,
  StockDetail,
  StockFactorsResponse,
  StockMasterResponse,
  StockPaperRunResult,
  StockPaperSummary,
  StockPaperTrade,
  StockPricePoint,
  StockQuote,
  StockFinancials,
  StockSignalsResponse,
  StockTechnicalResponse,
  StockUniverseResponse,
  ResearchPortfoliosResponse,
  SyncStatusResponse,
  Transaction,
  ValidationRequest,
  ValidationResponse,
  WalkForwardRequest,
  WalkForwardResult,
} from "./types";

const BASE_URL = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/+$/, "");
// v4：基金详情加入后台新闻分析与综合建议，淘汰旧的纯历史趋势缓存。
const CACHE_PREFIX = "money:api:v4:";
const memoryCache = new Map<string, { expiresAt: number; value: unknown }>();
const inFlight = new Map<string, Promise<unknown>>();

export class ApiError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function resolveUrl(path: string): string {
  // 浏览器端默认同源请求，由 Next.js 代理到后端，避免浏览器直连服务器 127.0.0.1。
  if (!BASE_URL && typeof window === "undefined") {
    return `http://127.0.0.1:8001${path}`;
  }
  return `${BASE_URL}${path}`;
}

async function parseErrorBody(res: Response): Promise<string> {
  try {
    const data: unknown = await res.json();
    if (data && typeof data === "object") {
      const obj = data as Record<string, unknown>;
      if (typeof obj.detail === "string") return obj.detail;
      if (typeof obj.message === "string") return obj.message;
      return JSON.stringify(data);
    }
  } catch {
    /* ignore */
  }
  return `HTTP ${res.status}`;
}

function cacheTtl(path: string): number {
  if (path.startsWith("/api/sync/status")) return 15_000;
  // 持仓净值晚间会分批更新，短缓存确保“今日收益”及时反映后台同步结果。
  if (path.startsWith("/api/portfolio/") || path.startsWith("/api/positions")) return 15_000;
  if (path.startsWith("/api/news")) return 60_000;
  if (
    path.startsWith("/api/quant/") ||
    path.startsWith("/api/discovery/") ||
    path.startsWith("/api/funds/")
  ) {
    return 6 * 60 * 60_000;
  }
  return 30 * 60_000;
}

function readCache<T>(key: string): T | undefined {
  const now = Date.now();
  const memory = memoryCache.get(key);
  if (memory) {
    if (memory.expiresAt > now) return memory.value as T;
    memoryCache.delete(key);
  }
  if (typeof window === "undefined") return undefined;
  try {
    const raw = window.sessionStorage.getItem(`${CACHE_PREFIX}${key}`);
    if (!raw) return undefined;
    const cached = JSON.parse(raw) as { expiresAt: number; value: T };
    if (cached.expiresAt <= now) {
      window.sessionStorage.removeItem(`${CACHE_PREFIX}${key}`);
      return undefined;
    }
    memoryCache.set(key, cached);
    return cached.value;
  } catch {
    return undefined;
  }
}

/** 页面初始化时同步读取已有数据，避免先闪现整页 Loading。 */
export function peekApiCache<T>(path: string): T | undefined {
  return readCache<T>(path);
}

function writeCache<T>(key: string, value: T, ttl: number): void {
  const entry = { expiresAt: Date.now() + ttl, value };
  memoryCache.set(key, entry);
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(`${CACHE_PREFIX}${key}`, JSON.stringify(entry));
  } catch {
    // 浏览器存储空间不足时仍保留内存缓存，不影响正常请求。
  }
}

export function clearApiCache(): void {
  memoryCache.clear();
  if (typeof window === "undefined") return;
  for (let index = window.sessionStorage.length - 1; index >= 0; index -= 1) {
    const key = window.sessionStorage.key(index);
    if (key?.startsWith(CACHE_PREFIX)) window.sessionStorage.removeItem(key);
  }
}

/** 只清除某一类 GET 缓存，供用户主动点击“刷新”时绕过本地快照。 */
export function invalidateApiCache(pathPrefix: string): void {
  for (const key of memoryCache.keys()) {
    if (key.startsWith(pathPrefix)) memoryCache.delete(key);
  }
  if (typeof window === "undefined") return;
  for (let index = window.sessionStorage.length - 1; index >= 0; index -= 1) {
    const storageKey = window.sessionStorage.key(index);
    if (!storageKey?.startsWith(CACHE_PREFIX)) continue;
    const apiPath = storageKey.slice(CACHE_PREFIX.length);
    if (apiPath.startsWith(pathPrefix)) window.sessionStorage.removeItem(storageKey);
  }
}

async function getJson<T>(path: string): Promise<T> {
  const cached = readCache<T>(path);
  if (cached !== undefined) return cached;

  const pending = inFlight.get(path);
  if (pending) return pending as Promise<T>;

  const request = (async () => {
    const res = await fetch(resolveUrl(path), {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!res.ok) {
      throw new ApiError(await parseErrorBody(res), res.status);
    }
    const value = (await res.json()) as T;
    writeCache(path, value, cacheTtl(path));
    return value;
  })();
  inFlight.set(path, request);
  try {
    return await request;
  } finally {
    inFlight.delete(path);
  }
}

async function postJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(resolveUrl(path), {
    method: "POST",
    headers: { Accept: "application/json" },
    ...init,
  });
  if (!res.ok) {
    throw new ApiError(await parseErrorBody(res), res.status);
  }
  const value = (await res.json()) as T;
  if (
    path.startsWith("/api/imports/") ||
    path.includes("/sync/") ||
    path.endsWith("/paper/run") ||
    path.includes("/discovery/pools") ||
    (path.startsWith("/api/funds/") && path.endsWith("/refresh"))
  ) {
    clearApiCache();
  }
  return value;
}

export const api = {
  portfolioSummary: () => getJson<PortfolioSummary>("/api/portfolio/summary"),
  portfolioSnapshots: () => getJson<PortfolioSnapshot[]>("/api/portfolio/snapshots"),
  /* 组合区间收益：不传 window 时后端一次返回全部窗口（1d/1w/1m/3m） */
  portfolioReturns: (window?: string) =>
    getJson<PortfolioReturnsResponse>(
      `/api/portfolio/returns${window ? `?window=${encodeURIComponent(window)}` : ""}`
    ),
  /* 同步任务运行状态：不带参数返回每个任务最近一次运行 + 下次计划时间 */
  syncStatus: () => getJson<SyncStatusResponse>("/api/sync/status"),
  positions: () => getJson<Position[]>("/api/positions"),
  fundNavHistory: (fundCode: string) =>
    getJson<FundNavHistoryResponse>(`/api/positions/${encodeURIComponent(fundCode)}/nav-history`),
  transactions: () => getJson<Transaction[]>("/api/transactions"),
  importPreview: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return postJson<ImportPreview>("/api/imports/preview", { body: form });
  },
  importCommit: (importId: string | number) =>
    postJson<CommitResult>(`/api/imports/${encodeURIComponent(String(importId))}/commit`),
  /* 量化分析 */
  quantPortfolio: () => getJson<QuantPortfolio>("/api/quant/portfolio"),
  quantFunds: () => getJson<QuantFundMetrics[]>("/api/quant/funds"),
  quantBacktest: (body: BacktestRequest) =>
    postJson<BacktestResult>("/api/quant/backtest", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  /* 研究信号 */
  quantSignals: async () => {
    const raw = await getJson<QuantSignal[] | { items?: QuantSignal[]; signals?: QuantSignal[] }>("/api/quant/signals?limit=500");
    return Array.isArray(raw)
      ? raw
      : Array.isArray(raw.items)
        ? raw.items
        : Array.isArray(raw.signals)
          ? raw.signals
          : [];
  },
  /* 规则模型：综合信号 / 五档模型（返回全部完成分析的候选与响应元信息） */
  screenerSignals: async (): Promise<{ items: ScreenerSignal[]; meta: ScreenerMeta }> => {
    const raw = await getJson<
      | ScreenerSignal[]
      | {
          items?: ScreenerSignal[];
          signals?: ScreenerSignal[];
          results?: ScreenerSignal[];
          as_of?: string | null;
          selected_count?: number | null;
          allocation_count?: number | null;
          candidate_count?: number | null;
          excluded_count?: number | null;
          observe_count?: number | null;
          warnings?: string[] | null;
        }
    >("/api/quant/screener/signals");
    if (Array.isArray(raw)) {
      return {
        items: raw,
        meta: {
          asOf: null,
          selectedCount: null,
          allocationCount: null,
          candidateCount: null,
          excludedCount: null,
          observeCount: null,
          warnings: [],
        },
      };
    }
    const items = Array.isArray(raw.items)
      ? raw.items
      : Array.isArray(raw.signals)
        ? raw.signals
        : Array.isArray(raw.results)
          ? raw.results
          : [];
    const num = (v: unknown): number | null =>
      typeof v === "number" && Number.isFinite(v) ? v : null;
    return {
      items,
      meta: {
        asOf: typeof raw.as_of === "string" ? raw.as_of : null,
        selectedCount: num(raw.selected_count),
        allocationCount: num(raw.allocation_count),
        candidateCount: num(raw.candidate_count),
        excludedCount: num(raw.excluded_count),
        observeCount: num(raw.observe_count),
        warnings: Array.isArray(raw.warnings)
          ? raw.warnings.filter((w): w is string => typeof w === "string" && w.length > 0)
          : [],
      },
    };
  },
  /* Walk-Forward 滚动回测 */
  quantWalkForward: (body: WalkForwardRequest) => {
    const trainWindow = body.train_window ?? body.train ?? body.train_size ?? 120;
    const testWindow = body.test_window ?? body.test ?? body.test_size ?? 20;
    return postJson<WalkForwardResult>("/api/quant/walkforward", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...body,
        window: { train_window: trainWindow, test_window: testWindow, step: testWindow },
      }),
    });
  },
  /* 稳健组合策略 V2：月频动量 + 层内 HRP + 波动率目标 */
  quantV2Backtest: (body: BacktestV2Request) =>
    postJson<BacktestV2Result>("/api/quant/v2/backtest", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  quantV2Signals: (params?: { codes?: string[]; topN?: number }) => {
    const search = new URLSearchParams();
    if (params?.codes && params.codes.length > 0) {
      search.set("codes", params.codes.join(","));
    }
    if (params?.topN !== undefined) {
      search.set("top_n", String(params.topN));
    }
    const qs = search.toString();
    return getJson<SignalsV2Response>(`/api/quant/v2/signals${qs ? `?${qs}` : ""}`);
  },
  /* 统计验证：as_of 快照下的样本外验证与稳健性检验 */
  quantValidation: (body: ValidationRequest) =>
    postJson<ValidationResponse>("/api/quant/validation", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  quantSnapshot: (params?: { codes?: string[]; asOf?: string }) => {
    const search = new URLSearchParams();
    if (params?.codes && params.codes.length > 0) {
      search.set("codes", params.codes.join(","));
    }
    if (params?.asOf) {
      search.set("as_of", params.asOf);
    }
    const qs = search.toString();
    return getJson<SnapshotResponse>(`/api/quant/snapshot${qs ? `?${qs}` : ""}`);
  },
  /* 每日资讯 */
  news: async (scope: NewsScope) => {
    const raw = await getJson<NewsItem[] | { items?: NewsItem[] }>(`/api/news?scope=${scope}`);
    return Array.isArray(raw) ? raw : Array.isArray(raw.items) ? raw.items : [];
  },
  /* 模拟交易（虚拟盘，非真实交易） */
  paperSummary: () => getJson<PaperSummary>("/api/paper/summary"),
  paperHistory: async () => {
    const raw = await getJson<
      PaperHistoryPoint[] | { items?: PaperHistoryPoint[]; points?: PaperHistoryPoint[]; history?: PaperHistoryPoint[] }
    >("/api/paper/history");
    if (Array.isArray(raw)) return raw;
    if (Array.isArray(raw.items)) return raw.items;
    if (Array.isArray(raw.points)) return raw.points;
    if (Array.isArray(raw.history)) return raw.history;
    return [];
  },
  paperPositions: async () => {
    const raw = await getJson<
      PaperPosition[] | { items?: PaperPosition[]; positions?: PaperPosition[]; holdings?: PaperPosition[] }
    >("/api/paper/positions");
    if (Array.isArray(raw)) return raw;
    if (Array.isArray(raw.items)) return raw.items;
    if (Array.isArray(raw.positions)) return raw.positions;
    if (Array.isArray(raw.holdings)) return raw.holdings;
    return [];
  },
  paperTrades: async () => {
    const raw = await getJson<
      PaperTrade[] | { items?: PaperTrade[]; trades?: PaperTrade[]; records?: PaperTrade[] }
    >("/api/paper/trades");
    if (Array.isArray(raw)) return raw;
    if (Array.isArray(raw.items)) return raw.items;
    if (Array.isArray(raw.trades)) return raw.trades;
    if (Array.isArray(raw.records)) return raw.records;
    return [];
  },
  paperSignals: async () => {
    const raw = await getJson<
      PaperSignal[] | { items?: PaperSignal[]; signals?: PaperSignal[]; results?: PaperSignal[] }
    >("/api/paper/signals");
    if (Array.isArray(raw)) return raw;
    if (Array.isArray(raw.items)) return raw.items;
    if (Array.isArray(raw.signals)) return raw.signals;
    if (Array.isArray(raw.results)) return raw.results;
    return [];
  },
  paperRun: () => postJson<PaperRunResult>("/api/paper/run"),
  /* A股规则策略：两个月前向模拟（与基金模拟盘独立） */
  stockPaperSummary: () =>
    getJson<StockPaperSummary>("/api/stocks/paper/summary"),
  stockPaperTrades: () =>
    getJson<StockPaperTrade[]>("/api/stocks/paper/trades"),
  stockPaperRun: () =>
    postJson<StockPaperRunResult>("/api/stocks/paper/run"),
  /* ==================== 股票研究（/api/stocks/*，后端可能尚未上线） ==================== */
  /* 股票数据可用性总览：quotes / financials / factors / signals 等数据集的 available_at */
  stockDataStatus: () => getJson<StockDataStatus>("/api/stocks/data/status"),
  /* 股票主数据列表（代码/名称/行业），可选行业或关键词过滤 */
  stockMaster: (params?: { industry?: string; search?: string; limit?: number }) => {
    const search = new URLSearchParams();
    if (params?.industry) search.set("industry", params.industry);
    if (params?.search) search.set("search", params.search);
    if (params?.limit !== undefined) search.set("limit", String(params.limit));
    const qs = search.toString();
    return getJson<StockMasterResponse | unknown[]>(`/api/stocks/master${qs ? `?${qs}` : ""}`);
  },
  /* 股票宇宙（筛选范围）：可选 universe 名称，缺省返回默认宇宙 */
  stockUniverse: (params?: { universe?: string }) => {
    const qs = params?.universe ? `?universe=${encodeURIComponent(params.universe)}` : "";
    return getJson<StockUniverseResponse | unknown[]>(`/api/stocks/universe${qs}`);
  },
  /* 研究因子横截面：可按宇宙/行业/关键词过滤 */
  stockFactors: (params?: { universe?: string; industry?: string; search?: string; limit?: number }) => {
    const search = new URLSearchParams();
    if (params?.universe) search.set("universe", params.universe);
    if (params?.industry) search.set("industry", params.industry);
    if (params?.search) search.set("search", params.search);
    if (params?.limit !== undefined) search.set("limit", String(params.limit));
    const qs = search.toString();
    return getJson<StockFactorsResponse | unknown[]>(
      `/api/stocks/research/factors${qs ? `?${qs}` : ""}`
    );
  },
  /* 股票研究信号（单只或全量） */
  stockSignals: (params?: { code?: string; limit?: number }) => {
    const search = new URLSearchParams();
    if (params?.code) search.set("code", params.code);
    if (params?.limit !== undefined) search.set("limit", String(params.limit));
    const qs = search.toString();
    return getJson<StockSignalsResponse | unknown[]>(
      `/api/stocks/research/signals${qs ? `?${qs}` : ""}`
    );
  },
  /* 股票研究回测 */
  stockBacktest: (body: StockBacktestRequest) =>
    postJson<StockBacktestResult>("/api/stocks/research/backtest", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  /* 单只股票的可用性状态（含 available_at） */
  stockStatus: (code: string) =>
    getJson<StockDataStatus>(`/api/stocks/${encodeURIComponent(code)}/status`),
  /* 单只股票主数据 + 基础信息（详情页主数据源） */
  stockDetail: (code: string) =>
    getJson<StockDetail>(`/api/stocks/${encodeURIComponent(code)}/master`),
  /* 单只股票行情快照 */
  stockQuote: (code: string) =>
    getJson<StockQuote>(`/api/stocks/${encodeURIComponent(code)}/quote`),
  /* 单只股票历史行情（K 线/收盘序列） */
  stockHistory: (code: string, params?: { start?: string; end?: string; limit?: number }) => {
    const search = new URLSearchParams();
    if (params?.start) search.set("start", params.start);
    if (params?.end) search.set("end", params.end);
    if (params?.limit !== undefined) search.set("limit", String(params.limit));
    const qs = search.toString();
    return getJson<StockPricePoint[] | { items?: StockPricePoint[]; history?: StockPricePoint[]; prices?: StockPricePoint[] }>(
      `/api/stocks/${encodeURIComponent(code)}/history${qs ? `?${qs}` : ""}`
    );
  },
  /* 单只股票白话技术趋势摘要 */
  stockTechnical: (code: string) =>
    getJson<StockTechnicalResponse>(`/api/stocks/${encodeURIComponent(code)}/technical`),
  /* 单只股票财务与估值 */
  stockFinancials: (code: string) =>
    getJson<StockFinancials>(`/api/stocks/${encodeURIComponent(code)}/financials`),
  /* 研究组合列表（基金发现 + 股票组合），接口可能缺失，调用方需优雅降级 */
  researchPortfolios: () =>
    getJson<ResearchPortfoliosResponse | unknown[]>("/api/research/portfolios"),
  /* ==================== 全市场基金发现（/api/discovery/* 与 /api/discovery-quant/*，后端可能尚未上线） ==================== */
  /* 目录统计：基金总数 + 按类型/市场的分布 */
  discoveryCatalogStats: () =>
    getJson<DiscoveryCatalogStats>("/api/discovery/catalog/stats"),
  /* 目录列表：可选类型/市场/关键词过滤与分页 */
  discoveryCatalogList: (params?: {
    type?: string;
    market?: string;
    search?: string;
    limit?: number;
    offset?: number;
  }) => {
    const search = new URLSearchParams();
    if (params?.type) search.set("fund_type", params.type);
    if (params?.market) search.set("market", params.market);
    if (params?.search) search.set("keyword", params.search);
    if (params?.limit !== undefined) search.set("limit", String(params.limit));
    if (params?.offset !== undefined) search.set("offset", String(params.offset));
    const qs = search.toString();
    return getJson<DiscoveryCatalogListResponse>(
      `/api/discovery/catalog${qs ? `?${qs}` : ""}`
    );
  },
  /* 手动触发全市场目录同步 */
  discoveryCatalogSync: () =>
    postJson<DiscoveryCatalogSyncResult>("/api/discovery/catalog/sync", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_active: false, mark_inactive: false }),
    }),
  /* 候选池列表 */
  discoveryPools: () =>
    getJson<DiscoveryPool[] | { items?: DiscoveryPool[]; pools?: DiscoveryPool[] }>(
      "/api/discovery/pools"
    ),
  /* 按筛选条件构建候选池（max_size 缺省 800） */
  discoveryPoolBuild: (body: DiscoveryPoolBuildRequest) =>
    postJson<DiscoveryPoolBuildResult>("/api/discovery/pools/build", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_size: 800, ...body }),
    }),
  /* 候选池详情：成员列表 + 历史覆盖进度 */
  discoveryPoolDetail: (poolId: string | number) =>
    getJson<DiscoveryPoolDetail>(
      `/api/discovery/pools/${encodeURIComponent(String(poolId))}`
    ),
  /* 历史回填后刷新成员样本数与研究就绪状态 */
  discoveryPoolRefreshNav: (poolId: string | number) =>
    postJson<DiscoveryPoolDetail>(
      `/api/discovery/pools/${encodeURIComponent(String(poolId))}/refresh-nav`
    ),
  /* 因子榜：池内基金的动量/风险因子打分与综合分 */
  discoveryPoolFactors: (poolId: string | number) =>
    getJson<DiscoveryFactorsResponse>(
      `/api/discovery/quant/factors?pool_id=${encodeURIComponent(String(poolId))}&limit=100&min_samples=120`
    ),
  /* 双动量：绝对动量（12-1）过滤 + 相对动量排名 */
  discoveryPoolDualMomentum: (poolId: string | number) =>
    getJson<DiscoveryDualMomentumResponse>(
      `/api/discovery/quant/dual-momentum?pool_id=${encodeURIComponent(String(poolId))}`
    ),
  /* 当期入选信号：月度信号日 top_n 与权重 */
  discoveryPoolSignals: (poolId: string | number) =>
    getJson<DiscoverySignalsResponse>(
      `/api/discovery/quant/signals-v2?pool_id=${encodeURIComponent(String(poolId))}`
    ),
  /* 候选池 V2 回测（响应结构与 /api/quant/v2/backtest 一致） */
  discoveryPoolBacktest: (poolId: string | number, body: DiscoveryBacktestRequest) =>
    postJson<BacktestV2Result>("/api/discovery/quant/backtest-v2", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, pool_id: Number(poolId) }),
    }),
  /* 候选池统计验证（响应结构与 /api/quant/validation 一致） */
  discoveryPoolValidation: (poolId: string | number, body: DiscoveryValidationRequest) =>
    postJson<ValidationResponse>("/api/discovery/quant/validation", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, pool_id: Number(poolId) }),
    }),
  /* ==================== 基金详情（/api/funds/*） ==================== */
  /* 单只基金聚合详情：概况/介绍/量化指标/重仓股/行业配置 */
  fundDetail: (code: string) =>
    getJson<FundDetailResponse>(`/api/funds/${encodeURIComponent(code)}/detail`),
  /* 手动刷新基金详情（重新拉取介绍与披露数据），响应结构与详情一致并带 refreshed 标记 */
  fundRefresh: (code: string) =>
    postJson<FundDetailResponse>(`/api/funds/${encodeURIComponent(code)}/refresh`),
};
