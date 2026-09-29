/** 能力清单（§6.4）：批二三个 + 批三的「学习路径」+ P6 的「深度研究」。
 *
 * 阶段顺序与后端 `CapabilityManifest.stages` 保持一致（前端不该自己发明顺序，
 * 这只是静态镜像）；能力多起来就改读 `GET /api/v1/plugins` 的 `capabilities[].stages`。
 */

export const CAPABILITY_CHAT = "chat";
export const CAPABILITY_SOLVE = "deep_solve";
export const CAPABILITY_QUESTION = "deep_question";
export const CAPABILITY_MASTERY = "mastery_path";
export const CAPABILITY_RESEARCH = "deep_research";

/** 各能力的阶段顺序（chat 单阶段自由循环，没有阶段条）。
 *  学习路径对外仍只有一个阶段（§6.4），只是有阶段条才画得出来。 */
export const STAGES_BY_CAPABILITY: Record<string, string[]> = {
  [CAPABILITY_SOLVE]: ["planning", "reasoning", "writing"],
  [CAPABILITY_QUESTION]: ["ideation", "generation"],
  [CAPABILITY_MASTERY]: ["responding"],
  // 深度研究四阶段（§7.6）：一次调研跨两个回合，阶段条会在第二回合从「澄清」重新点亮，
  // 由后端每个阶段发一条 status 事件驱动，前端不用知道回合边界
  [CAPABILITY_RESEARCH]: ["rephrasing", "decomposing", "researching", "reporting"],
};

/** UI 文案键：能力名 → locales 里的键（与 manifest 的 label_i18n 各管各的：
 *  那边的键指提示词 YAML，UI 文案归 locales，§10.2）。 */
export const CAPABILITY_LABEL_KEYS: Record<string, string> = {
  [CAPABILITY_CHAT]: "chat.capabilityChat",
  [CAPABILITY_SOLVE]: "chat.capabilitySolve",
  [CAPABILITY_QUESTION]: "chat.capabilityQuestion",
  [CAPABILITY_MASTERY]: "chat.capabilityMastery",
  [CAPABILITY_RESEARCH]: "chat.capabilityResearch",
};

export const CAPABILITY_HINT_KEYS: Record<string, string> = {
  [CAPABILITY_CHAT]: "chat.capabilityChatHint",
  [CAPABILITY_SOLVE]: "chat.capabilitySolveHint",
  [CAPABILITY_QUESTION]: "chat.capabilityQuestionHint",
  [CAPABILITY_MASTERY]: "chat.capabilityMasteryHint",
  [CAPABILITY_RESEARCH]: "chat.capabilityResearchHint",
};

export const STAGE_LABEL_KEYS: Record<string, string> = {
  planning: "chat.stagePlanning",
  reasoning: "chat.stageReasoning",
  writing: "chat.stageWriting",
  ideation: "chat.stageIdeation",
  generation: "chat.stageGeneration",
  responding: "chat.stageResponding",
  rephrasing: "chat.stageRephrasing",
  decomposing: "chat.stageDecomposing",
  researching: "chat.stageResearching",
  reporting: "chat.stageReporting",
};

/** 研究档位（§7.6）：与后端 `services/research/models.py:DEPTHS` 一一对应。 */
export const RESEARCH_DEPTHS = ["quick", "standard", "deep"] as const;
export type ResearchDepth = (typeof RESEARCH_DEPTHS)[number];
/** 研究产出模式：report = 分节研究报告；answer = 直接回答。 */
export const RESEARCH_MODES = ["report", "answer"] as const;
export type ResearchMode = (typeof RESEARCH_MODES)[number];

/** 大纲正文的标题（与 prompts/{zh,en}/deep_research.yaml 的 `outline.heading` 对应）。
 *
 * 最后一条 assistant 消息里出现它 = 会话正停在大纲上等答复，确认按钮就该出现。
 * 报告段的标题是「研究报告 / Research report」，两者不会互相误判（修订版大纲的标题
 * 前面也带「研究大纲」，照样命中）。这是前端与提示词之间唯一的一处文本耦合，
 * 所以集中放在这里，改提示词时一眼能看见。 */
export const RESEARCH_OUTLINE_MARKERS = ["## 研究大纲", "## Research outline"];

export function isResearchOutline(content: string): boolean {
  return RESEARCH_OUTLINE_MARKERS.some((marker) => content.includes(marker));
}

/** 记录类型：解题会话存进笔记本标 solve、出题标 question，研究标 research，
 *  其余算 chat（§9.1 白名单，后端 `RECORD_TYPES` 同表）。 */
export function recordTypeFor(capability: string): "chat" | "solve" | "question" | "research" {
  if (capability === CAPABILITY_SOLVE) {
    return "solve";
  }
  if (capability === CAPABILITY_QUESTION) {
    return "question";
  }
  return capability === CAPABILITY_RESEARCH ? "research" : "chat";
}
