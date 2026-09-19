"use client";

/** 后端心跳指示：绿/红点，悬停显示版本。 */

import { useHealth } from "@/hooks/useHealth";

export default function HealthDot() {
  const { data, isError } = useHealth();
  const online = !isError && data?.status === "online";

  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs text-muted"
      title={online && data ? `v${data.version}` : undefined}
    >
      <span className={`size-2 rounded-full ${online ? "bg-success" : "bg-danger"}`} aria-hidden />
      {online ? data?.version : "—"}
    </span>
  );
}
