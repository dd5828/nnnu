"use client";

/**
 * 技能编辑器（§7.16）：name / description / when_to_use + 剧本正文 + 引用文件只读清单。
 *
 * 前端只做轻校验（同名正则、非空），权威判定在后端——保存后把返回的安全门报告
 * （gate findings）摆在下面：block 红条、warn 黄条。本地保存不硬拦，但要让用户看见。
 */

import { useState } from "react";
import { CircleAlert, Loader2, Save, X } from "lucide-react";
import { useCreateSkill, useUpdateSkill } from "@/hooks/useSkills";
import { useI18n } from "@/hooks/useI18n";
import { errorText } from "@/lib/errors";
import {
  composeSkillMd,
  fileSizeLabel,
  gateCounts,
  ruleLabelKey,
  skillBodyOf,
  validateSkillDraft,
  type SkillDraftError,
} from "@/lib/skills";
import type { GateReport, SkillDetail } from "@/types/api";

const DRAFT_ERROR_KEYS: Record<Exclude<SkillDraftError, null>, string> = {
  name: "skills.errorName",
  description: "skills.errorDescription",
  body: "skills.errorBody",
};

export default function SkillEditor({
  skill,
  onClose,
}: {
  skill: SkillDetail | null; // null = 新建
  onClose: () => void;
}) {
  const { t } = useI18n();
  const create = useCreateSkill();
  const update = useUpdateSkill();
  const [name, setName] = useState(skill?.name ?? "");
  const [description, setDescription] = useState(skill?.description ?? "");
  const [whenToUse, setWhenToUse] = useState(skill?.when_to_use ?? "");
  const [body, setBody] = useState(skill ? skillBodyOf(skill.content) : "");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [report, setReport] = useState<GateReport | null>(skill?.gate ?? null);
  const busy = create.isPending || update.isPending;

  const submit = async () => {
    const draft = { name: name.trim(), description, whenToUse, body };
    const invalid = validateSkillDraft(draft);
    if (invalid) {
      setError(t(DRAFT_ERROR_KEYS[invalid]));
      return;
    }
    setError(null);
    setSaved(false);
    try {
      const content = composeSkillMd(draft);
      const result = skill
        ? await update.mutateAsync({ name: skill.name, content })
        : await create.mutateAsync({ name: draft.name, content });
      setReport(result.gate);
      setSaved(true);
      if (!skill) {
        onClose(); // 新建成功直接回列表（新技能已在里面）
      }
    } catch (err) {
      setError(errorText(err, t("common.requestFailed")));
    }
  };

  const counts = gateCounts(report);
  const inputClass =
    "w-full rounded-lg border border-border bg-background/40 px-3 py-1.5 text-sm outline-none transition-colors focus:border-primary/60";

  return (
    <section
      data-testid="sk-editor"
      className="space-y-3 rounded-xl border border-border bg-surface p-4"
    >
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">
          {skill ? t("skills.editSkill", { name: skill.name }) : t("skills.newSkill")}
        </h2>
        <button
          type="button"
          data-testid="sk-editor-close"
          onClick={onClose}
          aria-label={t("common.close")}
          className="rounded-full p-1 text-muted transition-colors hover:bg-accent hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-xs font-medium">{t("skills.name")}</span>
          <input
            data-testid="sk-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            disabled={Boolean(skill)} // 改名 = 新目录，编辑器不给改（后端也没这接口）
            placeholder={t("skills.namePlaceholder")}
            className={`${inputClass} disabled:opacity-60`}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium">{t("skills.whenToUse")}</span>
          <input
            data-testid="sk-when"
            value={whenToUse}
            onChange={(event) => setWhenToUse(event.target.value)}
            placeholder={t("skills.whenToUsePlaceholder")}
            className={inputClass}
          />
        </label>
      </div>

      <label className="block">
        <span className="mb-1 block text-xs font-medium">{t("skills.description")}</span>
        <input
          data-testid="sk-desc"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder={t("skills.descPlaceholder")}
          className={inputClass}
        />
      </label>

      <label className="block">
        <span className="mb-1 block text-xs font-medium">{t("skills.body")}</span>
        <textarea
          data-testid="sk-body"
          value={body}
          onChange={(event) => setBody(event.target.value)}
          rows={12}
          placeholder={t("skills.bodyPlaceholder")}
          className={`${inputClass} font-mono`}
        />
      </label>

      {skill && skill.files.length > 0 && (
        <div data-testid="sk-files" className="rounded-lg border border-border/70 p-3">
          <div className="mb-1 text-xs font-medium">{t("skills.files")}</div>
          <ul className="space-y-0.5">
            {skill.files.map((file) => (
              <li key={file.path} className="flex items-center justify-between gap-2 text-xs">
                <span className="truncate font-mono">{file.path}</span>
                <span className="shrink-0 text-muted">{fileSizeLabel(file.size)}</span>
              </li>
            ))}
          </ul>
          <p className="mt-1 text-[10px] text-muted">{t("skills.filesHint")}</p>
        </div>
      )}

      {report && report.findings.length > 0 && (
        <div
          data-testid="sk-gate"
          className={`rounded-lg border p-3 ${
            counts.block > 0 ? "border-danger/40 bg-danger/5" : "border-border"
          }`}
        >
          <p
            className={`mb-1 flex items-center gap-1.5 text-xs ${
              counts.block > 0 ? "text-danger" : "text-muted"
            }`}
          >
            <CircleAlert className="h-3.5 w-3.5 shrink-0" />
            {counts.block > 0
              ? t("skills.gateBlocked", { n: String(counts.block) })
              : t("skills.gateWarn", { n: String(counts.warn) })}
          </p>
          <ul className="space-y-1">
            {report.findings.map((finding, index) => (
              <li key={`${finding.rule}-${index}`} className="text-xs">
                <span className={finding.severity === "block" ? "text-danger" : "text-muted"}>
                  {t(ruleLabelKey(finding.rule))}
                </span>
                <span className="text-muted"> · {finding.path}</span>
                <p className="truncate font-mono text-[10px] text-muted">{finding.excerpt}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {error && (
        <p data-testid="sk-editor-error" className="text-xs text-danger">
          {error}
        </p>
      )}
      {saved && (
        <p data-testid="sk-editor-saved" className="text-xs text-success">
          {t("skills.saved")}
        </p>
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          data-testid="sk-save"
          onClick={() => void submit()}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
        >
          {busy ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Save className="h-3.5 w-3.5" />
          )}
          {skill ? t("common.save") : t("skills.create")}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg px-3.5 py-1.5 text-sm text-muted transition-colors hover:bg-accent"
        >
          {t("common.close")}
        </button>
      </div>
    </section>
  );
}
