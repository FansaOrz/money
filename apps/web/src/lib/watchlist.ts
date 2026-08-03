/**
 * 自选股/基金清单：localStorage 持久化，基金与股票共存。
 * 仅浏览器端可用；SSR 时所有读取返回空列表。
 */

export type WatchlistKind = "fund" | "stock";

export interface WatchlistItem {
  kind: WatchlistKind;
  code: string;
  name: string;
  addedAt: string; // ISO 时间戳
}

const STORAGE_KEY = "money.watchlist.v1";
const LEGACY_DISCOVERY_KEY = "money:discovery:watchlist";

function readAll(): WatchlistItem[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const data: unknown = raw ? JSON.parse(raw) : [];
    const current = Array.isArray(data)
      ? data
          .filter(
            (x): x is WatchlistItem =>
              !!x &&
              typeof x === "object" &&
              (x as WatchlistItem).kind !== undefined &&
              typeof (x as WatchlistItem).code === "string"
          )
          .map((x) => ({
            kind: x.kind === "stock" ? "stock" : "fund" as WatchlistKind,
            code: x.code,
            name: typeof x.name === "string" && x.name ? x.name : x.code,
            addedAt: typeof x.addedAt === "string" ? x.addedAt : new Date().toISOString(),
          }))
      : [];

    const legacyRaw = window.localStorage.getItem(LEGACY_DISCOVERY_KEY);
    const legacy: unknown = legacyRaw ? JSON.parse(legacyRaw) : [];
    if (!Array.isArray(legacy)) return current;
    let changed = false;
    for (const code of legacy) {
      if (typeof code !== "string" || !code || current.some((x) => x.kind === "fund" && x.code === code)) {
        continue;
      }
      current.push({ kind: "fund", code, name: code, addedAt: new Date().toISOString() });
      changed = true;
    }
    if (changed) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(current));
    if (legacyRaw) window.localStorage.removeItem(LEGACY_DISCOVERY_KEY);
    return current;
  } catch {
    return [];
  }
}

function writeAll(items: WatchlistItem[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
    window.dispatchEvent(new Event("money:watchlist"));
  } catch {
    /* 存储失败静默 */
  }
}

export function getWatchlist(): WatchlistItem[] {
  return readAll();
}

export function isWatched(kind: WatchlistKind, code: string): boolean {
  return readAll().some((x) => x.kind === kind && x.code === code);
}

export function addToWatchlist(kind: WatchlistKind, code: string, name: string): WatchlistItem[] {
  const items = readAll();
  if (items.some((x) => x.kind === kind && x.code === code)) return items;
  const next = [...items, { kind, code, name: name || code, addedAt: new Date().toISOString() }];
  writeAll(next);
  return next;
}

export function removeFromWatchlist(kind: WatchlistKind, code: string): WatchlistItem[] {
  const next = readAll().filter((x) => !(x.kind === kind && x.code === code));
  writeAll(next);
  return next;
}

export function toggleWatchlist(kind: WatchlistKind, code: string, name: string): { items: WatchlistItem[]; watched: boolean } {
  if (isWatched(kind, code)) {
    return { items: removeFromWatchlist(kind, code), watched: false };
  }
  return { items: addToWatchlist(kind, code, name), watched: true };
}

/** 订阅自选变更（同页内多组件同步） */
export function subscribeWatchlist(listener: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const handler = () => listener();
  window.addEventListener("money:watchlist", handler);
  window.addEventListener("storage", handler);
  return () => {
    window.removeEventListener("money:watchlist", handler);
    window.removeEventListener("storage", handler);
  };
}
