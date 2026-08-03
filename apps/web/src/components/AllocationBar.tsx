import type { PositionView } from "@/lib/normalize";
import { fmtMoney } from "@/lib/normalize";
import { FundLink } from "@/components/FundLink";

const PALETTE = [
  "bg-slate-900",
  "bg-slate-500",
  "bg-slate-400",
  "bg-slate-300",
  "bg-slate-200",
  "bg-slate-100",
];

/** 纯 CSS 持仓占比条形图（不依赖外部图表库）。 */
export function AllocationBar({ positions }: { positions: PositionView[] }) {
  const items = positions
    .filter((p) => p.weight !== null && p.weight > 0)
    .sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0))
    .slice(0, 6);

  if (items.length === 0) return null;

  return (
    <div>
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-slate-100">
        {items.map((p, i) => (
          <div
            key={p.key}
            className={`${PALETTE[i % PALETTE.length]} h-full`}
            style={{ width: `${p.weight}%` }}
            title={`${p.name} ${p.weight?.toFixed(1)}%`}
          />
        ))}
      </div>
      <ul className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1.5 sm:grid-cols-2">
        {items.map((p, i) => (
          <li key={p.key} className="flex items-center gap-2 text-xs text-slate-600">
            <span className={`h-2.5 w-2.5 shrink-0 rounded-sm ${PALETTE[i % PALETTE.length]}`} />
            <FundLink
              code={p.code}
              name={p.name}
              className="truncate text-slate-600 hover:text-blue-700 hover:underline"
            />
            <span className="ml-auto shrink-0 tabular-nums text-slate-400">
              {p.weight?.toFixed(1)}% · ¥{fmtMoney(p.marketValue, 0)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
