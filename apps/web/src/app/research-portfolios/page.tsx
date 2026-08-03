"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, ApiError } from "@/lib/api";
import {
  fmtDate,
  fmtPercent,
  normalizeResearchPortfolios,
  normalizeStockFactors,
  signClass,
  type ResearchPortfoliosView,
  type StockFactorRowView,
} from "@/lib/normalize";
import { Card, EmptyState, PageHeader, Spinner } from "@/components/ui";
import { WatchlistButton } from "@/components/WatchlistButton";
import { FundLink } from "@/components/FundLink";

/** 接口缺失提示块（优雅降级：结构性展示，不报错） */
function MissingApiNotice({ label, endpoints }: { label: string; endpoints: string }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-slate-50 px-4 py-6 text-center">
      <p className="text-sm font-medium text-slate-700">{label} 接口尚未上线</p>
      <p className="mt-1 text-xs text-slate-500">
        依赖后端 {endpoints}。接口就绪后此处自动展示数据，无需改动前端。
      </p>
    </div>
  );
}

/** 基金组合由统一研究组合接口返回，明细回到基金发现的“生成组合”步骤。 */
function FundDiscoveryLink() {
  return (
    <div className="rounded-xl border border-blue-100 bg-blue-50/60 px-4 py-4 sm:px-5">
      <p className="text-sm font-medium text-slate-800">基金组合已统一到研究组合快照</p>
      <p className="mt-1 text-xs leading-relaxed text-slate-500">
        本页不再重复请求和绘制一套候选池信号；组合来源、目标权重与数据提示由统一接口返回。
      </p>
      <Link
        href="/discovery?stage=portfolio"
        className="mt-3 inline-flex rounded-lg bg-slate-900 px-3.5 py-2 text-xs font-medium text-white hover:bg-slate-700"
      >
        查看基金发现组合明细 →
      </Link>
    </div>
  );
}

/** 股票组合：研究组合接口（缺失时回退展示因子 Top N） */
function StockPortfolioSection() {
  const [portfolios, setPortfolios] = useState<ResearchPortfoliosView | null>(null);
  const [fallback, setFallback] = useState<StockFactorRowView[]>([]);
  const [loading, setLoading] = useState(true);
  const [missing, setMissing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const raw = await api.researchPortfolios();
      setPortfolios(normalizeResearchPortfolios(raw as never));
      setMissing(false);
      setLoading(false);
      return;
    } catch {
      /* 接口缺失，进入兜底 */
    }
    // 兜底：用因子横截面 Top 15 作为“研究组合候选”展示（接口可能同样缺失）
    try {
      const fraw = await api.stockFactors({ limit: 500 });
      const view = normalizeStockFactors(fraw as never);
      const top = [...view.items]
        .sort((a, b) => (b.compositeScore ?? Number.NEGATIVE_INFINITY) - (a.compositeScore ?? Number.NEGATIVE_INFINITY))
        .slice(0, 15);
      setFallback(top);
    } catch {
      setFallback([]);
    }
    setMissing(true);
    setLoading(false);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return <Spinner label="正在加载股票研究组合…" />;
  }

  if (!missing && portfolios && portfolios.portfolios.length > 0) {
    return (
      <div className="space-y-5">
        {portfolios.portfolios.map((p) => (
          <div key={p.key} className="rounded-xl border border-slate-200 bg-white">
            <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-100 px-4 py-3.5 sm:px-5">
              <div>
                <h3 className="text-sm font-semibold text-slate-800">{p.name}</h3>
                {p.description && <p className="mt-0.5 text-xs text-slate-500">{p.description}</p>}
              </div>
              <div className="text-right text-xs text-slate-400">
                {p.kind && <span className="mr-2 rounded bg-slate-100 px-1.5 py-0.5">{p.kind}</span>}
                {p.asOf && <span>截至 {fmtDate(p.asOf)}</span>}
              </div>
            </div>
            {p.holdings.length === 0 ? (
              <EmptyState title="组合暂无持仓" hint="接口已返回组合，但持仓列表为空。" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-sm">
                  <thead>
                    <tr className="bg-slate-50 text-left text-xs text-slate-500">
                      <th className="px-4 py-2.5 font-medium sm:px-5">标的</th>
                      <th className="px-4 py-2.5 font-medium">行业</th>
                      <th className="px-4 py-2.5 text-right font-medium">权重</th>
                      <th className="px-4 py-2.5 text-right font-medium">评分</th>
                      <th className="px-4 py-2.5 font-medium">理由</th>
                      <th className="px-4 py-2.5 text-right font-medium sm:px-5">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {p.holdings.map((h) => {
                      const isFund = p.kind.trim().toLowerCase() === "fund";
                      return (
                      <tr key={h.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                        <td className="px-4 py-3 sm:px-5">
                          {isFund ? (
                            <FundLink code={h.code} name={h.name} />
                          ) : (
                            <Link
                              href={`/stocks/${encodeURIComponent(h.code)}`}
                              className="font-medium text-slate-800 hover:text-blue-700"
                            >
                              {h.name}
                            </Link>
                          )}
                          <p className="text-xs text-slate-400">{h.code}</p>
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-600">{h.industry}</td>
                        <td className="px-4 py-3 text-right font-semibold tabular-nums text-slate-800">
                          {h.weight === null ? "—" : fmtPercent(h.weight)}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                          {h.score === null ? "—" : h.score.toFixed(2)}
                        </td>
                        <td className="max-w-[240px] px-4 py-3 text-xs text-slate-500">{h.reason || "—"}</td>
                        <td className="px-4 py-3 text-right sm:px-5">
                          <WatchlistButton
                            kind={isFund ? "fund" : "stock"}
                            code={h.code}
                            name={h.name}
                            size="sm"
                          />
                        </td>
                      </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {p.methodology && (
              <p className="border-t border-slate-100 px-4 py-3 text-xs leading-relaxed text-slate-400 sm:px-5">
                方法说明：{p.methodology}
              </p>
            )}
          </div>
        ))}
      </div>
    );
  }

  // 接口缺失：优雅降级为因子 Top N 候选
  return (
    <div className="space-y-4">
      <MissingApiNotice label="股票研究组合" endpoints="GET /api/research/portfolios" />
      {fallback.length > 0 && (
        <div>
          <p className="mb-2 text-xs text-slate-500">
            兜底展示：研究因子综合分 Top {fallback.length}（来自 GET /api/stocks/research/factors）
          </p>
          <div className="overflow-x-auto rounded-lg border border-slate-100">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="bg-slate-50 text-left text-xs text-slate-500">
                  <th className="px-4 py-2.5 font-medium sm:px-5">股票</th>
                  <th className="px-4 py-2.5 font-medium">行业</th>
                  <th className="px-4 py-2.5 text-right font-medium">综合分</th>
                  <th className="px-4 py-2.5 text-right font-medium">动量</th>
                  <th className="px-4 py-2.5 text-right font-medium">20 日收益</th>
                  <th className="px-4 py-2.5 text-right font-medium sm:px-5">操作</th>
                </tr>
              </thead>
              <tbody>
                {fallback.map((f) => (
                  <tr key={f.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                    <td className="px-4 py-3 sm:px-5">
                      <Link
                        href={`/stocks/${encodeURIComponent(f.code)}`}
                        className="font-medium text-slate-800 hover:text-blue-700"
                      >
                        {f.name}
                      </Link>
                      <p className="text-xs text-slate-400">{f.code}</p>
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-600">{f.industry}</td>
                    <td className="px-4 py-3 text-right font-semibold tabular-nums text-slate-800">
                      {f.compositeScore === null ? "—" : f.compositeScore.toFixed(2)}
                    </td>
                    <td className={`px-4 py-3 text-right tabular-nums ${signClass(f.momentum)}`}>
                      {f.momentum === null ? "—" : fmtPercent(f.momentum)}
                    </td>
                    <td className={`px-4 py-3 text-right tabular-nums ${signClass(f.return20d)}`}>
                      {f.return20d === null ? "—" : fmtPercent(f.return20d)}
                    </td>
                    <td className="px-4 py-3 text-right sm:px-5">
                      <WatchlistButton kind="stock" code={f.code} name={f.name} size="sm" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

export default function ResearchPortfoliosPage() {
  return (
    <>
      <PageHeader
        title="研究组合"
        description="基金发现候选池的当期入选组合，以及股票研究组合；接口缺失的板块优雅降级"
      />

      <Card className="mb-6 px-4 py-5 sm:px-5">
        <div className="mb-4">
          <h2 className="text-sm font-semibold text-slate-800">基金发现</h2>
          <p className="mt-0.5 text-xs text-slate-400">
            GET /api/discovery/pools · /api/discovery-quant/pools/{"{id}"}/signals
          </p>
        </div>
        <FundDiscoveryLink />
      </Card>

      <Card className="px-4 py-5 sm:px-5">
        <div className="mb-4">
          <h2 className="text-sm font-semibold text-slate-800">股票组合</h2>
          <p className="mt-0.5 text-xs text-slate-400">
            GET /api/research/portfolios（缺失时回退展示因子 Top N）
          </p>
        </div>
        <StockPortfolioSection />
      </Card>

      <p className="mt-4 text-xs leading-relaxed text-slate-400">
        研究组合仅为研究信号与历史分析，不构成投资建议，也不会产生任何实盘下单。
      </p>
    </>
  );
}
