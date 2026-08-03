"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, ApiError } from "@/lib/api";
import type { Transaction } from "@/lib/types";
import {
  fmtDate,
  fmtMoney,
  fmtShares,
  normalizeTransactions,
  signClass,
  toNumber,
  type TransactionView,
} from "@/lib/normalize";
import { Card, EmptyState, ErrorState, PageHeader, SnapshotNotice, Spinner } from "@/components/ui";
import { FundLink } from "@/components/FundLink";

type FlowType = "buy" | "sell" | "dividend" | "other";

interface SummaryItem {
  key: FlowType;
  label: string;
  amount: number;
  count: number;
  color: string;
}

interface MonthlyPoint {
  month: string;
  buy: number;
  sell: number;
  dividend: number;
  other: number;
  net: number;
}

interface FundAggregate {
  code: string;
  name: string;
  amount: number;
  count: number;
}

function classify(type: string): FlowType {
  if (type === "buy" || type.includes("买")) return "buy";
  if (type === "sell" || type.includes("卖") || type.includes("赎")) return "sell";
  if (type === "dividend" || type.includes("分红")) return "dividend";
  return "other";
}

export default function TransactionsPage() {
  const [transactions, setTransactions] = useState<TransactionView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FlowType | "all">("all");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const raw = await api.transactions();
      setTransactions(normalizeTransactions(raw as Transaction[]));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "网络请求失败，请确认后端服务已启动");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const summary = useMemo<SummaryItem[]>(() => {
    const base: SummaryItem[] = [
      { key: "buy", label: "买入", amount: 0, count: 0, color: "#dc2626" },
      { key: "sell", label: "卖出", amount: 0, count: 0, color: "#059669" },
      { key: "dividend", label: "分红", amount: 0, count: 0, color: "#0284c7" },
      { key: "other", label: "其他", amount: 0, count: 0, color: "#64748b" },
    ];
    const map = new Map(base.map((item) => [item.key, item]));
    transactions.forEach((transaction) => {
      const key = classify(transaction.type);
      const item = map.get(key);
      if (!item) return;
      item.amount += toNumber(transaction.amount) ?? 0;
      item.count += 1;
    });
    return base;
  }, [transactions]);

  const monthly = useMemo<MonthlyPoint[]>(() => {
    const map = new Map<string, MonthlyPoint>();
    transactions.forEach((transaction) => {
      const date = typeof transaction.date === "string" ? transaction.date.slice(0, 7) : "";
      if (!date) return;
      const type = classify(transaction.type);
      const amount = toNumber(transaction.amount) ?? 0;
      const item = map.get(date) ?? { month: date, buy: 0, sell: 0, dividend: 0, other: 0, net: 0 };
      item[type] += amount;
      item.net += type === "buy" ? -amount : amount;
      map.set(date, item);
    });
    return [...map.values()].sort((a, b) => a.month.localeCompare(b.month)).slice(-12);
  }, [transactions]);

  const topFunds = useMemo<FundAggregate[]>(() => {
    const map = new Map<string, FundAggregate>();
    transactions.forEach((transaction) => {
      const key = transaction.code;
      const item = map.get(key) ?? { code: transaction.code, name: transaction.name, amount: 0, count: 0 };
      item.amount += toNumber(transaction.amount) ?? 0;
      item.count += 1;
      map.set(key, item);
    });
    return [...map.values()].sort((a, b) => b.amount - a.amount).slice(0, 8);
  }, [transactions]);

  const filtered = useMemo(
    () => (filter === "all" ? transactions : transactions.filter((t) => classify(t.type) === filter)),
    [transactions, filter]
  );

  const maxFlow = Math.max(1, ...monthly.flatMap((item) => [item.buy, item.sell, item.dividend, item.other]));
  const maxFundAmount = Math.max(1, ...topFunds.map((item) => item.amount));

  return (
    <>
      <PageHeader
        title="交易分析"
        description={
          transactions.length > 0
            ? `共 ${transactions.length} 笔交易，按类型、月份和基金整理`
            : "买入、卖出、分红和转换记录分析"
        }
      />
      <SnapshotNotice />

      {loading ? (
        <Card><Spinner label="正在整理交易数据…" /></Card>
      ) : error ? (
        <Card><ErrorState message={error} onRetry={load} /></Card>
      ) : transactions.length === 0 ? (
        <Card>
          <EmptyState
            title="暂无交易记录"
            hint="数据库中还没有交易数据。"
            action={<Link href="/positions" className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700">查看持仓</Link>}
          />
        </Card>
      ) : (
        <div className="space-y-6">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {summary.map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => setFilter(filter === item.key ? "all" : item.key)}
                className={`rounded-xl border bg-white px-4 py-4 text-left shadow-sm transition ${filter === item.key ? "border-slate-900" : "border-slate-200 hover:border-slate-300"}`}
              >
                <div className="flex items-center justify-between">
                  <p className="text-sm font-medium text-slate-600">{item.label}</p>
                  <span className="h-2.5 w-2.5 rounded-full" style={{ background: item.color }} />
                </div>
                <p className="mt-2 text-xl font-semibold tabular-nums text-slate-900">¥{fmtMoney(item.amount)}</p>
                <p className="mt-1 text-xs text-slate-400">{item.count} 笔</p>
              </button>
            ))}
          </div>

          <div className="grid grid-cols-1 gap-6 xl:grid-cols-5">
            <Card className="px-4 py-5 sm:px-5 xl:col-span-3">
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-sm font-semibold text-slate-800">近 12 个月资金流向</h2>
                <div className="flex gap-3 text-xs text-slate-500">
                  <span className="flex items-center gap-1"><i className="h-2 w-2 rounded-full bg-rose-600" />买入</span>
                  <span className="flex items-center gap-1"><i className="h-2 w-2 rounded-full bg-emerald-600" />卖出</span>
                  <span className="flex items-center gap-1"><i className="h-2 w-2 rounded-full bg-sky-600" />分红</span>
                </div>
              </div>
              <svg viewBox="0 0 720 250" className="h-64 w-full">
                {[0, 1, 2, 3].map((index) => <line key={index} x1="45" x2="700" y1={35 + index * 50} y2={35 + index * 50} stroke="#e2e8f0" strokeDasharray="4" />)}
                {monthly.map((item, index) => {
                  const x = 65 + index * 52;
                  const buyHeight = (item.buy / maxFlow) * 145;
                  const sellHeight = (item.sell / maxFlow) * 145;
                  const dividendHeight = ((item.dividend + item.other) / maxFlow) * 145;
                  return (
                    <g key={item.month}>
                      <rect x={x} y={185 - buyHeight} width="12" height={buyHeight} rx="2" fill="#dc2626" />
                      <rect x={x + 14} y={185 - sellHeight} width="12" height={sellHeight} rx="2" fill="#059669" />
                      <rect x={x + 28} y={185 - dividendHeight} width="12" height={dividendHeight} rx="2" fill="#0284c7" />
                      <text x={x + 2} y="225" fontSize="9" fill="#64748b" transform={`rotate(-35 ${x + 2} 225)`}>{item.month.slice(2)}</text>
                      <title>{`${item.month}\n买入 ¥${fmtMoney(item.buy)}\n卖出 ¥${fmtMoney(item.sell)}\n分红/其他 ¥${fmtMoney(item.dividend + item.other)}\n净投入 ¥${fmtMoney(-item.net)}`}</title>
                    </g>
                  );
                })}
              </svg>
            </Card>

            <Card className="px-4 py-5 sm:px-5 xl:col-span-2">
              <h2 className="mb-4 text-sm font-semibold text-slate-800">交易金额最高基金</h2>
              <div className="space-y-3">
                {topFunds.map((fund) => (
                  <div key={fund.code}>
                    <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                      <FundLink
                        code={fund.code}
                        name={fund.name}
                        className="truncate font-medium text-slate-700 hover:text-blue-700 hover:underline"
                      />
                      <span className="tabular-nums text-slate-500">¥{fmtMoney(fund.amount)}</span>
                    </div>
                    <div className="h-2 overflow-hidden rounded-full bg-slate-100">
                      <div className="h-full rounded-full bg-blue-600" style={{ width: `${(fund.amount / maxFundAmount) * 100}%` }} />
                    </div>
                    <p className="mt-1 text-xs text-slate-400">{fund.code} · {fund.count} 笔</p>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          <Card className="overflow-hidden">
            <div className="flex flex-wrap items-center gap-2 px-4 py-3 sm:px-5">
              <button type="button" onClick={() => setFilter("all")} className={`rounded-full px-3 py-1 text-xs font-medium ${filter === "all" ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}>全部</button>
              {summary.map((item) => (
                <button key={item.key} type="button" onClick={() => setFilter(filter === item.key ? "all" : item.key)} className={`rounded-full px-3 py-1 text-xs font-medium ${filter === item.key ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}>{item.label}</button>
              ))}
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-sm">
                <thead>
                  <tr className="border-t border-slate-100 bg-slate-50 text-left text-xs text-slate-500">
                    <th className="px-4 py-2.5 font-medium sm:px-5">日期</th>
                    <th className="px-4 py-2.5 font-medium">基金</th>
                    <th className="px-4 py-2.5 font-medium">类型</th>
                    <th className="px-4 py-2.5 text-right font-medium">金额（元）</th>
                    <th className="px-4 py-2.5 text-right font-medium">份额</th>
                    <th className="px-4 py-2.5 text-right font-medium">净值</th>
                    <th className="px-4 py-2.5 text-right font-medium">费用（元）</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.slice(0, 100).map((t) => {
                    const kind = classify(t.type);
                    return (
                      <tr key={t.key} className="border-t border-slate-100 hover:bg-slate-50/60">
                        <td className="whitespace-nowrap px-4 py-3 tabular-nums text-slate-600 sm:px-5">{fmtDate(t.date)}</td>
                        <td className="px-4 py-3"><FundLink code={t.code} name={t.name} className="font-medium text-slate-800 hover:text-blue-700 hover:underline" /><p className="text-xs text-slate-400">{t.code}</p></td>
                        <td className="px-4 py-3"><span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${kind === "buy" ? "bg-rose-50 text-rose-700" : kind === "sell" ? "bg-emerald-50 text-emerald-700" : kind === "dividend" ? "bg-sky-50 text-sky-700" : "bg-slate-100 text-slate-600"}`}>{t.type}</span></td>
                        <td className={`px-4 py-3 text-right tabular-nums font-medium ${signClass(kind === "buy" ? -(toNumber(t.amount) ?? 0) : toNumber(t.amount))}`}>{fmtMoney(t.amount)}</td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">{fmtShares(t.shares)}</td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">{fmtMoney(t.nav, 4)}</td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-600">{fmtMoney(t.fee)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {filtered.length > 100 && <p className="border-t border-slate-100 bg-slate-50 px-4 py-2 text-xs text-slate-500">当前筛选共 {filtered.length} 笔，下方列表仅显示最近 100 笔；上方图表已使用全部记录。</p>}
              {filtered.length === 0 && <EmptyState title="该类型下暂无记录" hint="切换筛选条件查看其它交易。" />}
            </div>
          </Card>
        </div>
      )}
    </>
  );
}
