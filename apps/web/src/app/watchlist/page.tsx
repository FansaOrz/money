"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import {
  fmtPercent,
  normalizeQuantFunds,
  normalizeStockFactors,
  signClass,
  toNumber,
} from "@/lib/normalize";
import {
  getWatchlist,
  removeFromWatchlist,
  subscribeWatchlist,
  type WatchlistItem,
} from "@/lib/watchlist";
import { Card, EmptyState, PageHeader } from "@/components/ui";
import { FundLink } from "@/components/FundLink";

interface FundEnrich {
  annualizedReturn: number | null;
  sharpe: number | null;
  maxDrawdown: number | null;
}

interface StockEnrich {
  compositeScore: number | null;
  industry: string | null;
  momentum: number | null;
}

/**
 * 自选页：基金 + 股票共存（localStorage）。
 * 基金行情/指标来自 /api/quant/funds（兜底展示），股票指标来自 /api/stocks/research/factors（可能缺失，优雅降级）。
 */
export default function WatchlistPage() {
  const [items, setItems] = useState<WatchlistItem[]>([]);
  const [fundEnrich, setFundEnrich] = useState<Map<string, FundEnrich>>(new Map());
  const [stockEnrich, setStockEnrich] = useState<Map<string, StockEnrich>>(new Map());
  const [stockApiMissing, setStockApiMissing] = useState(false);

  useEffect(() => {
    setItems(getWatchlist());
    return subscribeWatchlist(() => setItems(getWatchlist()));
  }, []);

  /* 基金指标兜底（复用已有量化接口） */
  useEffect(() => {
    let cancelled = false;
    api
      .quantFunds()
      .then((raw) => {
        if (cancelled) return;
        const views = normalizeQuantFunds(raw);
        const map = new Map<string, FundEnrich>();
        views.forEach((f) => {
          map.set(f.code, {
            annualizedReturn: toNumber(f.annualizedReturn),
            sharpe: toNumber(f.sharpeRatio),
            maxDrawdown: toNumber(f.maxDrawdown),
          });
        });
        setFundEnrich(map);
      })
      .catch(() => {
        /* 基金指标缺失静默 */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /* 股票因子兜底（接口可能未上线） */
  useEffect(() => {
    let cancelled = false;
    api
      .stockFactors({ limit: 500 })
      .then((raw) => {
        if (cancelled) return;
        const view = normalizeStockFactors(raw as never);
        const map = new Map<string, StockEnrich>();
        view.items.forEach((f) => {
          map.set(f.code, {
            compositeScore: f.compositeScore,
            industry: f.industry !== "—" ? f.industry : null,
            momentum: f.momentum,
          });
        });
        setStockEnrich(map);
      })
      .catch(() => {
        if (!cancelled) setStockApiMissing(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const funds = useMemo(() => items.filter((x) => x.kind === "fund"), [items]);
  const stocks = useMemo(() => items.filter((x) => x.kind === "stock"), [items]);

  const remove = useCallback((kind: WatchlistItem["kind"], code: string) => {
    setItems(removeFromWatchlist(kind, code));
  }, []);

  return (
    <>
      <PageHeader
        title="自选"
        description="本地收藏夹：基金与股票共存，保存在浏览器 localStorage，不上传服务器"
      />

      {items.length === 0 ? (
        <Card>
          <EmptyState
            title="自选为空"
            hint="在股票筛选页或个股详情页点击「加自选」，标的会出现在这里。"
            action={
              <Link
                href="/stock-screener"
                className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
              >
                去股票筛选
              </Link>
            }
          />
        </Card>
      ) : (
        <div className="space-y-6">
          {/* 自选股票 */}
          <Card className="overflow-hidden">
            <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-4 sm:px-5">
              <h2 className="text-sm font-semibold text-slate-800">自选股票（{stocks.length}）</h2>
              {stockApiMissing && stocks.length > 0 && (
                <p className="text-xs text-amber-700">股票指标接口暂不可用，仅展示代码与名称</p>
              )}
            </div>
            {stocks.length === 0 ? (
              <EmptyState title="暂无自选股票" hint="在股票筛选页或个股详情页加入自选。" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-sm">
                  <thead>
                    <tr className="border-t border-slate-100 bg-slate-50 text-left text-xs text-slate-500">
                      <th className="px-4 py-2.5 font-medium sm:px-5">股票</th>
                      <th className="px-4 py-2.5 font-medium">行业</th>
                      <th className="px-4 py-2.5 text-right font-medium">综合分</th>
                      <th className="px-4 py-2.5 text-right font-medium">动量</th>
                      <th className="px-4 py-2.5 font-medium">加入时间</th>
                      <th className="px-4 py-2.5 text-right font-medium sm:px-5">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {stocks.map((it) => {
                      const enrich = stockEnrich.get(it.code);
                      return (
                        <tr key={`s-${it.code}`} className="border-t border-slate-100 hover:bg-slate-50/60">
                          <td className="px-4 py-3 sm:px-5">
                            <Link
                              href={`/stocks/${encodeURIComponent(it.code)}`}
                              className="font-medium text-slate-800 hover:text-blue-700"
                            >
                              {it.name}
                            </Link>
                            <p className="text-xs text-slate-400">{it.code}</p>
                          </td>
                          <td className="px-4 py-3 text-xs text-slate-600">{enrich?.industry ?? "—"}</td>
                          <td className="px-4 py-3 text-right tabular-nums text-slate-800">
                            {enrich?.compositeScore === null || enrich?.compositeScore === undefined
                              ? "—"
                              : enrich.compositeScore.toFixed(2)}
                          </td>
                          <td className={`px-4 py-3 text-right tabular-nums ${signClass(enrich?.momentum)}`}>
                            {enrich?.momentum === null || enrich?.momentum === undefined
                              ? "—"
                              : fmtPercent(enrich.momentum)}
                          </td>
                          <td className="px-4 py-3 text-xs text-slate-500">
                            {new Date(it.addedAt).toLocaleDateString("zh-CN")}
                          </td>
                          <td className="px-4 py-3 text-right sm:px-5">
                            <div className="flex items-center justify-end gap-2">
                              <Link
                                href={`/stocks/${encodeURIComponent(it.code)}`}
                                className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50"
                              >
                                详情
                              </Link>
                              <button
                                type="button"
                                onClick={() => remove("stock", it.code)}
                                className="rounded-md border border-rose-200 px-2 py-1 text-xs font-medium text-rose-600 hover:bg-rose-50"
                              >
                                移除
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          {/* 自选基金 */}
          <Card className="overflow-hidden">
            <div className="px-4 py-4 sm:px-5">
              <h2 className="text-sm font-semibold text-slate-800">自选基金（{funds.length}）</h2>
              <p className="mt-0.5 text-xs text-slate-400">指标来自 GET /api/quant/funds（组合内基金）</p>
            </div>
            {funds.length === 0 ? (
              <EmptyState title="暂无自选基金" hint="基金详情与持仓页的「加自选」入口上线后可在此管理。" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-sm">
                  <thead>
                    <tr className="border-t border-slate-100 bg-slate-50 text-left text-xs text-slate-500">
                      <th className="px-4 py-2.5 font-medium sm:px-5">基金</th>
                      <th className="px-4 py-2.5 text-right font-medium">年化收益</th>
                      <th className="px-4 py-2.5 text-right font-medium">夏普</th>
                      <th className="px-4 py-2.5 text-right font-medium">最大回撤</th>
                      <th className="px-4 py-2.5 font-medium">加入时间</th>
                      <th className="px-4 py-2.5 text-right font-medium sm:px-5">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {funds.map((it) => {
                      const enrich = fundEnrich.get(it.code);
                      return (
                        <tr key={`f-${it.code}`} className="border-t border-slate-100 hover:bg-slate-50/60">
                          <td className="px-4 py-3 sm:px-5">
                            <FundLink code={it.code} name={it.name} />
                            <p className="text-xs text-slate-400">{it.code}</p>
                          </td>
                          <td className={`px-4 py-3 text-right tabular-nums ${signClass(enrich?.annualizedReturn)}`}>
                            {enrich?.annualizedReturn === null || enrich?.annualizedReturn === undefined
                              ? "—"
                              : fmtPercent(enrich.annualizedReturn)}
                          </td>
                          <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                            {enrich?.sharpe === null || enrich?.sharpe === undefined
                              ? "—"
                              : enrich.sharpe.toFixed(2)}
                          </td>
                          <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                            {enrich?.maxDrawdown === null || enrich?.maxDrawdown === undefined
                              ? "—"
                              : fmtPercent(enrich.maxDrawdown)}
                          </td>
                          <td className="px-4 py-3 text-xs text-slate-500">
                            {new Date(it.addedAt).toLocaleDateString("zh-CN")}
                          </td>
                          <td className="px-4 py-3 text-right sm:px-5">
                            <div className="flex items-center justify-end gap-2">
                              <Link
                                href={`/positions?fund=${encodeURIComponent(it.code)}`}
                                className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50"
                              >
                                持仓
                              </Link>
                              <button
                                type="button"
                                onClick={() => remove("fund", it.code)}
                                className="rounded-md border border-rose-200 px-2 py-1 text-xs font-medium text-rose-600 hover:bg-rose-50"
                              >
                                移除
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <p className="text-xs leading-relaxed text-slate-400">
            自选清单仅保存在当前浏览器（localStorage），清除站点数据会丢失；不构成投资建议。
          </p>
        </div>
      )}
    </>
  );
}
