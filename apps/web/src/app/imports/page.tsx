"use client";

import { useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import {
  fmtDate,
  fmtMoney,
  fmtPercent,
  fmtShares,
  normalizePreview,
  signClass,
  type PreviewView,
} from "@/lib/normalize";
import { Card, PageHeader, SnapshotNotice, Spinner } from "@/components/ui";
import { FundLink } from "@/components/FundLink";

type SlotStatus = "idle" | "uploading" | "preview" | "committing" | "committed" | "error";

interface SlotState {
  status: SlotStatus;
  fileName: string | null;
  preview: PreviewView | null;
  error: string | null;
  commitMessage: string | null;
}

const INITIAL_SLOT: SlotState = {
  status: "idle",
  fileName: null,
  preview: null,
  error: null,
  commitMessage: null,
};

const SLOT_LABELS = ["快照文件 A", "快照文件 B"] as const;

function StatusBadge({ status }: { status: SlotStatus }) {
  const map: Record<SlotStatus, { text: string; cls: string }> = {
    idle: { text: "待上传", cls: "bg-slate-100 text-slate-500" },
    uploading: { text: "解析中", cls: "bg-sky-50 text-sky-700" },
    preview: { text: "待确认", cls: "bg-amber-50 text-amber-700" },
    committing: { text: "写入中", cls: "bg-sky-50 text-sky-700" },
    committed: { text: "已导入", cls: "bg-emerald-50 text-emerald-700" },
    error: { text: "失败", cls: "bg-rose-50 text-rose-700" },
  };
  const { text, cls } = map[status];
  return (
    <span className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${cls}`}>
      {text}
    </span>
  );
}

function PreviewTables({ preview }: { preview: PreviewView }) {
  return (
    <div className="space-y-4">
      {preview.summary && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {[
            ["总市值", `¥${fmtMoney(preview.summary.totalMarketValue)}`],
            ["累计收益", `¥${fmtMoney(preview.summary.totalProfit)}`],
            ["收益率", fmtPercent(preview.summary.totalReturnRate)],
            ["持仓数", preview.summary.positionCount ?? "—"],
          ].map(([label, value]) => (
            <div key={label as string} className="rounded-lg bg-slate-50 px-3 py-2.5">
              <p className="text-xs text-slate-500">{label}</p>
              <p className="mt-0.5 text-sm font-semibold tabular-nums text-slate-800">
                {value as string}
              </p>
            </div>
          ))}
        </div>
      )}

      {preview.positions.length > 0 && (
        <div>
          <p className="mb-1.5 text-xs font-medium text-slate-500">
            解析到 {preview.positions.length} 条持仓
          </p>
          <div className="overflow-x-auto rounded-lg border border-slate-100">
            <table className="w-full min-w-[520px] text-xs">
              <thead>
                <tr className="bg-slate-50 text-left text-slate-500">
                  <th className="px-3 py-2 font-medium">基金</th>
                  <th className="px-3 py-2 text-right font-medium">份额</th>
                  <th className="px-3 py-2 text-right font-medium">市值（元）</th>
                  <th className="px-3 py-2 text-right font-medium">收益率</th>
                </tr>
              </thead>
              <tbody>
                {preview.positions.slice(0, 5).map((p) => (
                  <tr key={p.key} className="border-t border-slate-100">
                    <td className="px-3 py-2">
                      <FundLink
                        code={p.code}
                        name={p.name}
                        className="font-medium text-slate-700 hover:text-blue-700 hover:underline"
                      />
                      <span className="ml-1.5 text-slate-400">{p.code}</span>
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-slate-600">{fmtShares(p.shares)}</td>
                    <td className="px-3 py-2 text-right tabular-nums text-slate-700">{fmtMoney(p.marketValue)}</td>
                    <td className={`px-3 py-2 text-right tabular-nums ${signClass(p.returnRate)}`}>
                      {fmtPercent(p.returnRate)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {preview.positions.length > 5 && (
              <p className="border-t border-slate-100 bg-slate-50 px-3 py-1.5 text-slate-400">
                … 其余 {preview.positions.length - 5} 条从略
              </p>
            )}
          </div>
        </div>
      )}

      {preview.transactions.length > 0 && (
        <div>
          <p className="mb-1.5 text-xs font-medium text-slate-500">
            解析到 {preview.transactions.length} 条交易记录
          </p>
          <div className="overflow-x-auto rounded-lg border border-slate-100">
            <table className="w-full min-w-[520px] text-xs">
              <thead>
                <tr className="bg-slate-50 text-left text-slate-500">
                  <th className="px-3 py-2 font-medium">日期</th>
                  <th className="px-3 py-2 font-medium">基金</th>
                  <th className="px-3 py-2 font-medium">类型</th>
                  <th className="px-3 py-2 text-right font-medium">金额（元）</th>
                </tr>
              </thead>
              <tbody>
                {preview.transactions.slice(0, 5).map((t) => (
                  <tr key={t.key} className="border-t border-slate-100">
                    <td className="whitespace-nowrap px-3 py-2 tabular-nums text-slate-600">{fmtDate(t.date)}</td>
                    <td className="px-3 py-2">
                      <FundLink
                        code={t.code}
                        name={t.name}
                        className="font-medium text-slate-700 hover:text-blue-700 hover:underline"
                      />
                      <span className="ml-1.5 text-slate-400">{t.code}</span>
                    </td>
                    <td className="px-3 py-2 text-slate-600">{t.type}</td>
                    <td className="px-3 py-2 text-right tabular-nums text-slate-700">{fmtMoney(t.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {preview.transactions.length > 5 && (
              <p className="border-t border-slate-100 bg-slate-50 px-3 py-1.5 text-slate-400">
                … 其余 {preview.transactions.length - 5} 条从略
              </p>
            )}
          </div>
        </div>
      )}

      {preview.positions.length === 0 && preview.transactions.length === 0 && (
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
          后端未返回明细数据，请确认 PDF 内容可被解析后再确认导入。
        </p>
      )}

      {preview.warnings.length > 0 && (
        <ul className="space-y-1 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
          {preview.warnings.map((w, i) => (
            <li key={i}>· {w}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function ImportsPage() {
  const [slots, setSlots] = useState<SlotState[]>([
    { ...INITIAL_SLOT },
    { ...INITIAL_SLOT },
  ]);
  const fileInputs = useRef<(HTMLInputElement | null)[]>([]);

  const patchSlot = (index: number, patch: Partial<SlotState>) => {
    setSlots((prev) => prev.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  };

  const handleFile = async (index: number, file: File) => {
    if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
      patchSlot(index, {
        status: "error",
        fileName: file.name,
        error: "仅支持 PDF 文件",
        preview: null,
        commitMessage: null,
      });
      return;
    }
    patchSlot(index, {
      status: "uploading",
      fileName: file.name,
      error: null,
      preview: null,
      commitMessage: null,
    });
    try {
      const raw = await api.importPreview(file);
      const preview = normalizePreview(raw, file.name);
      if (preview.importId === null) {
        throw new ApiError("后端未返回 import_id，无法确认导入");
      }
      patchSlot(index, { status: "preview", preview });
    } catch (e) {
      patchSlot(index, {
        status: "error",
        error: e instanceof ApiError ? e.message : "上传或解析失败，请重试",
      });
    }
  };

  const handleCommit = async (index: number) => {
    const slot = slots[index];
    if (!slot.preview || slot.preview.importId === null) return;
    patchSlot(index, { status: "committing", error: null });
    try {
      const result = await api.importCommit(slot.preview.importId);
      const parts: string[] = [];
      if (typeof result.positions_written === "number")
        parts.push(`持仓 ${result.positions_written} 条`);
      if (typeof result.transactions_written === "number")
        parts.push(`交易 ${result.transactions_written} 条`);
      patchSlot(index, {
        status: "committed",
        commitMessage:
          result.message ??
          (parts.length > 0 ? `已写入${parts.join("、")}` : "导入成功"),
      });
    } catch (e) {
      patchSlot(index, {
        status: "error",
        error: e instanceof ApiError ? e.message : "确认导入失败，请重试",
      });
    }
  };

  const resetSlot = (index: number) => {
    patchSlot(index, { ...INITIAL_SLOT });
    const input = fileInputs.current[index];
    if (input) input.value = "";
  };

  return (
    <>
      <PageHeader
        title="数据导入"
        description="上传 PDF 净值快照，解析预览后确认写入。两个文件槽可分别多次上传，互不影响。"
      />
      <SnapshotNotice />

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        {slots.map((slot, index) => (
          <Card key={index} className="flex flex-col px-4 py-5 sm:px-5">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-800">
                {SLOT_LABELS[index]}
              </h2>
              <StatusBadge status={slot.status} />
            </div>

            <input
              ref={(el) => {
                fileInputs.current[index] = el;
              }}
              type="file"
              accept="application/pdf,.pdf"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void handleFile(index, file);
                e.target.value = "";
              }}
            />

            {slot.status === "idle" && (
              <button
                type="button"
                onClick={() => fileInputs.current[index]?.click()}
                className="flex flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-slate-200 px-4 py-10 text-slate-500 transition-colors hover:border-slate-400 hover:text-slate-700"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-8 w-8">
                  <path d="M12 3v12M7 10l5 5 5-5" />
                  <path d="M4 19h16" />
                </svg>
                <span className="text-sm font-medium">点击选择 PDF 文件</span>
                <span className="text-xs text-slate-400">
                  上传后将自动解析并展示预览
                </span>
              </button>
            )}

            {slot.status === "uploading" && (
              <Spinner label={`正在解析 ${slot.fileName}…`} />
            )}

            {slot.status === "error" && (
              <div className="flex flex-col items-center gap-3 py-8 text-center">
                <p className="text-sm font-medium text-rose-600">处理失败</p>
                {slot.fileName && (
                  <p className="break-all text-xs text-slate-500">{slot.fileName}</p>
                )}
                <p className="max-w-sm break-all text-xs text-slate-500">{slot.error}</p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => fileInputs.current[index]?.click()}
                    className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
                  >
                    重新选择文件
                  </button>
                  <button
                    type="button"
                    onClick={() => resetSlot(index)}
                    className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
                  >
                    清空
                  </button>
                </div>
              </div>
            )}

            {(slot.status === "preview" || slot.status === "committing") &&
              slot.preview && (
                <div className="space-y-4">
                  <div className="flex items-start justify-between gap-3 rounded-lg bg-slate-50 px-3 py-2.5">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-slate-800">
                        {slot.preview.fileName}
                      </p>
                      <p className="mt-0.5 text-xs text-slate-500">
                        快照日期：
                        {slot.preview.snapshotDate
                          ? fmtDate(slot.preview.snapshotDate)
                          : "未识别"}
                      </p>
                    </div>
                  </div>

                  <PreviewTables preview={slot.preview} />

                  <div className="flex flex-wrap gap-2 border-t border-slate-100 pt-4">
                    <button
                      type="button"
                      disabled={slot.status === "committing"}
                      onClick={() => void handleCommit(index)}
                      className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-60"
                    >
                      {slot.status === "committing" ? "写入中…" : "确认导入"}
                    </button>
                    <button
                      type="button"
                      disabled={slot.status === "committing"}
                      onClick={() => fileInputs.current[index]?.click()}
                      className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
                    >
                      换一份文件
                    </button>
                    <button
                      type="button"
                      disabled={slot.status === "committing"}
                      onClick={() => resetSlot(index)}
                      className="rounded-lg px-4 py-2 text-sm font-medium text-slate-500 hover:bg-slate-50 disabled:opacity-60"
                    >
                      取消
                    </button>
                  </div>
                </div>
              )}

            {slot.status === "committed" && (
              <div className="flex flex-col items-center gap-3 py-8 text-center">
                <div className="flex h-11 w-11 items-center justify-center rounded-full bg-emerald-50 text-emerald-600">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" className="h-6 w-6">
                    <path d="M5 12.5l4.5 4.5L19 7.5" />
                  </svg>
                </div>
                <p className="text-sm font-medium text-slate-800">
                  {slot.commitMessage ?? "导入成功"}
                </p>
                {slot.fileName && (
                  <p className="break-all text-xs text-slate-500">{slot.fileName}</p>
                )}
                <button
                  type="button"
                  onClick={() => resetSlot(index)}
                  className="mt-1 rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
                >
                  继续上传下一份
                </button>
              </div>
            )}
          </Card>
        ))}
      </div>

      <p className="mt-6 text-xs leading-relaxed text-slate-400">
        流程：选择 PDF → 后端解析并返回预览（/api/imports/preview）→ 确认无误后写入
        （/api/imports/{"{id}"}/commit）。每个文件槽可独立重复以上流程多次。
      </p>
    </>
  );
}
