"use client";

import { useMemo } from "react";
import { fmtDate } from "@/lib/normalize";
import { EmptyState } from "@/components/ui";

export interface PriceChartPoint {
  date: string;
  close: number;
}

/**
 * 轻量 SVG 收盘价走势图：与回测曲线同风格，无第三方依赖。
 */
export function PriceChart({
  points,
  color = "#2563eb",
  gradientId = "priceFill",
  heightClass = "h-56 sm:h-64",
}: {
  points: PriceChartPoint[];
  color?: string;
  gradientId?: string;
  heightClass?: string;
}) {
  const chart = useMemo(() => {
    if (points.length < 2) return null;
    const values = points.map((p) => p.close);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || Math.max(Math.abs(max) * 0.01, 0.0001);
    const width = 560;
    const height = 170;
    const left = 44;
    const top = 16;
    const xFor = (i: number) => left + (i / (points.length - 1)) * width;
    const yFor = (v: number) => top + (1 - (v - min) / span) * height;

    let line = "";
    points.forEach((p, i) => {
      line += `${i === 0 ? "M" : "L"}${xFor(i).toFixed(1)},${yFor(p.close).toFixed(1)} `;
    });
    const linePath = line.trim();
    const areaPath = `${linePath} L${xFor(points.length - 1).toFixed(1)},${top + height} L${left},${top + height} Z`;
    const yLabels = [0, 1, 2, 3].map((i) => (max - (span * i) / 3).toFixed(2));
    return { linePath, areaPath, yLabels, width, height, left, top };
  }, [points]);

  if (!chart || points.length < 2) {
    return (
      <EmptyState
        title="暂无可绘制的行情数据"
        hint="接口已返回，但行情序列为空或不足两个点；后端股票行情模块可能尚未上线。"
      />
    );
  }

  const first = points[0];
  const latest = points[points.length - 1];
  const prev = points.length >= 2 ? points[points.length - 2] : first;
  const up = latest.close >= prev.close;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs text-slate-500">最新收盘（{fmtDate(latest.date)}）</p>
          <p className={`text-2xl font-semibold tabular-nums ${up ? "text-rose-600" : "text-emerald-600"}`}>
            {latest.close.toFixed(2)}
          </p>
        </div>
        <p className="text-xs text-slate-400">{points.length} 个交易日</p>
      </div>
      <svg viewBox="0 0 620 220" className={`w-full overflow-visible ${heightClass}`}>
        <defs>
          <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.22" />
            <stop offset="100%" stopColor={color} stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {[0, 1, 2, 3].map((index) => {
          const y = chart.top + index * (chart.height / 3);
          return (
            <line
              key={index}
              x1={chart.left}
              x2={chart.left + chart.width}
              y1={y}
              y2={y}
              stroke="#e2e8f0"
              strokeDasharray="4"
            />
          );
        })}
        <path d={chart.areaPath} fill={`url(#${gradientId})`} />
        <path d={chart.linePath} fill="none" stroke={color} strokeWidth="2.5" strokeLinecap="round" />
        {chart.yLabels.map((label, index) => (
          <text key={index} x="0" y={chart.top + 3 + index * (chart.height / 3)} fontSize="10" fill="#64748b">
            {label}
          </text>
        ))}
        <text x={chart.left} y="212" fontSize="10" fill="#64748b">
          {fmtDate(first.date)}
        </text>
        <text x={chart.left + chart.width - 80} y="212" fontSize="10" fill="#64748b">
          {fmtDate(latest.date)}
        </text>
      </svg>
    </div>
  );
}
