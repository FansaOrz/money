"use client";

import { useEffect, useState } from "react";
import { Card, EmptyState, ErrorState, PageHeader, Spinner } from "@/components/ui";
import { fmtMoney, fmtPercent, toNumber } from "@/lib/normalize";

interface ExposureItem {
  code: string;
  name: string;
  portfolio_weight: string | number;
  source_funds: number;
  report_date?: string | null;
}
interface ExposureResponse {
  stocks?: ExposureItem[];
  industries?: ExposureItem[];
  stocks_total?: number;
  industries_total?: number;
  covered_market_value?: string;
  total_market_value?: string;
  coverage_rate?: string;
}

// 展示条数选项；Infinity 表示“全部”
const STOCK_LIMIT_OPTIONS: Array<{ label: string; value: number }> = [
  { label: "前 50", value: 50 },
  { label: "前 100", value: 100 },
  { label: "前 200", value: 200 },
  { label: "全部", value: Infinity },
];
const INDUSTRY_LIMIT_OPTIONS: Array<{ label: string; value: number }> = [
  { label: "前 50", value: 50 },
  { label: "前 100", value: 100 },
  { label: "前 200", value: 200 },
  { label: "全部", value: Infinity },
];

// 一次性请求后端支持的最大条数，前端再按所选条数截断，避免重复请求
const FETCH_STOCK_LIMIT = 2000;
const FETCH_INDUSTRY_LIMIT = 500;

/**
 * 组合穿透面板：原 /exposure 页面内容。
 * hideHeader 用于嵌入 /positions?view=exposure 标签时隐藏自带 PageHeader。
 */
export function ExposurePanel({ hideHeader = false }: { hideHeader?: boolean }) {
  const [data, setData] = useState<ExposureResponse | null>(null);
  const [tab, setTab] = useState<"stocks" | "industries">("stocks");
  const [stockLimit, setStockLimit] = useState(50);
  const [industryLimit, setIndustryLimit] = useState(50);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/holdings/portfolio/exposure?stock_limit=${FETCH_STOCK_LIMIT}&industry_limit=${FETCH_INDUSTRY_LIMIT}`, { cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setError(String(e)));
  }, []);

  const isStocks = tab === "stocks";
  const allItems = (isStocks ? data?.stocks : data?.industries) ?? [];
  const total = (isStocks ? data?.stocks_total : data?.industries_total) ?? allItems.length;
  const limit = isStocks ? stockLimit : industryLimit;
  const setLimit = isStocks ? setStockLimit : setIndustryLimit;
  const limitOptions = isStocks ? STOCK_LIMIT_OPTIONS : INDUSTRY_LIMIT_OPTIONS;
  const items = allItems.slice(0, limit);
  const max = Math.max(0, ...items.map((x) => toNumber(x.portfolio_weight) ?? 0));

  return (
    <>
      {!hideHeader && (
        <PageHeader
          title="穿透持仓"
          description="按基金当前市值 × 最新季度披露持仓比例，估算组合底层股票和行业暴露"
        />
      )}
      {error ? (
        <Card>
          <ErrorState message={error} />
        </Card>
      ) : !data ? (
        <Card>
          <Spinner label="正在计算组合穿透持仓…" />
        </Card>
      ) : (
        <div className="space-y-5">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Card className="px-4 py-4">
              <p className="text-xs text-slate-500">组合总市值</p>
              <p className="mt-1 text-xl font-semibold">¥{fmtMoney(data.total_market_value)}</p>
            </Card>
            <Card className="px-4 py-4">
              <p className="text-xs text-slate-500">成分覆盖市值</p>
              <p className="mt-1 text-xl font-semibold">¥{fmtMoney(data.covered_market_value)}</p>
            </Card>
            <Card className="px-4 py-4">
              <p className="text-xs text-slate-500">披露数据覆盖率</p>
              <p className="mt-1 text-xl font-semibold">{fmtPercent(data.coverage_rate)}</p>
            </Card>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <div className="inline-flex rounded-lg bg-slate-200/70 p-1 text-sm">
              <button
                onClick={() => setTab("stocks")}
                className={`rounded-md px-4 py-2 ${tab === "stocks" ? "bg-white shadow-sm" : ""}`}
              >
                底层股票
              </button>
              <button
                onClick={() => setTab("industries")}
                className={`rounded-md px-4 py-2 ${tab === "industries" ? "bg-white shadow-sm" : ""}`}
              >
                行业分布
              </button>
            </div>
            <label className="flex items-center gap-2 text-sm text-slate-600">
              展示条数
              <select
                value={limit === Infinity ? "all" : String(limit)}
                onChange={(e) => setLimit(e.target.value === "all" ? Infinity : Number(e.target.value))}
                className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
              >
                {limitOptions.map((opt) => (
                  <option key={opt.label} value={opt.value === Infinity ? "all" : String(opt.value)}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </label>
            <span className="text-sm text-slate-500">
              已展示 {items.length} / 总计 {total}
            </span>
          </div>
          <Card className="overflow-hidden">
            {items.length === 0 ? (
              <EmptyState
                title="暂无穿透数据"
                hint="部分 QDII、债券和新基金不公开对应股票成分。"
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[700px] text-sm">
                  <thead>
                    <tr className="bg-slate-50 text-left text-xs text-slate-500">
                      <th className="px-5 py-3">排名</th>
                      <th className="px-4 py-3">{tab === "stocks" ? "股票" : "行业"}</th>
                      <th className="px-4 py-3 text-right">组合穿透占比</th>
                      <th className="px-4 py-3">暴露条</th>
                      <th className="px-4 py-3 text-right">来源基金数</th>
                      <th className="px-5 py-3">报告期</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((item, i) => {
                      const weight = toNumber(item.portfolio_weight) ?? 0;
                      return (
                        <tr key={`${item.code}-${i}`} className="border-t border-slate-100">
                          <td className="px-5 py-3 text-slate-400">{i + 1}</td>
                          <td className="px-4 py-3">
                            <p className="font-medium text-slate-800">{item.name}</p>
                            <p className="text-xs text-slate-400">{item.code}</p>
                          </td>
                          <td className="px-4 py-3 text-right font-medium tabular-nums">
                            {fmtPercent(weight)}
                          </td>
                          <td className="px-4 py-3">
                            <div className="h-2 w-44 overflow-hidden rounded-full bg-slate-100">
                              <div
                                className="h-full rounded-full bg-blue-600"
                                style={{ width: `${max ? (weight / max) * 100 : 0}%` }}
                              />
                            </div>
                          </td>
                          <td className="px-4 py-3 text-right tabular-nums">{item.source_funds}</td>
                          <td className="px-5 py-3 text-xs text-slate-500">
                            {item.report_date ?? "—"}
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
            这是基于基金最近一次公开季度报告的估算，不代表基金今天的实际持仓。未披露部分不会被推测或补造。
          </p>
        </div>
      )}
    </>
  );
}
