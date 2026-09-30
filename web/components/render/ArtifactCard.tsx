"use client";

/** 二进制产物卡片（§7.8）：视频/图片 + 播放 + 下载 + 存笔记本。
 *
 * 产物地址是 `/api/v1/renders/{id}`（不带扩展名，见后端约定）；下载走 `?download=1`
 * 让服务端带 Content-Disposition。播放失败（mock 产物/浏览器解不了编码）不报错，退成
 * 「下载后用本地播放器看」——素材本身没问题，是播放环境的问题。
 */

import { useState } from "react";
import { Download, FileVideo } from "lucide-react";
import SaveToNotebook from "@/components/chat/SaveToNotebook";
import { useI18n } from "@/hooks/useI18n";
import { ARTIFACT_LANG, type ArtifactInfo } from "@/lib/render";

export default function ArtifactCard({ info }: { info: ArtifactInfo }) {
  const { t } = useI18n();
  const [failed, setFailed] = useState(false);
  const isVideo = info.kind === "video" || info.mime.startsWith("video/");
  const isImage = !isVideo && (info.kind === "image" || info.mime.startsWith("image/"));
  const fence = `\`\`\`${ARTIFACT_LANG}\n${JSON.stringify(info, null, 2)}\n\`\`\``;

  return (
    <div
      data-testid="artifact-card"
      className="flex w-full flex-col gap-2 rounded-xl border border-border bg-surface p-3"
    >
      {isVideo && !failed && (
        <video
          data-testid="artifact-video"
          src={info.url}
          controls
          preload="metadata"
          onError={() => setFailed(true)}
          className="w-full rounded-lg bg-black/80"
        />
      )}
      {isVideo && failed && (
        <div
          data-testid="artifact-fallback"
          className="flex items-center gap-2 rounded-lg border border-border px-3 py-6 text-xs text-muted"
        >
          <FileVideo className="h-4 w-4 shrink-0" />
          {t("render.playbackFailed")}
        </div>
      )}
      {isImage && (
        // eslint-disable-next-line @next/next/no-img-element -- 产物在本地渲染服务上，next/image 的优化器够不着
        <img
          data-testid="artifact-image"
          src={info.url}
          alt={info.filename || t("render.artifact")}
          className="w-full rounded-lg"
        />
      )}
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted">
        <span className="min-w-0 flex-1 truncate" title={info.filename}>
          {info.filename || t("render.artifact")}
        </span>
        {info.attempts > 1 && (
          <span data-testid="artifact-attempts">
            {t("render.attempts", { n: String(info.attempts) })}
          </span>
        )}
        <a
          data-testid="artifact-download"
          href={`${info.url}?download=1`}
          className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 transition-colors hover:bg-accent hover:text-foreground"
        >
          <Download className="h-3 w-3" />
          {t("render.download")}
        </a>
        <SaveToNotebook content={fence} />
      </div>
    </div>
  );
}
