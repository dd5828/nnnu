/** 技能的纯计算（§7.16，P11）：名字轻校验、SKILL.md 组装与正文回填、安全门摘要。
 *
 * 全是纯函数，交给 `tests/skills.test.ts` 盯（vitest 是 node 环境，组件测试跑不起来）。
 * 前端校验只拦明显手误——权威判定永远在后端（spec.py 解析、gate.py 扫描）。
 */

import type { GateReport } from "@/types/api";

/** 技能名规范：与后端 spec.SKILL_NAME_RE 一致（小写字母开头，仅小写字母/数字/连字符，≤48）。 */
export const SKILL_NAME_RE = /^[a-z][a-z0-9-]{0,47}$/;

/** 编辑器四个字段（正文不含 frontmatter，保存时由 composeSkillMd 拼回去）。 */
export interface SkillDraft {
  name: string;
  description: string;
  whenToUse: string;
  body: string;
}

export type SkillDraftError = "name" | "description" | "body" | null;

/** 表单轻校验：返回第一个不合规字段（locale 键后缀），全合格返回 null。 */
export function validateSkillDraft(draft: SkillDraft): SkillDraftError {
  if (!SKILL_NAME_RE.test(draft.name.trim())) {
    return "name";
  }
  if (!draft.description.trim()) {
    return "description";
  }
  if (!draft.body.trim()) {
    return "body";
  }
  return null;
}

/** JS 字符串 → YAML 双引号标量（JSON 是 YAML 1.2 的子集，引号/换行/反斜杠都安全）。 */
function yamlString(value: string): string {
  return JSON.stringify(value.trim());
}

/** 表单字段 → SKILL.md 全文（保存时作为 content 发出去）。 */
export function composeSkillMd(draft: SkillDraft): string {
  const head = [`name: ${yamlString(draft.name)}`, `description: ${yamlString(draft.description)}`];
  if (draft.whenToUse.trim()) {
    head.push(`when_to_use: ${yamlString(draft.whenToUse)}`);
  }
  return `---\n${head.join("\n")}\n---\n\n${draft.body.trim()}\n`;
}

/** SKILL.md 全文 → 正文（frontmatter 结束行之后；语义对齐后端 parse_skill_md）。 */
export function skillBodyOf(content: string): string {
  const lines = content.replace(/^﻿/, "").split(/\r?\n/);
  if (lines[0]?.trim() !== "---") {
    return content.trim();
  }
  const close = lines.findIndex(
    (line, index) => index > 0 && (line.trim() === "---" || line.trim() === "...")
  );
  if (close === -1) {
    return content.trim();
  }
  return lines
    .slice(close + 1)
    .join("\n")
    .trim();
}

/** 市场条目的双语字段取值：当前语言 → zh → en → 空串。 */
export function hubText(map: Record<string, string>, lang: string): string {
  return map[lang] || map.zh || map.en || "";
}

/** 安全门报告摘要（null = 没扫过）：block/warn 各几条。 */
export function gateCounts(report: GateReport | null | undefined): { block: number; warn: number } {
  const findings = report?.findings ?? [];
  return {
    block: findings.filter((finding) => finding.severity === "block").length,
    warn: findings.filter((finding) => finding.severity !== "block").length,
  };
}

/** 安全门规则 id → 文案键（locales skills.rule_*）。 */
export function ruleLabelKey(rule: string): string {
  return `skills.rule_${rule}`;
}

/** 技能来源 → 徽标文案键。 */
export function sourceLabelKey(source: string): string {
  return source === "market" ? "skills.sourceMarket" : "skills.sourceLocal";
}

/** 引用文件体积（B/KB/MB，一位小数；展示用）。 */
export function fileSizeLabel(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
