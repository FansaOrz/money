"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { SyncStatusBar } from "@/components/SyncStatusBar";
import { rememberPageScroll, restorePageScroll } from "@/lib/navigation-memory";
import { api } from "@/lib/api";

/* ---------- 图标（沿用原有线性 SVG 风格） ---------- */

function DashboardIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <rect x="3" y="3" width="7" height="9" rx="1" />
      <rect x="14" y="3" width="7" height="5" rx="1" />
      <rect x="14" y="12" width="7" height="9" rx="1" />
      <rect x="3" y="16" width="7" height="5" rx="1" />
    </svg>
  );
}

function PositionsIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <path d="M3 20h18" />
      <rect x="5" y="10" width="4" height="8" rx="1" />
      <rect x="11" y="5" width="4" height="13" rx="1" />
      <rect x="17" y="13" width="4" height="5" rx="1" />
    </svg>
  );
}

function TransactionsIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <path d="M4 7h13M17 7l-3-3M17 7l-3 3" />
      <path d="M20 17H7M7 17l3-3M7 17l3 3" />
    </svg>
  );
}

function ImportsIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <path d="M12 3v12" />
      <path d="M7 10l5 5 5-5" />
      <path d="M4 19h16" />
    </svg>
  );
}

function DiscoveryIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <circle cx="11" cy="11" r="7" />
      <path d="M20 20l-3.8-3.8" />
    </svg>
  );
}

function QuantIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <path d="M3 17l5-6 4 4 6-8" />
      <path d="M18 7h3v3" />
      <path d="M3 21h18" />
    </svg>
  );
}

function SignalsIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 3" />
    </svg>
  );
}

function StockScreenerIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3" />
    </svg>
  );
}

function StockDetailIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <path d="M4 19V5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z" />
      <path d="M4 19a2 2 0 0 0 2 2h13" />
      <path d="M9 7h6M9 11h4" />
    </svg>
  );
}

function WatchlistIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <path d="M12 3l2.7 5.6 6.1.8-4.5 4.2 1.1 6-5.4-3-5.4 3 1.1-6L3.2 9.4l6.1-.8L12 3z" />
    </svg>
  );
}

function ResearchPortfoliosIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <path d="M4 19V5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z" />
      <path d="M4 19a2 2 0 0 0 2 2h13" />
      <path d="M9 7h6M9 11h4" />
    </svg>
  );
}

function PaperIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <rect x="3" y="6" width="18" height="13" rx="2" />
      <path d="M3 10h18" />
      <path d="M7 15h4" />
    </svg>
  );
}

function NewsIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <rect x="3" y="4" width="14" height="16" rx="2" />
      <path d="M17 8h3a1 1 0 0 1 1 1v9a2 2 0 0 1-2 2H7" />
      <path d="M7 9h6M7 13h6M7 17h4" />
    </svg>
  );
}

function KnowledgeIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4.5 w-4.5">
      <path d="M12 5.5C10.2 3.9 7.5 3.5 4 3.5v14c3.5 0 6.2.4 8 2 1.8-1.6 4.5-2 8-2v-14c-3.5 0-6.2.4-8 2z" />
      <path d="M12 5.5v14" />
    </svg>
  );
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      className={`h-3.5 w-3.5 shrink-0 transition-transform ${open ? "rotate-90" : ""}`}
    >
      <path d="M9 6l6 6-6 6" />
    </svg>
  );
}

/* ---------- 信息架构：一级大任务 → 二级小任务 ---------- */

interface NavLeaf {
  href: string;
  label: string;
  icon: ReactNode;
  /** 上下文高亮：当路径命中任一前缀时该小任务视为激活 */
  matchPrefixes?: string[];
}

interface NavGroup {
  key: string;
  label: string;
  items: NavLeaf[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    key: "overview",
    label: "总览",
    items: [
      { href: "/", label: "总览", icon: <DashboardIcon /> },
    ],
  },
  {
    key: "assets",
    label: "管理我的资产",
    items: [
      { href: "/positions", label: "持仓", icon: <PositionsIcon /> },
      { href: "/transactions", label: "交易记录", icon: <TransactionsIcon /> },
      { href: "/imports", label: "数据导入", icon: <ImportsIcon /> },
    ],
  },
  {
    key: "fund-research",
    label: "研究基金",
    items: [
      { href: "/discovery", label: "基金发现", icon: <DiscoveryIcon /> },
      { href: "/quant", label: "量化分析", icon: <QuantIcon /> },
      { href: "/signals", label: "研究信号", icon: <SignalsIcon /> },
    ],
  },
  {
    key: "stock-research",
    label: "研究股票",
    items: [
      {
        href: "/stock-screener",
        label: "股票筛选",
        icon: <StockScreenerIcon />,
        // 个股详情是股票筛选后的上下文页面，不单列不存在的列表入口。
        matchPrefixes: ["/stocks/"],
      },
    ],
  },
  {
    key: "tracking",
    label: "跟踪与验证",
    items: [
      { href: "/watchlist", label: "自选", icon: <WatchlistIcon /> },
      { href: "/research-portfolios", label: "研究组合", icon: <ResearchPortfoliosIcon /> },
      { href: "/paper", label: "模拟交易", icon: <PaperIcon /> },
      { href: "/news", label: "每日资讯", icon: <NewsIcon /> },
    ],
  },
  {
    key: "knowledge",
    label: "知识中心",
    items: [
      {
        href: "/knowledge",
        label: "指标与方法百科",
        icon: <KnowledgeIcon />,
        matchPrefixes: ["/knowledge/"],
      },
    ],
  },
];

/** 判断小任务是否激活 */
function isLeafActive(leaf: NavLeaf, pathname: string): boolean {
  if (leaf.href === "/") return pathname === "/";
  if (pathname === leaf.href || pathname.startsWith(`${leaf.href}/`)) return true;
  return (leaf.matchPrefixes ?? []).some((p) => pathname.startsWith(p));
}

/** 判断大任务是否包含当前路径 */
function isGroupActive(group: NavGroup, pathname: string): boolean {
  return group.items.some((leaf) => isLeafActive(leaf, pathname));
}

/** localStorage 里保存折叠状态的 key */
const COLLAPSE_STORAGE_KEY = "money:nav:collapsed";

function readCollapsedState(): Record<string, boolean> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(COLLAPSE_STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Record<string, boolean>) : {};
  } catch {
    return {};
  }
}

function writeCollapsedState(state: Record<string, boolean>) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify(state));
  } catch {
    // localStorage 不可用时静默降级
  }
}

/* ---------- 导航主体（桌面/移动共用） ---------- */

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  // 初始状态在挂载后从 localStorage 读取，避免 SSR 水合不一致
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    setCollapsed(readCollapsedState());
    setHydrated(true);
  }, []);

  // 当前路径变化时，自动展开所在大任务
  useEffect(() => {
    if (!hydrated) return;
    const active = NAV_GROUPS.find((g) => isGroupActive(g, pathname));
    if (!active) return;
    setCollapsed((prev) => {
      if (!prev[active.key]) return prev;
      const next = { ...prev, [active.key]: false };
      writeCollapsedState(next);
      return next;
    });
  }, [pathname, hydrated]);

  const toggleGroup = useCallback((key: string) => {
    setCollapsed((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      writeCollapsedState(next);
      return next;
    });
  }, []);

  return (
    <nav className="flex flex-col gap-1 px-3 pb-4">
      {NAV_GROUPS.map((group) => {
        const groupActive = isGroupActive(group, pathname);
        const isCollapsed = hydrated ? Boolean(collapsed[group.key]) : !groupActive;
        const single = group.items.length === 1;

        return (
          <div key={group.key}>
            {/* 一级大任务：单叶子时直接作为链接，多叶子时作为折叠开关 */}
            {single ? (
              <Link
                href={group.items[0].href}
                onClick={onNavigate}
                className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors ${
                  groupActive
                    ? "bg-slate-900 text-white"
                    : "text-slate-700 hover:bg-slate-200/70 hover:text-slate-900"
                }`}
              >
                {group.items[0].icon}
                {group.label}
              </Link>
            ) : (
              <button
                type="button"
                onClick={() => toggleGroup(group.key)}
                aria-expanded={!isCollapsed}
                className={`flex w-full items-center gap-2 rounded-lg px-3 py-2.5 text-sm font-semibold transition-colors ${
                  groupActive
                    ? "text-slate-900"
                    : "text-slate-500 hover:bg-slate-200/70 hover:text-slate-900"
                }`}
              >
                <ChevronIcon open={!isCollapsed} />
                <span className="flex-1 text-left">{group.label}</span>
              </button>
            )}

            {/* 二级小任务 */}
            {!single && !isCollapsed && (
              <div className="mt-0.5 flex flex-col gap-0.5">
                {group.items.map((leaf) => {
                  const active = isLeafActive(leaf, pathname);
                  return (
                    <Link
                      key={leaf.href}
                      href={leaf.href}
                      onClick={onNavigate}
                      className={`flex items-center gap-3 rounded-lg py-2 pl-9 pr-3 text-sm transition-colors ${
                        active
                          ? "bg-slate-900 font-medium text-white"
                          : "text-slate-600 hover:bg-slate-200/70 hover:text-slate-900"
                      }`}
                    >
                      {leaf.icon}
                      {leaf.label}
                    </Link>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </nav>
  );
}

/* ---------- 品牌区 ---------- */

function BrandMark({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <div
        className={`flex items-center justify-center bg-slate-900 font-bold text-white ${
          compact ? "h-7 w-7 rounded-md text-xs" : "h-8 w-8 rounded-lg text-sm"
        }`}
      >
        研
      </div>
      <div>
        <p className="text-sm font-semibold leading-tight">投资研究台</p>
        {!compact && (
          <p className="text-xs leading-tight text-slate-500">Investment Research Desk</p>
        )}
      </div>
    </div>
  );
}

/* ---------- 全局布局 ---------- */

export function AppShell({ children }: { children: ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const pathname = usePathname();
  const router = useRouter();

  useEffect(() => {
    const cancelRestore = restorePageScroll(pathname);
    // 不在路由卸载时再次保存：Next.js 可能已先把页面滚到顶部，
    // 此时保存会用 0 覆盖 FundLink 点击时记录的真实位置。
    return cancelRestore;
  }, [pathname]);

  useEffect(() => {
    const save = () => rememberPageScroll(pathname);
    window.addEventListener("pagehide", save);
    return () => window.removeEventListener("pagehide", save);
  }, [pathname]);

  useEffect(() => {
    if (!("scrollRestoration" in window.history)) return;
    const previous = window.history.scrollRestoration;
    window.history.scrollRestoration = "manual";
    return () => {
      window.history.scrollRestoration = previous;
    };
  }, []);

  useEffect(() => {
    // 本地工作台优先交互速度：浏览器空闲时预下载所有固定页面代码，
    // 后续点击侧栏无需再等待对应 JavaScript 分包。
    const prefetchRoutes = () => {
      const routes = new Set(NAV_GROUPS.flatMap((group) => group.items.map((item) => item.href)));
      routes.forEach((href) => router.prefetch(href));
      // 数据预热严格限制并发。SQLite 连接池较小，一次并发十几个研究接口会
      // 占满连接并让当前页面反而一直等待。因子榜、双动量、V2 信号等重计算
      // 只在用户进入对应标签时加载，之后继续复用六小时本地缓存。
      void (async () => {
        const batches: Array<Array<() => Promise<unknown>>> = [
          [api.portfolioSummary, api.positions, api.transactions],
          [api.portfolioReturns, api.quantPortfolio],
          [api.quantFunds, () => api.news("related"), () => api.news("market")],
          [api.paperSummary, api.paperPositions, api.researchPortfolios],
          [api.discoveryPools],
        ];
        for (const batch of batches) {
          await Promise.allSettled(batch.map((request) => request()));
        }
      })();
    };
    const idleWindow = window as Window & {
      requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
      cancelIdleCallback?: (id: number) => void;
    };
    if (idleWindow.requestIdleCallback) {
      const id = idleWindow.requestIdleCallback(prefetchRoutes, { timeout: 2000 });
      return () => idleWindow.cancelIdleCallback?.(id);
    }
    const timer = window.setTimeout(prefetchRoutes, 500);
    return () => window.clearTimeout(timer);
  }, [router]);

  return (
    <div className="flex min-h-screen">
      {/* 桌面端侧栏：粘性 + 自身滚动，容纳更多导航项 */}
      <aside className="sticky top-0 hidden h-screen w-64 shrink-0 flex-col border-r border-slate-200 bg-white md:flex">
        <div className="flex items-center gap-2 px-5 py-5">
          <BrandMark />
        </div>
        <div className="flex-1 overflow-y-auto">
          <NavLinks />
        </div>
        <div className="border-t border-slate-100 px-5 py-4 text-xs leading-relaxed text-slate-400">
          个人投资研究工作台
          <br />
          数据来自净值快照与公开行情，非实时，不构成投资建议
        </div>
      </aside>

      {/* 移动端顶栏 + 内容区 */}
      <div className="flex min-h-screen min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex items-center justify-between border-b border-slate-200 bg-white/90 px-4 py-3 backdrop-blur md:hidden">
          <BrandMark compact />
          <button
            type="button"
            aria-label="打开导航"
            onClick={() => setMobileOpen(true)}
            className="rounded-md p-2 text-slate-600 hover:bg-slate-100"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-5 w-5">
              <path d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
        </header>

        {/* 移动端抽屉：与桌面共用同一套任务导航 */}
        {mobileOpen && (
          <div className="fixed inset-0 z-40 md:hidden">
            <div
              className="absolute inset-0 bg-slate-900/40"
              onClick={() => setMobileOpen(false)}
            />
            <aside className="absolute left-0 top-0 flex h-full w-64 flex-col bg-white shadow-xl">
              <div className="flex items-center justify-between px-5 py-4">
                <BrandMark compact />
                <button
                  type="button"
                  aria-label="关闭导航"
                  onClick={() => setMobileOpen(false)}
                  className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100"
                >
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-5 w-5">
                    <path d="M6 6l12 12M18 6L6 18" />
                  </svg>
                </button>
              </div>
              <div className="flex-1 overflow-y-auto">
                <NavLinks onNavigate={() => setMobileOpen(false)} />
              </div>
            </aside>
          </div>
        )}

        <main className="flex-1 px-4 py-6 sm:px-6 lg:px-8">
          {/* 更宽的容器，适配研究类宽表；超宽屏居中 */}
          <div className="mx-auto w-full max-w-7xl">
            <SyncStatusBar />
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
