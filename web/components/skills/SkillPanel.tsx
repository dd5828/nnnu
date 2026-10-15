"use client";

/**
 * 技能列表（§7.16）：启用开关、来源徽标、新建/编辑/删除。
 *
 * 停用一个技能 = 下个回合不再挂 read_skill、系统提示也不再有它的清单（改完即生效，
 * 没有缓存要清）。编辑要先取详情（正文在详情接口里），所以这里是「列表 → 拉详情 →
 * 开编辑器」的三段，不是列表数据直接塞进去。
 */

import { useState } from "react";
import { Loader2, Pencil, Plus, Sparkles, Trash2 } from "lucide-react";
import SkillEditor from "@/components/skills/SkillEditor";
import { confirmDialog } from "@/hooks/useConfirm";
import { useI18n } from "@/hooks/useI18n";
import { useDeleteSkill, useSkill, useSkillList, useUpdateSkill } from "@/hooks/useSkills";
import { errorText } from "@/lib/errors";
import { sourceLabelKey } from "@/lib/skills";

type EditorState = { mode: "closed" } | { mode: "new" } | { mode: "edit"; name: string };

export default function SkillPanel() {
  const { t } = useI18n();
  const { data, isLoading, error } = useSkillList();
  const update = useUpdateSkill();
  const remove = useDeleteSkill();
  const [editor, setEditor] = useState<EditorState>({ mode: "closed" });
  const editing = editor.mode === "edit" ? editor.name : null;
  const detail = useSkill(editing);

  const closeEditor = () => setEditor({ mode: "closed" });

  if (editor.mode === "new") {
    return <SkillEditor skill={null} onClose={closeEditor} />;
  }
  if (editor.mode === "edit") {
    if (detail.isLoading) {
      return (
        <div className="flex justify-center py-8">
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        </div>
      );
    }
    if (detail.error || !detail.data) {
      return (
        <div className="space-y-2">
          <p className="text-sm text-danger">
            {errorText(detail.error, t("common.requestFailed"))}
          </p>
          <button
            type="button"
            onClick={closeEditor}
            className="rounded-lg px-3 py-1.5 text-sm text-muted transition-colors hover:bg-accent"
          >
            {t("common.close")}
          </button>
        </div>
      );
    }
    return <SkillEditor skill={detail.data} onClose={closeEditor} />;
  }

  const skills = data?.skills ?? [];

  const handleDelete = (name: string) => {
    void confirmDialog({
      message: t("skills.deleteConfirm", { name }),
      danger: true,
    }).then((ok) => {
      if (ok) {
        remove.mutate(name);
      }
    });
  };

  return (
    <div className="space-y-3">
      <div className="flex items-start gap-3">
        <p className="text-xs text-muted">{t("skills.desc")}</p>
        <button
          type="button"
          data-testid="sk-new"
          onClick={() => setEditor({ mode: "new" })}
          className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90"
        >
          <Plus className="h-3.5 w-3.5" />
          {t("skills.newSkill")}
        </button>
      </div>

      {(update.error || remove.error) && (
        <p className="text-xs text-danger">
          {errorText(update.error ?? remove.error, t("common.requestFailed"))}
        </p>
      )}

      {isLoading ? (
        <div className="flex justify-center py-8">
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        </div>
      ) : error ? (
        <p className="text-sm text-danger">{errorText(error, t("common.requestFailed"))}</p>
      ) : skills.length === 0 ? (
        <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-12 text-center">
          <Sparkles className="h-6 w-6 text-muted" />
          <p className="text-sm text-muted">{t("skills.empty")}</p>
          <p className="max-w-sm text-xs text-muted">{t("skills.emptyHint")}</p>
        </div>
      ) : (
        <ul className="space-y-2">
          {skills.map((skill) => (
            <li
              key={skill.name}
              data-testid="sk-item"
              data-name={skill.name}
              className={`rounded-xl border border-border bg-surface p-4 transition-colors hover:border-primary/40 ${
                skill.enabled ? "" : "opacity-70"
              }`}
            >
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate font-mono text-sm font-medium">{skill.name}</span>
                    <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted">
                      {t(sourceLabelKey(skill.source))}
                    </span>
                    {skill.hub_ref && (
                      <span className="text-[10px] text-muted">hub: {skill.hub_ref}</span>
                    )}
                  </div>
                  {skill.description && (
                    <p className="mt-0.5 line-clamp-2 text-xs text-muted">{skill.description}</p>
                  )}
                  {skill.when_to_use && (
                    <p className="mt-0.5 line-clamp-1 text-xs text-muted">
                      {t("skills.whenToUseInline", { text: skill.when_to_use })}
                    </p>
                  )}
                  <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-muted">
                    <span>{t("skills.filesCount", { n: String(skill.files_count) })}</span>
                    {skill.missing && <span className="text-danger">{t("skills.missing")}</span>}
                    {skill.broken && <span className="text-danger">{t("skills.broken")}</span>}
                  </div>
                </div>
                <label className="flex shrink-0 cursor-pointer items-center gap-1.5 text-xs text-muted">
                  <input
                    type="checkbox"
                    data-testid={`sk-toggle-${skill.name}`}
                    checked={skill.enabled}
                    onChange={(event) =>
                      update.mutate({ name: skill.name, enabled: event.target.checked })
                    }
                    className="h-4 w-4 accent-[var(--primary)]"
                  />
                  {t("skills.enable")}
                </label>
                <button
                  type="button"
                  data-testid={`sk-edit-${skill.name}`}
                  onClick={() => setEditor({ mode: "edit", name: skill.name })}
                  title={t("skills.edit")}
                  className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
                >
                  <Pencil className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  data-testid={`sk-delete-${skill.name}`}
                  onClick={() => handleDelete(skill.name)}
                  title={t("skills.delete")}
                  className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-danger"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
