"use client";

import { useEffect, useRef, useState } from "react";
import { FundHistoryChart } from "./FundHistoryChart";

interface LazyFundHistoryChartProps {
  fundCode: string;
  fundName: string;
}

/**
 * 懒加载版基金走势图：
 * 使用 IntersectionObserver，仅当容器进入视口附近时才真正挂载 FundHistoryChart，
 * 避免“展开全部”时一次性发起全部历史净值请求。
 */
export function LazyFundHistoryChart({ fundCode, fundName }: LazyFundHistoryChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (visible) return;
    const node = containerRef.current;
    if (!node) return;
    if (typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setVisible(true);
            observer.disconnect();
            break;
          }
        }
      },
      { rootMargin: "240px 0px" }
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [visible]);

  return (
    <div ref={containerRef} className="min-h-[120px]">
      {visible ? (
        <FundHistoryChart fundCode={fundCode} fundName={fundName} />
      ) : (
        <p className="py-10 text-center text-xs text-slate-400">滚动到此处后自动加载走势图…</p>
      )}
    </div>
  );
}
