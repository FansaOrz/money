"use client";

const SCROLL_PREFIX = "money:scroll:v1:";

export function rememberPageScroll(pathname = window.location.pathname): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(`${SCROLL_PREFIX}${pathname}`, String(window.scrollY));
  } catch {
    // sessionStorage 不可用时使用浏览器默认行为。
  }
}

export function restorePageScroll(pathname: string): () => void {
  if (typeof window === "undefined") return () => undefined;
  let target: number | null = null;
  try {
    const raw = window.sessionStorage.getItem(`${SCROLL_PREFIX}${pathname}`);
    if (raw !== null) {
      const parsed = Number(raw);
      if (Number.isFinite(parsed) && parsed >= 0) target = parsed;
    }
  } catch {
    return () => undefined;
  }
  if (target === null) {
    window.scrollTo({ top: 0 });
    return () => undefined;
  }

  // 缓存未命中时列表可能需要一段时间才能恢复高度，多次尝试直到内容就绪。
  // 任一用户滚动/触摸操作都会立即取消，避免与用户争夺滚动位置。
  let cancelled = false;
  const cancelOnInteraction = () => {
    cancelled = true;
  };
  window.addEventListener("wheel", cancelOnInteraction, { passive: true, once: true });
  window.addEventListener("touchstart", cancelOnInteraction, { passive: true, once: true });
  window.addEventListener("pointerdown", cancelOnInteraction, { passive: true, once: true });

  const timers = [0, 50, 150, 350, 700, 1200, 2000].map((delay) =>
    window.setTimeout(() => {
      if (!cancelled) window.scrollTo({ top: target!, behavior: "auto" });
    }, delay)
  );
  return () => {
    cancelled = true;
    timers.forEach((timer) => window.clearTimeout(timer));
    window.removeEventListener("wheel", cancelOnInteraction);
    window.removeEventListener("touchstart", cancelOnInteraction);
    window.removeEventListener("pointerdown", cancelOnInteraction);
  };
}
