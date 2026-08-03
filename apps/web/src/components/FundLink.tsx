import Link from "next/link";
import type { MouseEvent, ReactNode } from "react";

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
  const normalized = code?.trim();
  if (!normalized || normalized === "—") {
    return <span className={className}>{name}</span>;
  }

  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    if (stopPropagation) event.stopPropagation();
  };

  return (
    <Link
      href={`/funds/${encodeURIComponent(normalized)}`}
      onClick={handleClick}
      className={`${className} rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1`}
      title={title ?? `查看基金详情：${normalized}`}
    >
      {name}
    </Link>
  );
}
