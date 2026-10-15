import { describe, expect, it } from "vitest";
import { translate } from "@/i18n";
import en from "@/locales/en.json";
import zh from "@/locales/zh.json";
import {
  SKILL_NAME_RE,
  composeSkillMd,
  fileSizeLabel,
  gateCounts,
  hubText,
  ruleLabelKey,
  skillBodyOf,
  sourceLabelKey,
  validateSkillDraft,
} from "@/lib/skills";

const DRAFT = { name: "flash-cards", description: "做闪卡。", whenToUse: "", body: "第一步。" };

describe("validateSkillDraft", () => {
  it("合格的草稿返回 null", () => {
    expect(validateSkillDraft(DRAFT)).toBeNull();
    expect(validateSkillDraft({ ...DRAFT, whenToUse: "用户要做闪卡时" })).toBeNull();
  });

  it("名字不合规优先报 name（空、大写、下划线、超长、数字开头）", () => {
    for (const name of ["", "Flash", "fl ash", "flash_cards", "1flash", "-flash", "a".repeat(49)]) {
      expect(validateSkillDraft({ ...DRAFT, name })).toBe("name");
    }
    expect(SKILL_NAME_RE.test("a".repeat(48))).toBe(true);
  });

  it("说明和正文缺失各自报对应字段", () => {
    expect(validateSkillDraft({ ...DRAFT, description: "  " })).toBe("description");
    expect(validateSkillDraft({ ...DRAFT, body: "\n" })).toBe("body");
  });
});

describe("composeSkillMd / skillBodyOf", () => {
  it("组装出的 frontmatter 能被拆回正文（含 when_to_use 可选）", () => {
    const content = composeSkillMd({ ...DRAFT, whenToUse: "做闪卡时" });
    expect(content.startsWith('---\nname: "flash-cards"\ndescription: "做闪卡。"\n')).toBe(true);
    expect(content).toContain('when_to_use: "做闪卡时"');
    expect(skillBodyOf(content)).toBe("第一步。");
  });

  it("没填 when_to_use 就不写这一行", () => {
    expect(composeSkillMd(DRAFT)).not.toContain("when_to_use");
  });

  it("引号与换行经过 JSON 转义，不会撑破 frontmatter", () => {
    const content = composeSkillMd({ ...DRAFT, description: '他说"做闪卡"\n换行' });
    expect(content).toContain('description: "他说\\"做闪卡\\"\\n换行"');
    expect(skillBodyOf(content)).toBe("第一步。");
  });

  it("skillBodyOf：无 frontmatter 原样返回；结束行 ... 也认；CRLF 与 BOM 兼容", () => {
    expect(skillBodyOf("就是正文")).toBe("就是正文");
    expect(skillBodyOf('---\nname: "x"\n...\n正文')).toBe("正文");
    expect(skillBodyOf('﻿---\r\nname: "x"\r\n---\r\n正文')).toBe("正文");
    expect(skillBodyOf('---\nname: "x"\n没收尾')).toContain("没收尾");
  });
});

describe("市场与安全门的展示纯函数", () => {
  it("hubText 取值：当前语言 → zh → en → 空串", () => {
    expect(hubText({ zh: "中", en: "EN" }, "en")).toBe("EN");
    expect(hubText({ zh: "中", en: "EN" }, "zh")).toBe("中");
    expect(hubText({ en: "EN" }, "zh")).toBe("EN");
    expect(hubText({ fr: "FR" }, "zh")).toBe("");
    expect(hubText({}, "zh")).toBe("");
  });

  it("gateCounts 分开数 block 与其余（null 当没发现）", () => {
    const report = {
      ok: false,
      findings: [
        { rule: "dangerous_command", severity: "block" as const, path: "SKILL.md", excerpt: "x" },
        { rule: "obfuscation", severity: "warn" as const, path: "SKILL.md", excerpt: "y" },
      ],
    };
    expect(gateCounts(report)).toEqual({ block: 1, warn: 1 });
    expect(gateCounts(null)).toEqual({ block: 0, warn: 0 });
    expect(gateCounts(undefined)).toEqual({ block: 0, warn: 0 });
  });

  it("ruleLabelKey / sourceLabelKey 拼键名", () => {
    expect(ruleLabelKey("shell_pipe_exec")).toBe("skills.rule_shell_pipe_exec");
    expect(sourceLabelKey("market")).toBe("skills.sourceMarket");
    expect(sourceLabelKey("local")).toBe("skills.sourceLocal");
  });

  it("fileSizeLabel 分档展示", () => {
    expect(fileSizeLabel(0)).toBe("0 B");
    expect(fileSizeLabel(1023)).toBe("1023 B");
    expect(fileSizeLabel(1024)).toBe("1.0 KB");
    expect(fileSizeLabel(1536)).toBe("1.5 KB");
    expect(fileSizeLabel(1024 * 1024 * 2)).toBe("2.0 MB");
  });
});

describe("技能文案", () => {
  // 与后端 gate.py 的规则表一一对应：加规则忘了加文案，这里会红
  const RULES = [
    "dangerous_command",
    "shell_pipe_exec",
    "credential_exfil",
    "suspicious_url",
    "binary_payload",
    "oversized",
    "frontmatter_invalid",
    "obfuscation",
  ];

  it("每条安全门规则都有中英文案，缺一不可", () => {
    for (const rule of RULES) {
      const key = ruleLabelKey(rule);
      expect(translate("zh", key)).not.toBe(key);
      expect(translate("en", key)).not.toBe(key);
    }
  });

  it("带占位符的文案插值后不留花括号", () => {
    const cases: [string, Record<string, string>][] = [
      ["skills.deleteConfirm", { name: "x" }],
      ["skills.filesCount", { n: "2" }],
      ["skills.whenToUseInline", { text: "t" }],
      ["skills.gateBlocked", { n: "1" }],
      ["skills.gateWarn", { n: "1" }],
      ["skills.hubBy", { author: "a" }],
      ["skills.hubInstallConfirm", { name: "n" }],
      ["skills.hubRemoteError", { error: "e" }],
    ];
    for (const lang of ["zh", "en"] as const) {
      for (const [key, vars] of cases) {
        expect(translate(lang, key, vars)).not.toMatch(/\{\w+\}/);
      }
    }
  });

  it("技能页与设置区的新键都在（含 settings.skills.*）", () => {
    for (const lang of ["zh", "en"] as const) {
      const dict = lang === "zh" ? zh : en;
      expect(dict.skills.tabSkills).toBeTruthy();
      expect(dict.skills.hubInstall).toBeTruthy();
      expect(dict.settings.skillsTitle).toBeTruthy();
      expect(dict.settings.skills.hubUrl).toBeTruthy();
      expect(dict.settings.skills.hubUrlDesc).toBeTruthy();
    }
  });
});
