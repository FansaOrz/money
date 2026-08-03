"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import {
  fmtBeijingTime,
  fmtDate,
  fmtMoney,
  fmtPercent,
  normalizeStockDetail,
  normalizeStockFactors,
  normalizeStockFinancials,
  normalizeStockHistory,
  normalizeStockQuote,
  normalizeStockSignals,
  signClass,
  type StockDetailView,
  type StockFactorsView,
  type StockFinancialsView,
  type StockPricePointView,
  type StockQuoteView,
  type StockSignalsView,
} from "@/lib/normalize";
import { Card, EmptyState, ErrorState, PageHeader, Spinner } from "@/components/ui";
import { PriceChart } from "@/components/PriceChart";
import { WatchlistButton } from "@/components/WatchlistButton";
import type { StockTechnicalResponse } from "@/lib/types";

const API_DOWN_HINT =
  "股票研究接口暂不可用。该功能依赖后端 /api/stocks/* 接口，当前后端尚未上线该模块。";

/** available_at 信息条 */
function AvailabilityNotice({
  items,
}: {
  items: { label: string; availableAt: string | null }[];
}) {
  const withTime = items.filter((x) => x.availableAt);
  if (withTime.length === 0) return null;
  return (
    <div className="mb-6 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-slate-200 bg-white px-3.5 py-2.5 text-xs text-slate-500">
      <span className="font-medium text-slate-600">数据可用时间（available_at）</span>
      {withTime.map((x) => (
        <span key={x.label}>
          {x.label} <span className="tabular-nums text-slate-700">{fmtBeijingTime(x.availableAt)}</span>
        </span>
      ))}
    </div>
  );
}

function FinancialMetricValue({ value, format }: { value: number | null; format: "percent" | "number" | "money" }) {
  if (value === null) return <span className="text-slate-400">—</span>;
  if (format === "percent") return <span className={signClass(value)}>{fmtPercent(value)}</span>;
  if (format === "money") {
    // 市值类：>=1e8 转亿元展示
    const yi = Math.abs(value) >= 1e8 ? `${(value / 1e8).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} 亿` : fmtMoney(value);
    return <span className="text-slate-800">{yi}</span>;
  }
  return <span className="text-slate-800">{value.toFixed(2)}</span>;
}

const TREND_META: Record<
  StockTechnicalResponse["trend"],
  { label: string; tone: string; bar: string }
> = {
  strong_bullish: { label: "明显偏强", tone: "text-rose-700 bg-rose-50 border-rose-200", bar: "bg-rose-500" },
  bullish: { label: "走势偏强", tone: "text-orange-700 bg-orange-50 border-orange-200", bar: "bg-orange-400" },
  neutral: { label: "震荡整理", tone: "text-slate-700 bg-slate-50 border-slate-200", bar: "bg-slate-400" },
  bearish: { label: "走势偏弱", tone: "text-teal-700 bg-teal-50 border-teal-200", bar: "bg-teal-400" },
  strong_bearish: { label: "明显偏弱", tone: "text-emerald-700 bg-emerald-50 border-emerald-200", bar: "bg-emerald-500" },
  insufficient: { label: "数据不足", tone: "text-slate-500 bg-slate-50 border-slate-200", bar: "bg-slate-300" },
};

function indicatorValue(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? "—" : value.toFixed(digits);
}

export default function StockDetailPage() {
  const params = useParams<{ code: string }>();
  const code = decodeURIComponent(params.code ?? "");

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [partial, setPartial] = useState<string[]>([]);

  const [detail, setDetail] = useState<StockDetailView | null>(null);
  const [quote, setQuote] = useState<StockQuoteView | null>(null);
  const [history, setHistory] = useState<StockPricePointView[]>([]);
  const [financials, setFinancials] = useState<StockFinancialsView | null>(null);
  const [factors, setFactors] = useState<StockFactorsView | null>(null);
  const [signals, setSignals] = useState<StockSignalsView | null>(null);
  const [technical, setTechnical] = useState<StockTechnicalResponse | null>(null);

  const load = useCallback(async () => {
    if (!code) return;
    setLoading(true);
    setError(null);
    setPartial([]);
    try {
      const [detailRes, quoteRes, historyRes, financialsRes, factorsRes, signalsRes, technicalRes] =
        await Promise.allSettled([
          api.stockDetail(code),
          api.stockQuote(code),
          api.stockHistory(code, { limit: 250 }),
          api.stockFinancials(code),
          api.stockFactors({ search: code, limit: 5 }),
          api.stockSignals({ code, limit: 20 }),
          api.stockTechnical(code),
        ]);

      const failed: string[] = [];
      if (detailRes.status === "fulfilled") {
        setDetail(normalizeStockDetail(detailRes.value, code));
      } else {
        failed.push("主数据");
      }
      if (quoteRes.status === "fulfilled") {
        setQuote(normalizeStockQuote(quoteRes.value));
      } else {
        failed.push("行情");
      }
      if (historyRes.status === "fulfilled") {
        setHistory(normalizeStockHistory(historyRes.value));
      } else {
        failed.push("历史行情");
      }
      if (financialsRes.status === "fulfilled") {
        setFinancials(normalizeStockFinancials(financialsRes.value));
      } else {
        failed.push("财务估值");
      }
      if (factorsRes.status === "fulfilled") {
        setFactors(normalizeStockFactors(factorsRes.value as never));
      } else {
        failed.push("因子");
      }
      if (signalsRes.status === "fulfilled") {
        setSignals(normalizeStockSignals(signalsRes.value as never));
      } else {
        failed.push("信号");
      }
      if (technicalRes.status === "fulfilled") {
        setTechnical(technicalRes.value);
      } else {
        failed.push("趋势解读");
      }

      setPartial(failed);

      const allRejected = [detailRes, quoteRes, historyRes, financialsRes, factorsRes, signalsRes, technicalRes].every(
        (r) => r.status === "rejected"
      );
      if (allRejected) {
        const reason = (detailRes as PromiseRejectedResult).reason;
        throw reason instanceof Error ? reason : new Error(String(reason));
      }
    } catch (e) {
      setError(
        e instanceof ApiError
          ? `${e.message}。${e.status === 404 ? API_DOWN_HINT : ""}`
          : "网络请求失败，请确认后端服务已启动"
      );
    } finally {
      setLoading(false);
    }
  }, [code]);

  useEffect(() => {
    void load();
  }, [load]);

  const displayQuote = quote ?? detail?.quote ?? null;
  const displayHistory = history.length > 0 ? history : (detail?.history ?? []);
  const displayFinancials = financials ?? detail?.financials ?? null;
  const factorRow = factors?.items.find((f) => f.code === code) ?? factors?.items[0] ?? null;
  const detailFactors = detail?.factors ?? [];

  return (
    <>
      <PageHeader
        title={`${detail?.name ?? code}（${code}）`}
        description={
          detail
            ? `${detail.industry !== "—" ? detail.industry : "行业未知"} · ${detail.market !== "—" ? detail.market : ""}${detail.exchange !== "—" ? ` · ${detail.exchange}` : ""}`
            : "个股详情：行情、财务估值、研究因子与信号"
        }
        action={
          <div className="flex items-center gap-2">
            <WatchlistButton kind="stock" code={code} name={detail?.name ?? code} />
            <Link
              href="/stock-screener"
              className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-50"
            >
              返回筛选
            </Link>
          </div>
        }
      />

      <AvailabilityNotice
        items={[
          { label: "行情", availableAt: displayQuote?.availableAt ?? null },
          { label: "财务估值", availableAt: displayFinancials?.availableAt ?? null },
          { label: "因子", availableAt: factors?.availableAt ?? null },
          { label: "信号", availableAt: signals?.availableAt ?? null },
          { label: "主数据", availableAt: detail?.availableAt ?? null },
        ]}
      />

      {loading ? (
        <Card>
          <Spinner label={`正在加载 ${code} 的股票数据…`} />
        </Card>
      ) : error ? (
        <Card>
          <ErrorState message={error} onRetry={load} />
        </Card>
      ) : (
        <div className="space-y-6">
          {partial.length > 0 && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs text-amber-800">
              以下模块接口暂未返回数据：{partial.join("、")}。页面展示已可用部分，缺失模块在接口上线后自动补齐。
            </div>
          )}

          {/* 行情快照 */}
          <Card className="px-4 py-5 sm:px-5">
            <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-sm font-semibold text-slate-800">行情</h2>
              {displayQuote?.date && (
                <p className="text-xs text-slate-400">交易日 {fmtDate(displayQuote.date)}</p>
              )}
            </div>
            {displayQuote ? (
              <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
                <div className="rounded-lg bg-slate-50 px-3 py-2.5">
                  <p className="text-xs text-slate-500">最新价</p>
                  <p className={`mt-0.5 text-lg font-semibold tabular-nums ${signClass(displayQuote.changePct)}`}>
                    {displayQuote.price === null ? "—" : displayQuote.price.toFixed(2)}
                  </p>
                </div>
                <div className="rounded-lg bg-slate-50 px-3 py-2.5">
                  <p className="text-xs text-slate-500">涨跌幅</p>
                  <p className={`mt-0.5 text-lg font-semibold tabular-nums ${signClass(displayQuote.changePct)}`}>
                    {displayQuote.changePct === null ? "—" : fmtPercent(displayQuote.changePct)}
                  </p>
                </div>
                <div className="rounded-lg bg-slate-50 px-3 py-2.5">
                  <p className="text-xs text-slate-500">今开 / 昨收</p>
                  <p className="mt-0.5 text-lg font-semibold tabular-nums text-slate-800">
                    {displayQuote.open?.toFixed(2) ?? "—"} / {displayQuote.prevClose?.toFixed(2) ?? "—"}
                  </p>
                </div>
                <div className="rounded-lg bg-slate-50 px-3 py-2.5">
                  <p className="text-xs text-slate-500">最高 / 最低</p>
                  <p className="mt-0.5 text-lg font-semibold tabular-nums text-slate-800">
                    {displayQuote.high?.toFixed(2) ?? "—"} / {displayQuote.low?.toFixed(2) ?? "—"}
                  </p>
                </div>
              </div>
            ) : (
              <p className="mb-5 text-xs text-slate-400">行情快照接口暂未返回数据。</p>
            )}
            <PriceChart points={displayHistory} gradientId={`stock-${code}`} />
          </Card>

          {/* 面向非专业用户的白话趋势解读 */}
          <Card className="px-4 py-5 sm:px-5">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="text-sm font-semibold text-slate-800">现在是什么趋势？</h2>
                <p className="mt-1 text-xs text-slate-500">把最近价格和成交量变化翻译成白话，仅用于观察趋势</p>
              </div>
              {technical?.as_of && <p className="text-xs text-slate-400">数据截至 {fmtDate(technical.as_of)}</p>}
            </div>
            {technical ? (
              <div className="space-y-5">
                <div className={`rounded-xl border p-4 ${TREND_META[technical.trend].tone}`}>
                  <div className="flex flex-wrap items-center gap-3">
                    <span className="text-xl font-semibold">{TREND_META[technical.trend].label}</span>
                    {technical.sufficient && (
                      <span className="rounded-full bg-white/70 px-2 py-0.5 text-xs">
                        {technical.score > 0 ? "偏强信号" : technical.score < 0 ? "偏弱信号" : "强弱平衡"}{" "}
                        {Math.abs(technical.score)} 项
                      </span>
                    )}
                  </div>
                  <p className="mt-2 text-sm leading-relaxed">{technical.summary}</p>
                  <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-white/70">
                    <div
                      className={`h-full rounded-full ${TREND_META[technical.trend].bar}`}
                      style={{ width: `${technical.sufficient ? Math.max(12, Math.abs(technical.score) * 20) : 0}%` }}
                    />
                  </div>
                </div>

                {technical.signals.length > 0 && (
                  <div>
                    <h3 className="text-xs font-semibold text-slate-700">为什么这样判断</h3>
                    <ul className="mt-2 grid gap-2 sm:grid-cols-2">
                      {technical.signals.slice(0, 4).map((item) => (
                        <li key={item} className="rounded-lg bg-slate-50 px-3 py-2 text-sm leading-relaxed text-slate-700">
                          {item}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <div>
                  <h3 className="text-xs font-semibold text-slate-700">需要留意</h3>
                  {technical.risks.length > 0 ? (
                    <ul className="mt-2 space-y-2">
                      {technical.risks.map((item) => (
                        <li key={item} className="rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                          {item}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-2 rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600">
                      暂未发现明显的短期过热或高波动提示，但趋势仍可能随市场变化。
                    </p>
                  )}
                </div>

                <details className="rounded-lg border border-slate-200">
                  <summary className="cursor-pointer px-3 py-2.5 text-xs font-medium text-slate-600">
                    查看专业指标（可选）
                  </summary>
                  <div className="grid grid-cols-2 gap-2 border-t border-slate-100 p-3 text-xs sm:grid-cols-4">
                    {[
                      ["5 日均价", technical.indicators.ma5],
                      ["20 日均价", technical.indicators.ma20],
                      ["RSI 强弱值", technical.indicators.rsi12],
                      ["近期波动幅度", technical.indicators.atr_pct, "percent"],
                      ["20 日支撑参考", technical.indicators.support20],
                      ["20 日压力参考", technical.indicators.resistance20],
                      ["MACD 动力差", technical.indicators.macd_histogram],
                      ["近期量能倍数", technical.indicators.volume_ratio],
                    ].map(([label, value, kind]) => (
                      <div key={String(label)} className="rounded-md bg-slate-50 px-2.5 py-2">
                        <p className="text-slate-500">{label}</p>
                        <p className="mt-0.5 font-semibold tabular-nums text-slate-800">
                          {kind === "percent" && typeof value === "number"
                            ? fmtPercent(value)
                            : indicatorValue(typeof value === "number" ? value : null)}
                        </p>
                      </div>
                    ))}
                  </div>
                  <p className="border-t border-slate-100 px-3 py-2 text-xs leading-relaxed text-slate-400">
                    {technical.methodology}
                  </p>
                </details>
              </div>
            ) : (
              <EmptyState title="暂无趋势解读" hint="需要至少 30 条有效日线数据才能判断。" />
            )}
          </Card>

          {/* 财务估值 */}
          <Card className="px-4 py-5 sm:px-5">
            <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-sm font-semibold text-slate-800">财务与估值</h2>
              {displayFinancials?.reportDate && (
                <p className="text-xs text-slate-400">报告期 {fmtDate(displayFinancials.reportDate)}</p>
              )}
            </div>
            {displayFinancials && displayFinancials.metrics.length > 0 ? (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
                {displayFinancials.metrics.map((m) => (
                  <div key={m.key} className="rounded-lg bg-slate-50 px-3 py-2.5">
                    <p className="text-xs text-slate-500">{m.label}</p>
                    <p className="mt-0.5 text-sm font-semibold tabular-nums">
                      <FinancialMetricValue value={m.value} format={m.format} />
                    </p>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                title="暂无财务估值数据"
                hint="GET /api/stocks/{code}/financials 未返回数据；接口上线后此处展示 PE/PB/ROE 等指标。"
              />
            )}
          </Card>

          {/* 研究因子 */}
          <Card className="px-4 py-5 sm:px-5">
            <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-sm font-semibold text-slate-800">研究因子</h2>
              {factors?.asOf && <p className="text-xs text-slate-400">因子日期 {fmtDate(factors.asOf)}</p>}
            </div>
            {factorRow ? (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                {(
                  [
                    ["综合分", factorRow.compositeScore, "number"],
                    ["横截面排名", factorRow.rank, "rank"],
                    ["分位数", factorRow.percentile, "pct100"],
                    ["动量", factorRow.momentum, "percent"],
                    ["价值", factorRow.value, "number"],
                    ["质量", factorRow.quality, "number"],
                    ["成长", factorRow.growth, "number"],
                    ["波动率", factorRow.volatility, "number"],
                    ["市值", factorRow.size, "number"],
                    ["20 日收益", factorRow.return20d, "percent"],
                    ["60 日收益", factorRow.return60d, "percent"],
                  ] as [string, number | null, "number" | "percent" | "rank" | "pct100"][]
                ).map(([label, value, kind]) => (
                  <div key={label} className="rounded-lg bg-slate-50 px-3 py-2.5">
                    <p className="text-xs text-slate-500">{label}</p>
                    <p
                      className={`mt-0.5 text-sm font-semibold tabular-nums ${
                        kind === "percent" ? signClass(value) : "text-slate-800"
                      }`}
                    >
                      {value === null
                        ? "—"
                        : kind === "percent"
                          ? fmtPercent(value)
                          : kind === "rank"
                            ? `#${value}`
                            : kind === "pct100"
                              ? `${value.toFixed(0)}%`
                              : value.toFixed(2)}
                    </p>
                  </div>
                ))}
                {factorRow.extraFactors.slice(0, 8).map((f) => (
                  <div key={f.label} className="rounded-lg bg-slate-50 px-3 py-2.5">
                    <p className="text-xs text-slate-500">{f.label}</p>
                    <p className="mt-0.5 text-sm font-semibold tabular-nums text-slate-800">
                      {f.value === null ? "—" : f.value.toFixed(2)}
                    </p>
                  </div>
                ))}
              </div>
            ) : detailFactors.length > 0 ? (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                {detailFactors.slice(0, 12).map((f) => (
                  <div key={f.label} className="rounded-lg bg-slate-50 px-3 py-2.5">
                    <p className="text-xs text-slate-500">{f.label}</p>
                    <p className="mt-0.5 text-sm font-semibold tabular-nums text-slate-800">
                      {f.value === null ? "—" : f.value.toFixed(2)}
                    </p>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                title="暂无因子数据"
                hint="GET /api/stocks/research/factors 未返回该股票的因子；接口上线后此处展示动量/价值/质量等因子。"
              />
            )}
          </Card>

          {/* 研究信号 */}
          <Card className="px-4 py-5 sm:px-5">
            <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-sm font-semibold text-slate-800">研究信号</h2>
              {signals?.asOf && <p className="text-xs text-slate-400">信号日期 {fmtDate(signals.asOf)}</p>}
            </div>
            {signals && signals.items.length > 0 ? (
              <ul className="space-y-3">
                {signals.items.map((s) => (
                  <li
                    key={s.key}
                    className={`rounded-xl border border-slate-200 border-l-4 bg-white p-4 ${
                      s.direction === "long"
                        ? "border-l-rose-500"
                        : s.direction === "short"
                          ? "border-l-emerald-500"
                          : "border-l-slate-300"
                    }`}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        className={`rounded-full border px-2 py-0.5 text-xs font-medium ${
                          s.direction === "long"
                            ? "border-rose-200 bg-rose-50 text-rose-700"
                            : s.direction === "short"
                              ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                              : "border-slate-200 bg-slate-50 text-slate-600"
                        }`}
                      >
                        {s.direction === "long" ? "偏多" : s.direction === "short" ? "偏空" : "中性"}
                      </span>
                      <span className="text-sm font-semibold text-slate-900">{s.signal}</span>
                      {s.strength !== null && (
                        <span className="text-xs tabular-nums text-slate-500">
                          强度 {s.strength.toFixed(2)}
                        </span>
                      )}
                      {s.tier !== null && (
                        <span className="text-xs tabular-nums text-slate-500">{s.tier} 档</span>
                      )}
                      {s.asOf && <span className="ml-auto text-xs text-slate-400">{fmtDate(s.asOf)}</span>}
                    </div>
                    {s.reason && <p className="mt-1.5 text-xs leading-relaxed text-slate-500">{s.reason}</p>}
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState
                title="暂无信号数据"
                hint="GET /api/stocks/research/signals 未返回该股票的信号；接口上线后此处展示方向与理由。"
              />
            )}
          </Card>

          <p className="text-xs leading-relaxed text-slate-400">
            个股研究数据仅供研究参考，不构成投资建议。available_at 表示该数据在系统中的可获得时间，
            用于 point-in-time 研究口径。
          </p>
        </div>
      )}
    </>
  );
}
