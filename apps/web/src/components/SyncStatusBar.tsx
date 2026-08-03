"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import {
  fmtBeijingTime,
  normalizeSyncStatus,
  type SyncJobView,
  type SyncStatusView,
} from "@/lib/normalize";

const STATUS_DOT: Record<SyncJobView["status"], string> = {
  success: "bg-emerald-500",
  partial: "bg-amber-500",
  failed: "bg-rose-500",
  paused: "bg-sky-500",
  running: "bg-amber-400 animate-pulse",
  unknown: "bg-slate-300",
};

const STATUS_LABEL: Record<SyncJobView["status"], string> = {
  success: "正常",
  partial: "部分失败",
  failed: "失败",
  paused: "已暂停",
  running: "运行中",
  unknown: "无记录",
};

function jobTooltip(job: SyncJobView): string {
  const lines = [
    `${job.label} · ${STATUS_LABEL[job.status]}`,
    job.finishedAt
      ? `最近完成：${fmtBeijingTime(job.finishedAt)}`
      : job.startedAt
        ? `最近开始：${fmtBeijingTime(job.startedAt)}`
        : "暂无运行记录",
  ];
  if (job.dataDate) lines.push(`数据日期：${job.dataDate}`);
  if (job.updated !== null) {
    lines.push(
      `更新 ${job.updated}${job.total !== null ? ` / ${job.total}` : ""} 条` +
        (job.failedCount ? `，失败 ${job.failedCount}` : "")
    );
  }
  if (job.nextRun) lines.push(`下次计划：${fmtBeijingTime(job.nextRun)}`);
  if (job.error) lines.push(`错误：${job.error}`);
  return lines.join("\n");
}

/** 汇总所有任务状态，用于紧凑态的整体指示点 */
function overallStatus(view: SyncStatusView): SyncJobView["status"] {
  const jobs = view.jobs;
  if (jobs.some((j) => j.status === "failed")) return "failed";
  if (jobs.some((j) => j.status === "running")) return "running";
  if (jobs.some((j) => j.status === "partial")) return "partial";
  if (jobs.some((j) => j.status === "paused")) return "paused";
  if (jobs.length > 0 && jobs.every((j) => j.status === "unknown")) return "unknown";
  return "success";
}

export function SyncStatusBar() {
  const [view, setView] = useState<SyncStatusView | null>(null);
  const [failed, setFailed] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  // 紧凑可展开：默认收起，仅显示一行摘要
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(async (manual = false) => {
    if (manual) setRefreshing(true);
    try {
      const raw = await api.syncStatus();
      setView(normalizeSyncStatus(raw));
      setFailed(false);
    } catch {
      // 宽松容错：接口未上线/失败时仅隐藏数据，不影响页面其它部分
      setFailed(true);
    } finally {
      if (manual) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 60_000);
    return () => clearInterval(timer);
  }, [load]);

  const nextRuns = (view?.jobs ?? []).filter((j) => j.nextRun);
  // 最近的一次计划运行（用于摘要展示）
  const soonest = nextRuns
    .map((j) => ({ label: j.label, at: j.nextRun as string }))
    .sort((a, b) => a.at.localeCompare(b.at))[0];

  // 最近一次完成的任务时间（紧凑摘要用）
  const latestFinished = view?.jobs
    .filter((j) => j.finishedAt || j.startedAt)
    .map((j) => ({ label: j.label, at: (j.finishedAt ?? j.startedAt) as string }))
    .sort((a, b) => b.at.localeCompare(a.at))[0];

  const overall = view ? overallStatus(view) : "unknown";
  const failedCount = view?.jobs.filter((j) => j.status === "failed").length ?? 0;
  const partialCount = view?.jobs.filter((j) => j.status === "partial").length ?? 0;
  const pausedCount = view?.jobs.filter((j) => j.status === "paused").length ?? 0;

  return (
    <div className="mb-6 rounded-xl border border-slate-200 bg-white text-xs text-slate-500 shadow-sm">
      {/* 紧凑态：一行摘要，点击可展开明细 */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3.5 py-2">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="flex items-center gap-1.5 font-medium text-slate-600 hover:text-slate-900"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-3.5 w-3.5">
            <path d="M21 12a9 9 0 1 1-3-6.7" />
            <path d="M21 3v6h-6" />
          </svg>
          数据更新
          {view && <span className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT[overall]}`} />}
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            className={`h-3 w-3 transition-transform ${expanded ? "rotate-180" : ""}`}
          >
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>

        {view ? (
          <>
            {failedCount > 0 && (
              <span className="rounded bg-rose-50 px-1.5 py-0.5 font-medium text-rose-700">
                {failedCount} 项失败
              </span>
            )}
            {partialCount > 0 && (
              <span className="rounded bg-amber-50 px-1.5 py-0.5 font-medium text-amber-700">
                {partialCount} 项部分失败
              </span>
            )}
            {pausedCount > 0 && (
              <span className="rounded bg-sky-50 px-1.5 py-0.5 font-medium text-sky-700">
                {pausedCount} 项已暂停
              </span>
            )}
            {latestFinished && (
              <span className="tabular-nums text-slate-400">
                最近：{latestFinished.label} {fmtBeijingTime(latestFinished.at).slice(5)}
              </span>
            )}
            {soonest && (
              <span className="tabular-nums text-slate-400">
                下次：{soonest.label} {fmtBeijingTime(soonest.at).slice(5)}
              </span>
            )}
            {view.serverTime && (
              <span className="ml-auto tabular-nums text-slate-400">
                北京时间 {fmtBeijingTime(view.serverTime)}
              </span>
            )}
          </>
        ) : (
          <span className="text-slate-400">{failed ? "状态接口暂不可用" : "正在查询…"}</span>
        )}

        <button
          type="button"
          onClick={() => void load(true)}
          disabled={refreshing}
          className={`flex items-center gap-1 rounded-md border border-slate-200 px-2 py-1 font-medium text-slate-500 hover:bg-slate-50 hover:text-slate-800 disabled:opacity-50 ${
            view?.serverTime ? "" : "ml-auto"
          }`}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`}
          >
            <path d="M21 12a9 9 0 1 1-3-6.7" />
            <path d="M21 3v6h-6" />
          </svg>
          刷新
        </button>
      </div>

      {/* 展开态：各任务明细 */}
      {expanded && view && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-slate-100 px-3.5 py-2.5">
          {view.jobs.map((job) => (
            <span
              key={job.job}
              title={jobTooltip(job)}
              className="flex cursor-default items-center gap-1.5"
            >
              <span className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT[job.status]}`} />
              <span className="text-slate-600">{job.label}</span>
              <span className="tabular-nums text-slate-400">
                {job.finishedAt || job.startedAt
                  ? fmtBeijingTime(job.finishedAt ?? job.startedAt).slice(5)
                  : STATUS_LABEL[job.status]}
              </span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
