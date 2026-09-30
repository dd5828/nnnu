"use client";

/** ECharts 渲染（§7.7）：只吃严格 JSON 的 option，代码永不 eval。
 *
 * 依赖走动态 import（echarts 几百 KB，别的页面不该为它买单）。ECharts 的配色不吃 CSS
 * 变量，所以把 `--foreground` 的计算值塞进 textStyle 顶层默认（模型自己写了颜色的不覆盖），
 * 并监听 `data-theme` 变化重挂一次——换主题后图表文字跟着变。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import RenderError from "./RenderError";

interface ChartHandle {
  setOption(option: unknown): void;
  resize(): void;
  dispose(): void;
}

function cssVar(name: string, fallback: string): string {
  if (typeof window === "undefined") {
    return fallback;
  }
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

/** 顶层 textStyle 只在模型没给时补默认文字色（它自己写死的颜色不动）。 */
function applyThemeDefaults(option: unknown, color: string): unknown {
  if (typeof option !== "object" || option === null || Array.isArray(option)) {
    return option;
  }
  const record = option as Record<string, unknown>;
  if (record.textStyle !== undefined) {
    return option;
  }
  return { textStyle: { color }, ...record };
}

export default function EchartsViewer({ code, className }: { code: string; className?: string }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [runtimeError, setRuntimeError] = useState("");
  const [themeTick, setThemeTick] = useState(0);

  // JSON 解析在渲染期算（放 effect 里同步 setState 会触发级联渲染）
  const parsed = useMemo(() => {
    try {
      return { option: JSON.parse(code) as unknown, error: "" };
    } catch (err) {
      return { option: null as unknown, error: err instanceof Error ? err.message : String(err) };
    }
  }, [code]);

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeTick((tick) => tick + 1));
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || parsed.error) {
      return;
    }
    const option = parsed.option;

    let disposed = false;
    let chart: ChartHandle | null = null;
    let observer: ResizeObserver | null = null;
    void (async () => {
      try {
        const echarts = await import("echarts");
        if (disposed) {
          return;
        }
        const themed = applyThemeDefaults(option, cssVar("--foreground", "#1f2430"));
        chart = echarts.init(host, undefined, { renderer: "svg" }) as unknown as ChartHandle;
        chart.setOption(themed);
        observer = new ResizeObserver(() => chart?.resize());
        observer.observe(host);
      } catch (err) {
        if (!disposed) {
          setRuntimeError(err instanceof Error ? err.message : String(err));
        }
      }
    })();

    return () => {
      disposed = true;
      observer?.disconnect();
      chart?.dispose();
    };
  }, [parsed, themeTick]);

  const error = parsed.error || runtimeError;
  if (error) {
    return <RenderError kind="ECharts" detail={error} />;
  }
  return (
    <div
      ref={hostRef}
      data-testid="echarts-viewer"
      className={`w-full ${className ?? "h-[360px]"}`}
    />
  );
}
