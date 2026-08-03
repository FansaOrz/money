"use client";

import { useEffect, useMemo, useState } from "react";
import { fmtDate, fmtPercent, toNumber } from "@/lib/normalize";

interface IndexSummary {
  code?: string;
  name?: string;
  market?: string;
  close?: string | number | null;
  change_pct?: string | number | null;
  latest_date?: string | null;
}
interface IndexPoint { date?: string; close?: string | number | null }
type RangeKey = "1m" | "3m" | "6m" | "1y" | "all";
const RANGE_DAYS: Record<RangeKey, number> = { "1m": 30, "3m": 90, "6m": 180, "1y": 365, all: 3650 };

export function MarketTrends() {
  const [items, setItems] = useState<IndexSummary[]>([]);
  const [selected, setSelected] = useState("CSI300");
  const [range, setRange] = useState<RangeKey>("3m");
  const [points, setPoints] = useState<IndexPoint[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/indices", { cache: "no-store" })
      .then((response) => response.json())
      .then((data) => {
        const next = Array.isArray(data) ? data : Array.isArray(data.items) ? data.items : [];
        setItems(next);
        if (next.length && !next.some((item: IndexSummary) => item.code === selected)) setSelected(next[0].code);
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selected) return;
    fetch(`/api/indices/${selected}/history?days=${RANGE_DAYS[range]}`, { cache: "no-store" })
      .then((response) => response.json())
      .then((data) => setPoints(Array.isArray(data) ? data : Array.isArray(data.items) ? data.items : []));
  }, [selected, range]);

  const chart = useMemo(() => {
    const parsed = points.map((point) => ({ date: point.date ?? "", close: toNumber(point.close) }))
      .filter((point): point is { date: string; close: number } => Boolean(point.date) && point.close !== null);
    if (parsed.length < 2) return null;
    const values = parsed.map((point) => point.close); const min = Math.min(...values); const max = Math.max(...values); const span = max - min || 1;
    const coords = parsed.map((point, index) => [55 + index / (parsed.length - 1) * 510, 20 + (1 - (point.close - min) / span) * 150] as const);
    const path = coords.map(([x,y], index) => `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    const start = parsed[0].close; const end = parsed.at(-1)!.close;
    const yValues = [0,1,2,3].map(i => max - span * i / 3);
    const percentages = yValues.map(value => start ? (value / start - 1) * 100 : 0);
    return { parsed, path, min, max, start, end, change: start ? end / start - 1 : null, yValues, percentages };
  }, [points]);

  if (loading) return <p className="py-8 text-center text-sm text-slate-500">正在加载主要市场趋势…</p>;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
        {items.map((item) => {
          const change = toNumber(item.change_pct);
          return <button key={item.code} type="button" onClick={() => setSelected(item.code ?? "")} className={`rounded-lg border px-3 py-3 text-left ${selected === item.code ? "border-blue-500 bg-blue-50" : "border-slate-200 bg-white"}`}>
            <p className="text-xs text-slate-500">{item.market}</p><p className="mt-1 text-sm font-semibold text-slate-800">{item.name}</p>
            <p className="mt-1 tabular-nums text-slate-700">{item.close ?? "—"}</p>
            <p className={`text-xs ${change !== null && change < 0 ? "text-emerald-600" : "text-rose-600"}`}>{change === null ? "—" : `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`}</p>
          </button>;
        })}
      </div>
      <div className="flex justify-end">
        <div className="inline-flex rounded-lg bg-slate-100 p-1 text-xs">
          {([['1m','近1月'],['3m','近3月'],['6m','近6月'],['1y','近1年'],['all','全部']] as [RangeKey,string][]).map(([key,label]) => (
            <button key={key} type="button" onClick={() => setRange(key)} className={`rounded-md px-2.5 py-1 ${range === key ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500'}`}>{label}</button>
          ))}
        </div>
      </div>
      {chart ? <>
        <div className="flex flex-wrap items-end justify-between gap-3 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-500">
          <span>区间 {fmtDate(chart.parsed[0].date)} 至 {fmtDate(chart.parsed.at(-1)?.date)} · {chart.parsed.length} 个交易日</span>
          <span>起点 {chart.start.toFixed(2)} · 终点 {chart.end.toFixed(2)} · 最高 {chart.max.toFixed(2)} · 最低 {chart.min.toFixed(2)}</span>
          <strong className={chart.change !== null && chart.change < 0 ? "text-emerald-600" : "text-rose-600"}>{chart.change === null ? "—" : `${chart.change >= 0 ? "+" : ""}${(chart.change*100).toFixed(2)}%`}</strong>
        </div>
        <svg viewBox="0 0 640 210" className="h-56 w-full">
          {[0,1,2,3].map(i => <g key={i}><line x1="55" x2="565" y1={20+i*50} y2={20+i*50} stroke="#e2e8f0" strokeDasharray="4"/><text x="0" y={24+i*50} fontSize="10" fill="#64748b">{chart.yValues[i].toFixed(2)}</text><text x="578" y={24+i*50} fontSize="10" fill={chart.percentages[i] < 0 ? "#059669" : "#dc2626"}>{chart.percentages[i] >= 0 ? "+" : ""}{chart.percentages[i].toFixed(1)}%</text></g>)}
          <path d={chart.path} fill="none" stroke="#2563eb" strokeWidth="2.5"/>
          <text x="55" y="202" fontSize="10" fill="#64748b">{fmtDate(chart.parsed[0].date)}</text>
          <text x="285" y="202" fontSize="10" fill="#64748b">{fmtDate(chart.parsed[Math.floor(chart.parsed.length/2)].date)}</text>
          <text x="495" y="202" fontSize="10" fill="#64748b">{fmtDate(chart.parsed.at(-1)?.date)}</text>
        </svg>
      </> : <p className="py-8 text-center text-sm text-slate-500">该指数暂无历史数据</p>}
      <p className="text-xs text-slate-400">指数数据来自 AKShare 聚合的公开行情；A股/港股北京时间 17:30 更新，美股次日 07:30 补充。</p>
    </div>
  );
}
