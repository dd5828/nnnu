"use client";

/** 根级错误边界（Next 约定）：根 layout 自己抛错时的最后一道网。
 *  必须自带 <html>/<body>；语言 store 不依赖 Provider，直接读没问题。 */

import { useI18n } from "@/hooks/useI18n";

export default function GlobalError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const { t } = useI18n();
  return (
    <html lang="zh-CN">
      <body
        style={{
          display: "flex",
          minHeight: "100vh",
          alignItems: "center",
          justifyContent: "center",
          fontFamily: "system-ui, sans-serif",
          background: "#f7f8fa",
          color: "#1a1d24",
        }}
      >
        <div style={{ maxWidth: 420, padding: 24, textAlign: "center" }}>
          <h1 style={{ fontSize: 18, fontWeight: 600 }}>{t("common.errorTitle")}</h1>
          <p style={{ marginTop: 8, fontSize: 13, opacity: 0.7 }}>{t("common.errorBody")}</p>
          <button
            type="button"
            onClick={reset}
            style={{
              marginTop: 16,
              padding: "6px 14px",
              borderRadius: 8,
              border: "none",
              background: "#4f6ef7",
              color: "#fff",
              fontSize: 13,
              cursor: "pointer",
            }}
          >
            {t("common.retry")}
          </button>
        </div>
      </body>
    </html>
  );
}
