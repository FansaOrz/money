"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, ApiError, peekApiCache } from "@/lib/api";
import {
  fmtBeijingTime,
  fmtDate,
  fmtMoney,
  fmtPercent,
  fmtShares,
  signClass,
  toNumber,
} from "@/lib/normalize";
import type { FundDetailResponse } from "@/lib/types";
import { Card, EmptyState, ErrorState, PageHeader, Spinner } from "@/components/ui";
import { MetricLabel } from "@/components/KnowledgeLink";
import { LazyFundHistoryChart } from "@/components/LazyFundHistoryChart";
import { WatchlistButton } from "@/components/WatchlistButton";

/* ---------- 视图模型 ---------- */

interface OverviewItem {
  label: string;
  value: string;
}

interface IntroField {
  key: string;
  label: string;
  text: string;
}

interface MetricItem {
  key: string;
  label: string;
  value: number;
  kind: "percent" | "number";
  term?: string;
}

interface HoldingRow {
  key: string;
  rank: number | null;
  stockCode: string;
  stockName: string;
  weight: number | null;
  shares: number | null;
  marketValue: number | null;
}

interface IndustryRow {
  key: string;
  name: string;
  weight: number | null;
  marketValue: number | null;
}

interface NewsEventView {
  id: string;
  title: string;
  summary: string;
  direction: string;
  impactLevel: string;
  relationType: string;
  reason: string;
  score: number | null;
  publishedAt: string | null;
  sourceCount: number | null;
  analysisMethod: string;
}

interface FundDetailView {
  code: string;
  name: string;
  fundType: string;
  market: string;
  family: string;
  shareClass: string;
  active: boolean | null;
  overview: OverviewItem[];
  introFields: IntroField[];
  introSource: string | null;
  introFetchedAt: string | null;
  metrics: MetricItem[];
  metricsAsOf: string | null;
  metricsBasis: string | null;
  advice: {
    action: string;
    label: string;
    score: number | null;
    confidence: string;
    horizon: string;
    summary: string;
    reasons: string[];
    risks: string[];
    invalidation: string;
  } | null;
  analysis: {
    quantScore: number | null;
    newsScore: number | null;
    combinedScore: number | null;
    quantView: string;
    newsView: string;
    portfolioView: string;
    conclusion: string;
    conflictNote: string | null;
    asOf: string | null;
    newsEventCount: number;
    newsAnalysisMethod: string;
    keyEvents: NewsEventView[];
  } | null;
  holdings: HoldingRow[];
  industries: IndustryRow[];
  reportDate: string | null;
  warnings: string[];
}

/* ---------- 归一化（宽松可选字段兼容） ---------- */

function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

/** 指标键 -> 展示标签、格式与知识词条 slug；未匹配的键按原始键名兜底 */
const METRIC_META: Record<string, { label: string; kind: "percent" | "number"; term?: string }> = {
  /* 因子榜真实键（FactorBoardItem） */
  return_1m: { label: "近 1 月收益", kind: "percent", term: "total-return" },
  return_3m: { label: "近 3 月收益", kind: "percent", term: "total-return" },
  return_1y: { label: "近 1 年收益", kind: "percent", term: "total-return" },
  return_3y: { label: "近 3 年收益", kind: "percent", term: "total-return" },
  annual_volatility: { label: "年化波动率", kind: "percent", term: "annual-volatility" },
  max_drawdown: { label: "最大回撤", kind: "percent", term: "max-drawdown" },
  sharpe: { label: "夏普比率", kind: "number", term: "sharpe-ratio" },
  sortino: { label: "索提诺比率", kind: "number", term: "sortino-ratio" },
  calmar: { label: "卡玛比率", kind: "number", term: "calmar-ratio" },
  cvar95: { label: "CVaR95", kind: "percent", term: "cvar95" },
  momentum_12_1: { label: "动量（12-1）", kind: "percent", term: "momentum-12-1" },
  quantile: { label: "同类分位数", kind: "percent", term: "quantile" },
  /* 兼容键（旧口径/其他来源） */
  return_20d: { label: "20 日收益", kind: "percent", term: "total-return" },
  return_60d: { label: "60 日收益", kind: "percent", term: "total-return" },
  return_120d: { label: "120 日收益", kind: "percent", term: "total-return" },
  return_250d: { label: "250 日收益", kind: "percent", term: "total-return" },
  momentum: { label: "动量", kind: "percent", term: "momentum-score" },
  momentum_6_1: { label: "动量（6-1）", kind: "percent", term: "momentum-12-1" },
  momentum_3: { label: "动量（3 月）", kind: "percent", term: "momentum-12-1" },
  annualized_return: { label: "年化收益", kind: "percent", term: "annualized-return" },
  annual_return: { label: "年化收益", kind: "percent", term: "annualized-return" },
  annualized_volatility: { label: "年化波动率", kind: "percent", term: "annual-volatility" },
  volatility: { label: "波动率", kind: "percent", term: "annual-volatility" },
  sharpe_ratio: { label: "夏普比率", kind: "number", term: "sharpe-ratio" },
  sortino_ratio: { label: "索提诺比率", kind: "number", term: "sortino-ratio" },
  calmar_ratio: { label: "卡玛比率", kind: "number", term: "calmar-ratio" },
  win_rate: { label: "胜率", kind: "percent", term: "win-rate" },
  score: { label: "综合分", kind: "number", term: "composite-score" },
  composite_score: { label: "综合分", kind: "number", term: "composite-score" },
};

const METRIC_PRIORITY = [
  "return_1m",
  "return_3m",
  "return_1y",
  "return_3y",
  "momentum_12_1",
  "annual_volatility",
  "max_drawdown",
  "cvar95",
  "sharpe",
  "sortino",
  "calmar",
  "quantile",
  "return_20d",
  "return_60d",
  "return_250d",
  "annualized_return",
  "annualized_volatility",
  "win_rate",
  "score",
];

/** 指标数值兜底：ratio 类字段若按 0-100 口径返回则折算回小数 */
function metricRatioValue(v: unknown): number | null {
  const n = toNumber(v);
  if (n === null) return null;
  return Math.abs(n) > 1 && Math.abs(n) <= 100 ? n / 100 : n;
}

function normalizeFundDetail(raw: FundDetailResponse, fallbackCode: string): FundDetailView {
  const obj = raw && typeof raw === "object" ? raw : {};
  const code = str(obj.code) ?? str(obj.fund_code) ?? fallbackCode;
  const name = str(obj.name) ?? str(obj.fund_name) ?? code;
  const profile = obj.profile && typeof obj.profile === "object" ? obj.profile : null;

  /* 概览：目录/介绍合并，缺省显示 — */
  const overview: OverviewItem[] = (
    [
      ["基金类型", str(obj.fund_type) ?? str(obj.type) ?? str(profile?.fund_type)],
      ["市场", str(obj.market)],
      ["基金系列", str(obj.family)],
      ["份额类别", str(obj.share_class)],
      ["基金公司", str(profile?.company)],
      ["基金经理", str(profile?.manager)],
      ["托管人", str(profile?.custodian)],
      ["成立日期", str(profile?.inception_date) ? fmtDate(profile?.inception_date) : null],
      ["最新规模", str(profile?.latest_scale)],
      ["管理费率", str(profile?.management_fee)],
      ["托管费率", str(profile?.custody_fee)],
      ["基金评级", str(profile?.rating)],
      ["评级机构", str(profile?.rating_agency)],
      ["披露截止", str(obj.report_date) ? fmtDate(obj.report_date) : null],
    ] as [string, string | null][]
  ).map(([label, value]) => ({ label, value: value ?? "—" }));

  /* 介绍：长文本字段 + 全称，展示层折叠 */
  const introFields: IntroField[] = (
    [
      ["full_name", "基金全称", str(profile?.full_name)],
      ["investment_objective", "投资目标", str(profile?.investment_objective)],
      ["investment_strategy", "投资策略", str(profile?.investment_strategy)],
      ["benchmark", "业绩比较基准", str(profile?.benchmark)],
    ] as [string, string, string | null][]
  )
    .filter((f): f is [string, string, string] => f[2] !== null)
    .map(([key, label, text]) => ({ key, label, text }));

  /* 指标：字典形态展开，已知键按优先级排序，未知键兜底 */
  const metrics: MetricItem[] = [];
  const metricObj = obj.metrics && typeof obj.metrics === "object" ? obj.metrics : null;
  if (metricObj) {
    const seen = new Set<string>();
    const push = (key: string, value: unknown) => {
      if (seen.has(key)) return;
      const meta = METRIC_META[key] ?? { label: key, kind: "number" as const };
      const num =
        meta.kind === "percent" &&
        [
          "win_rate",
          "max_drawdown",
          "volatility",
          "annual_volatility",
          "annualized_volatility",
          "cvar95",
          "quantile",
        ].includes(key)
          ? metricRatioValue(value)
          : toNumber(value);
      if (num === null) return;
      seen.add(key);
      metrics.push({ key, label: meta.label, value: num, kind: meta.kind, term: meta.term });
    };
    for (const key of METRIC_PRIORITY) {
      if (key in metricObj) push(key, metricObj[key]);
    }
    for (const [key, value] of Object.entries(metricObj)) {
      if (typeof value === "number" || typeof value === "string") push(key, value);
    }
  }

  const holdings: HoldingRow[] = (Array.isArray(obj.holdings) ? obj.holdings : []).map((h, i) => {
    const stockCode = str(h?.stock_code) ?? str(h?.code) ?? "—";
    return {
      key: `${stockCode}-${i}`,
      rank: toNumber(h?.rank),
      stockCode,
      stockName: str(h?.stock_name) ?? str(h?.name) ?? stockCode,
      weight: metricRatioValue(h?.weight),
      shares: toNumber(h?.shares),
      marketValue: toNumber(h?.market_value),
    };
  });

  const industries: IndustryRow[] = (Array.isArray(obj.industries) ? obj.industries : []).map((d, i) => {
    const industry = str(d?.industry) ?? str(d?.name) ?? `行业 ${i + 1}`;
    return {
      key: `${industry}-${i}`,
      name: industry,
      weight: metricRatioValue(d?.weight),
      marketValue: toNumber(d?.market_value),
    };
  });
  const rawAnalysis =
    obj.analysis && typeof obj.analysis === "object" ? obj.analysis : null;
  const keyEvents: NewsEventView[] = (
    rawAnalysis && Array.isArray(rawAnalysis.key_events) ? rawAnalysis.key_events : []
  ).map((event, index) => ({
    id: String(event?.id ?? index),
    title: str(event?.title) ?? "未命名事件",
    summary: str(event?.summary) ?? "",
    direction: str(event?.direction) ?? "neutral",
    impactLevel: str(event?.impact_level) ?? "low",
    relationType: str(event?.relation_type) ?? "",
    reason: str(event?.reason) ?? "",
    score: toNumber(event?.score),
    publishedAt: str(event?.published_at),
    sourceCount: toNumber(event?.source_count),
    analysisMethod: str(event?.analysis_method) ?? "rules",
  }));

  return {
    code,
    name,
    fundType: str(obj.fund_type) ?? str(obj.type) ?? str(profile?.fund_type) ?? "—",
    market: str(obj.market) ?? "—",
    family: str(obj.family) ?? "",
    shareClass: str(obj.share_class) ?? "",
    active: typeof obj.active === "boolean" ? obj.active : null,
    overview,
    introFields,
    introSource: str(profile?.source),
    introFetchedAt: str(profile?.fetched_at),
    metrics: metrics.slice(0, 12),
    metricsAsOf: str(obj.metrics_as_of),
    metricsBasis: str(obj.metrics_basis),
    advice:
      obj.advice && typeof obj.advice === "object"
        ? {
            action: str(obj.advice.action) ?? "watch",
            label: str(obj.advice.label) ?? "暂时观望",
            score: toNumber(obj.advice.score),
            confidence: str(obj.advice.confidence) ?? "low",
            horizon: str(obj.advice.horizon) ?? "未来 1～3 个月",
            summary: str(obj.advice.summary) ?? "",
            reasons: Array.isArray(obj.advice.reasons)
              ? obj.advice.reasons.filter((item): item is string => typeof item === "string")
              : [],
            risks: Array.isArray(obj.advice.risks)
              ? obj.advice.risks.filter((item): item is string => typeof item === "string")
              : [],
            invalidation: str(obj.advice.invalidation) ?? "",
          }
        : null,
    analysis: rawAnalysis
      ? {
          quantScore: toNumber(rawAnalysis.quant_score),
          newsScore: toNumber(rawAnalysis.news_score),
          combinedScore: toNumber(rawAnalysis.combined_score),
          quantView: str(rawAnalysis.quant_view) ?? "历史数据暂时无法判断",
          newsView: str(rawAnalysis.news_view) ?? "近期没有明显新闻影响",
          portfolioView: str(rawAnalysis.portfolio_view) ?? "暂无持仓占比",
          conclusion: str(rawAnalysis.conclusion) ?? "",
          conflictNote: str(rawAnalysis.conflict_note),
          asOf: str(rawAnalysis.as_of),
          newsEventCount: toNumber(rawAnalysis.news_event_count) ?? 0,
          newsAnalysisMethod: str(rawAnalysis.news_analysis_method) ?? "no_news",
          keyEvents,
        }
      : null,
    holdings,
    industries,
    reportDate: str(obj.report_date),
    warnings: Array.isArray(obj.warnings)
      ? obj.warnings.filter((w): w is string => typeof w === "string" && w.length > 0)
      : [],
  };
}

/* ---------- 展示组件 ---------- */

/** 数据提示：与基金发现页一致的折叠样式 */
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

/** 长文本段落：超过阈值折叠为 4 行，可展开 */
const COLLAPSE_THRESHOLD = 160;

function CollapsibleText({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const collapsible = text.length > COLLAPSE_THRESHOLD;
  return (
    <div>
      <p
        className={`whitespace-pre-line text-sm leading-relaxed text-slate-600 ${
          !expanded && collapsible ? "line-clamp-4" : ""
        }`}
      >
        {text}
      </p>
      {collapsible && (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="mt-1 text-xs font-medium text-blue-600 hover:text-blue-700"
        >
          {expanded ? "收起" : "展开全文"}
        </button>
      )}
    </div>
  );
}

/* ---------- 页面 ---------- */

export default function FundDetailPage() {
  const params = useParams<{ code: string }>();
  const code = decodeURIComponent(params.code ?? "");

  const cachedRaw = peekApiCache<FundDetailResponse>(
    `/api/funds/${encodeURIComponent(code)}/detail`
  );
  const cachedDetail = cachedRaw ? normalizeFundDetail(cachedRaw, code) : null;
  const [loading, setLoading] = useState(!cachedDetail);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshMessage, setRefreshMessage] = useState<string | null>(null);
  const [detail, setDetail] = useState<FundDetailView | null>(cachedDetail);

  const load = useCallback(async () => {
    if (!code) return;
    setError(null);
    setRefreshMessage(null);
    try {
      const raw = await api.fundDetail(code);
      setDetail(normalizeFundDetail(raw, code));
    } catch (e) {
      setDetail(null);
      setError(
        e instanceof ApiError
          ? e.status === 404
            ? `基金 ${code} 不存在或尚未纳入目录`
            : e.message
          : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      setLoading(false);
    }
  }, [code]);

  useEffect(() => {
    void load();
  }, [load]);

  const refresh = useCallback(async () => {
    if (!code || refreshing) return;
    setRefreshing(true);
    setRefreshMessage(null);
    try {
      const raw = await api.fundRefresh(code);
      setDetail(normalizeFundDetail(raw, code));
      setRefreshMessage("已刷新：基金介绍与披露数据已重新拉取。");
    } catch (e) {
      setRefreshMessage(
        e instanceof ApiError ? `刷新失败：${e.message}` : "刷新失败：网络请求异常，请稍后重试"
      );
    } finally {
      setRefreshing(false);
    }
  }, [code, refreshing]);

  const headerDescription = useMemo(() => {
    if (!detail) return "基金详情：概况、介绍、量化指标、走势与底层披露";
    const parts = [
      detail.fundType !== "—" ? detail.fundType : null,
      detail.market !== "—" ? detail.market : null,
      detail.family || null,
      detail.shareClass ? `${detail.shareClass} 类` : null,
      detail.active === false ? "已停用" : null,
    ].filter((x): x is string => typeof x === "string" && x.length > 0);
    return parts.length > 0 ? parts.join(" · ") : "基金详情：概况、介绍、量化指标、走势与底层披露";
  }, [detail]);

  return (
    <>
      <PageHeader
        title={`${detail?.name ?? code}（${code}）`}
        description={headerDescription}
        action={
          <div className="flex items-center gap-2">
            <WatchlistButton kind="fund" code={code} name={detail?.name ?? code} />
            <button
              type="button"
              onClick={() => void refresh()}
              disabled={refreshing || loading}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`}
              >
                <path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" />
              </svg>
              {refreshing ? "刷新中…" : "刷新数据"}
            </button>
            <Link
              href="/watchlist"
              className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-50"
            >
              返回自选
            </Link>
          </div>
        }
      />

      {loading ? (
        <Card>
          <Spinner label={`正在加载 ${code} 的基金详情…`} />
        </Card>
      ) : error ? (
        <Card>
          <ErrorState message={error} onRetry={load} />
        </Card>
      ) : detail ? (
        <div className="space-y-6">
          {refreshMessage && (
            <div className="rounded-lg border border-slate-200 bg-white px-3.5 py-2.5 text-xs text-slate-600">
              {refreshMessage}
            </div>
          )}

          <WarningsBlock warnings={detail.warnings} />

          {detail.advice && (
            <Card className="overflow-hidden">
              <div
                className={`border-l-4 px-4 py-5 sm:px-5 ${
                  detail.advice.action === "add"
                    ? "border-l-rose-500 bg-rose-50/50"
                    : detail.advice.action === "hold"
                      ? "border-l-blue-500 bg-blue-50/40"
                      : detail.advice.action === "watch"
                        ? "border-l-slate-400 bg-slate-50"
                        : "border-l-emerald-500 bg-emerald-50/50"
                }`}
              >
                <div className="flex flex-wrap items-center gap-3">
                  <div>
                    <p className="text-xs font-medium text-slate-500">当前建议 · {detail.advice.horizon}</p>
                    <h2 className="mt-1 text-xl font-semibold text-slate-900">{detail.advice.label}</h2>
                  </div>
                  {detail.advice.score !== null && (
                    <span className="rounded-full bg-white px-2.5 py-1 text-xs font-medium text-slate-600 shadow-sm">
                      趋势评分 {detail.advice.score}/100
                    </span>
                  )}
                  <span className="rounded-full bg-white px-2.5 py-1 text-xs text-slate-500 shadow-sm">
                    可信度 {detail.advice.confidence === "high" ? "较高" : detail.advice.confidence === "medium" ? "中等" : "较低"}
                  </span>
                </div>
                <p className="mt-3 text-sm leading-relaxed text-slate-700">{detail.advice.summary}</p>
                {detail.analysis && (
                  <>
                    <div className="mt-4 grid gap-2 sm:grid-cols-3">
                      <div className="rounded-lg border border-white/80 bg-white/75 px-3 py-2.5">
                        <p className="text-xs font-semibold text-slate-500">数据趋势</p>
                        <p className="mt-1 text-sm text-slate-700">{detail.analysis.quantView}</p>
                      </div>
                      <div className="rounded-lg border border-white/80 bg-white/75 px-3 py-2.5">
                        <p className="text-xs font-semibold text-slate-500">新闻影响</p>
                        <p className="mt-1 text-sm text-slate-700">{detail.analysis.newsView}</p>
                      </div>
                      <div className="rounded-lg border border-white/80 bg-white/75 px-3 py-2.5">
                        <p className="text-xs font-semibold text-slate-500">你的持仓</p>
                        <p className="mt-1 text-sm text-slate-700">{detail.analysis.portfolioView}</p>
                      </div>
                    </div>
                    {detail.analysis.conflictNote && (
                      <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                        <span className="font-semibold">信号有分歧：</span>
                        {detail.analysis.conflictNote}
                      </div>
                    )}
                    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
                      <span>
                        综合分 {detail.analysis.combinedScore ?? "—"} / 100
                        {detail.analysis.newsScore !== null
                          ? `（新闻修正 ${detail.analysis.newsScore >= 0 ? "+" : ""}${detail.analysis.newsScore.toFixed(2)}）`
                          : ""}
                      </span>
                      <span>
                        新闻分析：
                        {detail.analysis.newsAnalysisMethod === "llm"
                          ? "大模型"
                          : detail.analysis.newsAnalysisMethod === "mixed"
                            ? "大模型 + 规则"
                            : detail.analysis.newsAnalysisMethod === "rules"
                              ? "规则初判"
                              : "暂无有效新闻"}
                      </span>
                      {detail.analysis.asOf && (
                        <span>消息截至 {fmtBeijingTime(detail.analysis.asOf)}</span>
                      )}
                    </div>
                  </>
                )}
                <div className="mt-4 grid gap-4 sm:grid-cols-2">
                  <div>
                    <p className="text-xs font-semibold text-slate-700">为什么</p>
                    <ul className="mt-2 space-y-1.5 text-sm text-slate-600">
                      {detail.advice.reasons.map((reason) => <li key={reason}>• {reason}</li>)}
                    </ul>
                  </div>
                  <div>
                    <p className="text-xs font-semibold text-slate-700">需要留意</p>
                    {detail.advice.risks.length > 0 ? (
                      <ul className="mt-2 space-y-1.5 text-sm text-amber-800">
                        {detail.advice.risks.map((risk) => <li key={risk}>• {risk}</li>)}
                      </ul>
                    ) : (
                      <p className="mt-2 text-sm text-slate-500">目前没有明显的额外风险提示。</p>
                    )}
                  </div>
                </div>
                {detail.advice.invalidation && (
                  <p className="mt-4 border-t border-slate-200/70 pt-3 text-xs text-slate-400">
                    {detail.advice.invalidation}
                  </p>
                )}
                {detail.analysis && detail.analysis.keyEvents.length > 0 && (
                  <div className="mt-4 border-t border-slate-200/70 pt-3">
                    <p className="text-xs font-semibold text-slate-700">
                      影响较大的近期事件（{detail.analysis.newsEventCount} 条有效事件）
                    </p>
                    <div className="mt-2 space-y-2">
                      {detail.analysis.keyEvents.slice(0, 3).map((event) => (
                        <div key={event.id} className="rounded-lg bg-white/70 px-3 py-2.5">
                          <div className="flex flex-wrap items-center gap-2">
                            <span
                              className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                                event.direction === "positive"
                                  ? "bg-rose-100 text-rose-700"
                                  : event.direction === "negative"
                                    ? "bg-emerald-100 text-emerald-700"
                                    : "bg-slate-100 text-slate-600"
                              }`}
                            >
                              {event.direction === "positive"
                                ? "偏利好"
                                : event.direction === "negative"
                                  ? "偏利空"
                                  : "中性"}
                            </span>
                            <p className="min-w-0 flex-1 text-sm font-medium text-slate-800">
                              {event.title}
                            </p>
                            {event.publishedAt && (
                              <span className="text-[11px] text-slate-400">
                                {fmtDate(event.publishedAt)}
                              </span>
                            )}
                          </div>
                          <p className="mt-1.5 text-xs leading-relaxed text-slate-600">
                            {event.summary}
                          </p>
                          {event.reason && (
                            <p className="mt-1 text-[11px] text-slate-400">
                              为什么相关：{event.reason}
                            </p>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </Card>
          )}

          {/* 概览 */}
          <Card className="px-4 py-5 sm:px-5">
            <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-sm font-semibold text-slate-800">概览</h2>
              {detail.reportDate && (
                <p className="text-xs text-slate-400">披露截止 {fmtDate(detail.reportDate)}</p>
              )}
            </div>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-4">
              {detail.overview.map((item) => (
                <div key={item.label}>
                  <dt className="text-xs text-slate-500">{item.label}</dt>
                  <dd className="mt-0.5 break-all text-sm font-medium text-slate-800">{item.value}</dd>
                </div>
              ))}
            </dl>
          </Card>

          {/* 基金介绍（长文本折叠） */}
          <Card className="px-4 py-5 sm:px-5">
            <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-sm font-semibold text-slate-800">基金介绍</h2>
              {detail.introFetchedAt && (
                <p className="text-xs text-slate-400">
                  {detail.introSource ? `来源 ${detail.introSource} · ` : ""}
                  更新于 {fmtBeijingTime(detail.introFetchedAt)}
                </p>
              )}
            </div>
            {detail.introFields.length > 0 ? (
              <div className="space-y-4">
                {detail.introFields.map((field) => (
                  <div key={field.key}>
                    <p className="mb-1 text-xs font-medium text-slate-500">{field.label}</p>
                    <CollapsibleText text={field.text} />
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                title="暂无基金介绍"
                hint="基金概况接口未返回介绍文本；点击右上角「刷新数据」可重新拉取。"
              />
            )}
          </Card>

          {/* 量化指标 */}
          <Card className="px-4 py-5 sm:px-5">
            <div className="mb-4">
              <h2 className="text-sm font-semibold text-slate-800">量化指标</h2>
              <p className="mt-0.5 text-xs text-slate-400">
                {detail.metricsAsOf ? `截至 ${fmtDate(detail.metricsAsOf)} · ` : ""}
                {detail.metricsBasis ?? "基于历史净值的横截面因子打分，供研究参考"}
              </p>
            </div>
            {detail.metrics.length > 0 ? (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                {detail.metrics.map((m) => (
                  <div key={m.key} className="rounded-lg bg-slate-50 px-3 py-2.5">
                    <p className="text-xs text-slate-500">
                      <MetricLabel term={m.term}>{m.label}</MetricLabel>
                    </p>
                    <p
                      className={`mt-0.5 text-sm font-semibold tabular-nums ${
                        m.kind === "percent" ? signClass(m.value) : "text-slate-800"
                      }`}
                    >
                      {m.kind === "percent" ? fmtPercent(m.value) : m.value.toFixed(2)}
                    </p>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                title="暂无量化指标"
                hint="历史净值样本不足或指标服务暂不可用；历史同步完成后自动补齐。"
              />
            )}
          </Card>

          {/* 净值走势（复用现有走势图组件，进入视口后懒加载） */}
          <Card className="px-4 py-5 sm:px-5">
            <LazyFundHistoryChart fundCode={detail.code} fundName={detail.name} />
          </Card>

          {/* 重仓股 + 行业配置 */}
          <div className="grid gap-6 lg:grid-cols-2">
            <Card className="overflow-hidden">
              <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-4 sm:px-5">
                <h2 className="text-sm font-semibold text-slate-800">
                  重仓股{detail.holdings.length > 0 ? `（${detail.holdings.length}）` : ""}
                </h2>
                {detail.reportDate && (
                  <p className="text-xs text-slate-400">报告期 {fmtDate(detail.reportDate)}</p>
                )}
              </div>
              {detail.holdings.length === 0 ? (
                <EmptyState
                  title="暂无重仓股披露"
                  hint="该基金暂无可用的季度重仓股数据；点击「刷新数据」可重新拉取。"
                />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[480px] text-sm">
                    <thead>
                      <tr className="border-t border-slate-100 bg-slate-50 text-left text-xs text-slate-500">
                        <th className="px-4 py-2.5 font-medium sm:px-5">#</th>
                        <th className="px-4 py-2.5 font-medium">股票</th>
                        <th className="px-4 py-2.5 text-right font-medium">占净值比</th>
                        <th className="px-4 py-2.5 text-right font-medium sm:px-5">持股数</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.holdings.map((h, i) => (
                        <tr key={h.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                          <td className="px-4 py-2.5 tabular-nums text-slate-400 sm:px-5">
                            {h.rank ?? i + 1}
                          </td>
                          <td className="px-4 py-2.5">
                            <p className="font-medium text-slate-800">{h.stockName}</p>
                            <p className="text-xs text-slate-400">{h.stockCode}</p>
                          </td>
                          <td className="px-4 py-2.5 text-right tabular-nums text-slate-800">
                            {h.weight === null ? "—" : fmtPercent(h.weight).replace("+", "")}
                          </td>
                          <td className="px-4 py-2.5 text-right tabular-nums text-slate-600 sm:px-5">
                            {h.shares === null ? "—" : fmtShares(h.shares)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            <Card className="overflow-hidden">
              <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-4 sm:px-5">
                <h2 className="text-sm font-semibold text-slate-800">
                  行业配置{detail.industries.length > 0 ? `（${detail.industries.length}）` : ""}
                </h2>
                {detail.reportDate && (
                  <p className="text-xs text-slate-400">报告期 {fmtDate(detail.reportDate)}</p>
                )}
              </div>
              {detail.industries.length === 0 ? (
                <EmptyState
                  title="暂无行业配置披露"
                  hint="该基金暂无可用的行业配置数据；点击「刷新数据」可重新拉取。"
                />
              ) : (
                <ul className="divide-y divide-slate-100 border-t border-slate-100">
                  {detail.industries.map((d) => {
                    const pct = d.weight === null ? null : Math.abs(d.weight) <= 1 ? d.weight * 100 : d.weight;
                    return (
                      <li key={d.key} className="px-4 py-2.5 sm:px-5">
                        <div className="flex items-baseline justify-between gap-3">
                          <p className="truncate text-sm text-slate-700">{d.name}</p>
                          <p className="shrink-0 text-sm tabular-nums text-slate-800">
                            {pct === null ? "—" : `${pct.toFixed(2)}%`}
                          </p>
                        </div>
                        {pct !== null && (
                          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-100">
                            <div
                              className="h-full rounded-full bg-blue-500/70"
                              style={{ width: `${Math.min(Math.max(pct, 0), 100)}%` }}
                            />
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </Card>
          </div>

          <p className="text-xs leading-relaxed text-slate-400">
            重仓股与行业配置来自基金定期报告披露，存在披露滞后；量化指标基于历史净值计算，仅供研究参考，不构成投资建议。
          </p>
        </div>
      ) : null}
    </>
  );
}
