"use client";

import { useEffect, useState } from "react";
import {
  isWatched,
  subscribeWatchlist,
  toggleWatchlist,
  type WatchlistKind,
} from "@/lib/watchlist";

/**
 * 加入/移出自选按钮：基金与股票共用，localStorage 持久化。
 */
export function WatchlistButton({
  kind,
  code,
  name,
  size = "md",
}: {
  kind: WatchlistKind;
  code: string;
  name: string;
  size?: "sm" | "md";
}) {
  const [watched, setWatched] = useState(false);

  useEffect(() => {
    setWatched(isWatched(kind, code));
    return subscribeWatchlist(() => setWatched(isWatched(kind, code)));
  }, [kind, code]);

  const base =
    size === "sm"
      ? "rounded-md px-2 py-1 text-xs"
      : "rounded-lg px-3 py-1.5 text-sm";

  return (
    <button
      type="button"
      aria-pressed={watched}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        const { watched: next } = toggleWatchlist(kind, code, name);
        setWatched(next);
      }}
      className={`inline-flex items-center gap-1 border font-medium transition-colors ${base} ${
        watched
          ? "border-amber-300 bg-amber-50 text-amber-700 hover:bg-amber-100"
          : "border-slate-300 bg-white text-slate-600 hover:bg-slate-50"
      }`}
      title={watched ? "移出自选" : "加入自选"}
    >
      <svg
        viewBox="0 0 24 24"
        fill={watched ? "currentColor" : "none"}
        stroke="currentColor"
        strokeWidth="1.8"
        className={size === "sm" ? "h-3.5 w-3.5" : "h-4 w-4"}
      >
        <path d="M12 3l2.7 5.6 6.1.8-4.5 4.2 1.1 6-5.4-3-5.4 3 1.1-6L3.2 9.4l6.1-.8L12 3z" />
      </svg>
      {watched ? "已自选" : "加自选"}
    </button>
  );
}
