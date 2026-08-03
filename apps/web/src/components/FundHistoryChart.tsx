"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { FundNavHistoryItem, FundTradePoint } from "@/lib/types";
import { fmtDate, fmtMoney, toNumber } from "@/lib/normalize";
import { FundLink } from "@/components/FundLink";

type RangeKey = "3m" | "6m" | "1y" | "all";

const RANGE_DAYS: Record<RangeKey, number> = {
  "3m": 92,
  "6m": 183,
  "1y": 365,
  all: 3650,
};

interface ChartPoint {
  date: string;
  nav: number;
}

interface TradeMarker {
  date: string;
  type: "buy" | "sell";
  amount: number;
  x: number;
  y: number;
}

export function FundHistoryChart({ fundCode, fundName }: { fundCode: string; fundName: string }) {
  const [items, setItems] = useState<FundNavHistoryItem[]>([]);
  const [trades, setTrades] = useState<FundTradePoint[]>([]);
  const [range, setRange] = useState<RangeKey>("1y");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    api
      .fundNavHistory(fundCode)
      .then((response) => {
        if (!active) return;
        setItems(Array.isArray(response.items) ? response.items : []);
        setTrades(Array.isArray(response.trades) ? response.trades : []);
      })
      .catch(() => {
        if (!active) return;
        setError("历史净值数据暂未同步或接口不可用");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [fundCode]);

  const points = useMemo<ChartPoint[]>(() => {
    const parsed = items
      .map((item) => ({
        date: item.nav_date ?? "",
        nav: toNumber(item.unit_nav) ?? Number.NaN,
      }))
      .filter((item) => item.date && Number.isFinite(item.nav))
      .sort((a, b) => a.date.localeCompare(b.date));
    if (range === "all") return parsed;
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - RANGE_DAYS[range]);
    const cutoffText = cutoff.toISOString().slice(0, 10);
    return parsed.filter((item) => item.date >= cutoffText);
  }, [items, range]);

  const chart = useMemo(() => buildChart(points), [points]);
  const latest = points.at(-1);
  const first = points[0];
  const changeRate = first && latest ? (latest.nav / first.nav - 1) * 100 : null;
  const percentLabels = chart
    ? chart.yValues.map((value) => (first ? ((value / first.nav - 1) * 100) : 0))
    : [];

  const markers = useMemo<TradeMarker[]>(() => {
    if (!chart || points.length < 2) return [];
    const start = points[0].date;
    const end = points[points.length - 1].date;
    return trades
      .map((trade) => {
        const date = trade.trade_date ?? "";
        const type = trade.type === "buy" ? "buy" : trade.type === "sell" ? "sell" : null;
        const amount = toNumber(trade.amount) ?? 0;
        if (!date || !type || amount <= 0 || date < start || date > end) return null;
        return {
          date,
          type,
          amount,
          x: chart.xForDate(date),
          y: chart.yForDate(date),
        } satisfies TradeMarker;
      })
      .filter((item): item is TradeMarker => item !== null);
  }, [chart, points, trades]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <FundLink
            code={fundCode}
            name={fundName}
            className="text-sm font-semibold text-slate-800 hover:text-blue-700 hover:underline"
          />
          <p className="text-xs text-slate-400">{fundCode} · 单位净值走势</p>
        </div>
        <div className="flex rounded-lg bg-slate-100 p-1 text-xs">
          {([
            ["3m", "近3月"],
            ["6m", "近6月"],
            ["1y", "近1年"],
            ["all", "全部"],
          ] as [RangeKey, string][]).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setRange(key)}
              className={`rounded-md px-2.5 py-1 ${range === key ? "bg-white text-slate-900 shadow-sm" : "text-slate-500"}`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <p className="py-10 text-center text-sm text-slate-500">正在加载历史走势…</p>
      ) : error ? (
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">{error}</p>
      ) : points.length < 2 || chart === null ? (
        <p className="py-10 text-center text-sm text-slate-500">暂无可绘制历史净值数据</p>
      ) : (
        <>
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <p className="text-xs text-slate-500">最新净值（{fmtDate(latest?.date)}）</p>
              <p className="text-2xl font-semibold tabular-nums text-slate-900">{fmtMoney(latest?.nav, 4)}</p>
            </div>
            <div className="flex items-center gap-4 text-xs text-slate-500">
              <span className="flex items-center gap-1">
                <span className="h-2.5 w-2.5 rounded-full bg-rose-600" /> 买入
              </span>
              <span className="flex items-center gap-1">
                <span className="h-2.5 w-2.5 rounded-full bg-emerald-600" /> 卖出
              </span>
              <p className={`text-sm font-medium ${changeRate !== null && changeRate < 0 ? "text-emerald-600" : "text-rose-600"}`}>
                {changeRate !== null ? `${changeRate >= 0 ? "+" : ""}${changeRate.toFixed(2)}%` : "—"}
              </p>
            </div>
          </div>
          <svg viewBox="0 0 640 220" className="h-56 w-full overflow-visible">
            <defs>
              <linearGradient id={`navFill-${fundCode}`} x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stopColor="#2563eb" stopOpacity="0.22" />
                <stop offset="100%" stopColor="#2563eb" stopOpacity="0.02" />
              </linearGradient>
            </defs>
            {[0, 1, 2, 3].map((index) => {
              const y = 20 + index * 55;
              return <line key={index} x1="35" x2="585" y1={y} y2={y} stroke="#e2e8f0" strokeDasharray="4" />;
            })}
            <path d={chart.areaPath} fill={`url(#navFill-${fundCode})`} />
            <path d={chart.linePath} fill="none" stroke="#2563eb" strokeWidth="2.5" strokeLinecap="round" />
            {markers.map((marker, index) => (
              <g key={`${marker.date}-${marker.type}-${index}`}>
                <circle
                  cx={marker.x}
                  cy={marker.y}
                  r={marker.type === "buy" ? 4 : 4.5}
                  fill={marker.type === "buy" ? "#dc2626" : "#059669"}
                  stroke="#fff"
                  strokeWidth="1.5"
                />
                <title>{`${marker.type === "buy" ? "买入" : "卖出"} ${fmtDate(marker.date)} ¥${fmtMoney(marker.amount)}`}</title>
              </g>
            ))}
            {chart.yLabels.map((label, index) => (
              <text key={index} x="0" y={23 + index * 55} fontSize="10" fill="#64748b">
                {label}
              </text>
            ))}
            {percentLabels.map((value, index) => (
              <text key={`pct-${index}`} x="592" y={23 + index * 55} fontSize="10" fill={value < 0 ? "#059669" : "#dc2626"}>
                {value >= 0 ? "+" : ""}{value.toFixed(1)}%
              </text>
            ))}
            <text x="35" y="214" fontSize="10" fill="#64748b">{fmtDate(first?.date)}</text>
            <text x="530" y="214" fontSize="10" fill="#64748b">{fmtDate(latest?.date)}</text>
          </svg>
        </>
      )}
    </div>
  );
}

function buildChart(points: ChartPoint[]) {
  if (points.length < 2) return null;
  const values = points.map((point) => point.nav);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || Math.max(Math.abs(max) * 0.01, 0.0001);
  const width = 550;
  const height = 160;
  const left = 35;
  const top = 20;
  const start = points[0].date;
  const end = points[points.length - 1].date;
  const startTime = new Date(start).getTime();
  const endTime = new Date(end).getTime();
  const dateSpan = Math.max(endTime - startTime, 1);

  const xForDate = (date: string) => {
    const time = new Date(date).getTime();
    return left + ((time - startTime) / dateSpan) * width;
  };
  const yForValue = (value: number) => top + (1 - (value - min) / span) * height;
  const yForDate = (date: string) => {
    const exact = points.find((point) => point.date === date);
    if (exact) return yForValue(exact.nav);
    let nearest = points[0];
    let nearestDistance = Math.abs(new Date(points[0].date).getTime() - new Date(date).getTime());
    for (const point of points) {
      const distance = Math.abs(new Date(point.date).getTime() - new Date(date).getTime());
      if (distance < nearestDistance) {
        nearest = point;
        nearestDistance = distance;
      }
    }
    return yForValue(nearest.nav);
  };

  const coords = points.map((point) => [xForDate(point.date), yForValue(point.nav)] as const);
  const linePath = coords.map(([x, y], index) => `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const areaPath = `${linePath} L${left + width},180 L${left},180 Z`;
  const yValues = [0, 1, 2, 3].map((index) => max - (span * index) / 3);
  const yLabels = yValues.map((value) => value.toFixed(3));
  return { linePath, areaPath, yLabels, yValues, xForDate, yForDate };
}
