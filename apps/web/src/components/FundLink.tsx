"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, type MouseEvent, type ReactNode } from "react";
import { api } from "@/lib/api";
import { rememberPageScroll } from "@/lib/navigation-memory";

interface FundLinkProps {
  code: string;
  name: ReactNode;
  className?: string;
  title?: string;
  stopPropagation?: boolean;
}

/** 全站统一的基金详情链接；代码无效时安全降级为纯文本。 */
export function FundLink({
  code,
  name,
  className = "font-medium text-slate-800 hover:text-blue-700 hover:underline",
  title,
  stopPropagation = false,
}: FundLinkProps) {
  const router = useRouter();
  const normalized = code?.trim();
  const href = normalized ? `/funds/${encodeURIComponent(normalized)}` : "";
  const prefetch = useCallback(() => {
    if (!normalized || normalized === "—") return;
    router.prefetch(href);
    // 页面代码与详情数据并行预热；失败时仍由详情页按正常流程处理。
    void api.fundDetail(normalized).catch(() => undefined);
  }, [href, normalized, router]);

  if (!normalized || normalized === "—") {
    return <span className={className}>{name}</span>;
  }

  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    if (stopPropagation) event.stopPropagation();
    rememberPageScroll();
  };

  return (
    <Link
      href={href}
      onClick={handleClick}
      onMouseEnter={prefetch}
      onFocus={prefetch}
      onTouchStart={prefetch}
      onPointerDown={prefetch}
      className={`${className} rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1`}
      title={title ?? `查看基金详情：${normalized}`}
    >
      {name}
    </Link>
  );
}
