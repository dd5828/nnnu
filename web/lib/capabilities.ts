/** 能力清单（§6.4）：批二起三个——聊天、解题、出题。
 *
 * 阶段顺序与后端 `CapabilityManifest.stages` 保持一致（前端不该自己发明顺序，
 * 这只是静态镜像）；能力多起来就改读 `GET /api/v1/plugins` 的 `capabilities[].stages`。
 */

export const CAPABILITY_CHAT = "chat";
export const CAPABILITY_SOLVE = "deep_solve";
export const CAPABILITY_QUESTION = "deep_question";

/** 各能力的阶段顺序（chat 单阶段自由循环，没有阶段条）。 */
export const STAGES_BY_CAPABILITY: Record<string, string[]> = {
  [CAPABILITY_SOLVE]: ["planning", "reasoning", "writing"],
  [CAPABILITY_QUESTION]: ["ideation", "generation"],
};

/** UI 文案键：能力名 → locales 里的键（与 manifest 的 label_i18n 各管各的：
 *  那边的键指提示词 YAML，UI 文案归 locales，§10.2）。 */
export const CAPABILITY_LABEL_KEYS: Record<string, string> = {
  [CAPABILITY_CHAT]: "chat.capabilityChat",
  [CAPABILITY_SOLVE]: "chat.capabilitySolve",
  [CAPABILITY_QUESTION]: "chat.capabilityQuestion",
};

export const CAPABILITY_HINT_KEYS: Record<string, string> = {
  [CAPABILITY_CHAT]: "chat.capabilityChatHint",
  [CAPABILITY_SOLVE]: "chat.capabilitySolveHint",
  [CAPABILITY_QUESTION]: "chat.capabilityQuestionHint",
};

export const STAGE_LABEL_KEYS: Record<string, string> = {
  planning: "chat.stagePlanning",
  reasoning: "chat.stageReasoning",
  writing: "chat.stageWriting",
  ideation: "chat.stageIdeation",
  generation: "chat.stageGeneration",
};

/** 记录类型：解题会话存进笔记本标 solve、出题标 question，其余算 chat（§9.1 白名单）。 */
export function recordTypeFor(capability: string): "chat" | "solve" | "question" {
  if (capability === CAPABILITY_SOLVE) {
    return "solve";
  }
  return capability === CAPABILITY_QUESTION ? "question" : "chat";
}
