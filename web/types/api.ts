// 与后端协议对齐的 TS 类型（§7.21 手工镜像，注释指向后端模型）。

import type { CitationSource } from "./stream";

/** 对应 nnnu/api/routers/health.py 的 /api/v1/health 响应。 */
export interface HealthMemory {
  rss_mb: number;
  percent: number;
  available_mb: number;
}

export interface HealthResponse {
  status: string;
  version: string;
  python: string;
  platform: string;
  memory: HealthMemory | null;
  uptime_s: number;
}

/** 对应 nnnu/services/settings/spec.py 的 SettingField 元数据。 */
export interface SettingsFieldMeta {
  key: string;
  type: string;
  label_key: string;
  description_key: string | null;
  effect: "instant" | "restart" | "reindex";
  choices: string[] | null;
  current: unknown;
}

/** 对应 nnnu/api/routers/settings.py 的 GET /api/v1/settings/{area} 响应。 */
export interface SettingsAreaResponse {
  area: string;
  values: Record<string, unknown>;
  fields: SettingsFieldMeta[];
}

/** 对应 nnnu/api/routers/settings.py 的 GET /api/v1/settings/llm-options 响应（§6.10）。
 *  只给 id 与布尔，密钥永不出现在这里。 */
export interface LlmOption {
  value: string;
  provider: string;
  provider_label: string;
  model: string;
  label: string;
  context_window: number | null;
  is_local: boolean;
  missing_key: boolean;
  is_active_default: boolean;
}

export interface LlmOptionsResponse {
  active: string | null;
  active_source: "settings" | "env" | "default" | "unresolved";
  options: LlmOption[];
}

/** 对应 nnnu/core/tool_protocol.py 的 ToolDefinition（§6.3）。 */
export interface ToolDefinitionMeta {
  name: string;
  description: string; // 提示词 i18n 键（后端 prompts/*/chat.yaml）
  parameters: Record<string, unknown>;
  mount: "user_toggleable" | "context_gated" | "always";
  cost_hint: string | null;
}

/** 对应 nnnu/api/routers/plugins.py 的 GET /api/v1/plugins 响应（§6.11 内省）。 */
export interface PluginsResponse {
  tools: { definition: ToolDefinitionMeta; available: boolean }[];
  capabilities: CapabilityMeta[];
}

// ---- 知识库（§7.9 / §8.3，对应 nnnu/services/knowledge/types.py） ----

/** KB 状态机：creating → indexing → ready | error。 */
export type KbStatus = "creating" | "indexing" | "ready" | "error";

/** 文档状态机：parsing → chunking → embedding → done | error；deleted 是终态。 */
export type KbDocStatus = "parsing" | "chunking" | "embedding" | "done" | "error" | "deleted";

export interface KbVersion {
  version: number;
  doc_count: number;
  chunk_count: number;
  dim: number;
  embedding_signature: string;
  created_at: number;
}

export interface KbDoc {
  doc_id: string;
  filename: string;
  stored_name: string;
  mime: string;
  size: number;
  fingerprint: string;
  status: KbDocStatus;
  page_count: number;
  chunk_count: number;
  error: string | null;
  progress: number; // 0~1，当前阶段进度
  note: string; // 给人看的一句（"正在下载嵌入模型 42/95 MB"）
  created_at: number;
  updated_at: number;
}

export interface KbBuild {
  version: number;
  doc_ids: string[];
  previous: Record<string, string>;
  stage: string;
  progress: number;
  note: string;
  cancel_requested: boolean;
  started_at: number;
}

export interface KbManifest {
  id: string;
  name: string;
  engine: string;
  status: KbStatus;
  active_version: number; // 0 = 还没有可用版本
  versions: KbVersion[];
  docs: KbDoc[];
  build: KbBuild | null;
  error: string | null;
  created_at: number;
  updated_at: number;
}

/** 对应 nnnu/services/rag/base.py 的 Hit（score 仅同一次检索内可比）。 */
export interface KbHit {
  doc_id: string;
  kb_id: string;
  score: number;
  text: string;
  page: number | null;
  metadata: Record<string, unknown>;
}

export interface KbSearchResponse {
  mode: string;
  query: string;
  hits: KbHit[];
}

/** 对应 GET /kbs/{id}/docs/{doc_id}/content（文本类阅读器）。 */
export interface KbDocContent {
  doc_id: string;
  filename: string;
  kind: string;
  page_count: number;
  text: string;
}

// ---- 笔记本（§8.2 / §9.1，对应 nnnu/services/notebooks/models.py） ----

/** 记录类型：批一三种 + 批二补的 question（出题回合存进笔记本的记录）+ P6 的 research。
 *  solve / question 是历史类型（解题、出题能力已下线），保留只为读旧记录。
 *  与后端 `services/notebooks/models.py:RecordType` 同一张表（多了就两边一起加）。 */
export type NotebookRecordType =
  | "chat"
  | "solve"
  | "note"
  | "question"
  | "research"
  | "visualize"
  | "math_animator";

export interface NotebookRecord {
  id: string;
  notebook_id: string;
  type: NotebookRecordType;
  title: string;
  content_md: string;
  source_ref: string | null; // 来源会话 id（聊天里存进来的那条）
  created_at: number;
}

/** 列表端点每条带 record_count；详情端点带 records。 */
export interface Notebook {
  id: string;
  name: string;
  description: string | null;
  created_at: number;
  record_count?: number;
  records?: NotebookRecord[];
}

/** GET /api/v1/notebooks/records/{id}（P9 引用深链跟随）：记录 + 当前归属的笔记本名。 */
export interface RecordDetail extends NotebookRecord {
  notebook_name: string;
}

/** 对应 GET /api/v1/plugins（§6.11）：能力清单里的 stages 是声明式的阶段顺序。 */
export interface CapabilityStageMeta {
  key: string;
  label_i18n: string;
  max_rounds: number;
  max_tokens: number;
}

export interface CapabilityMeta {
  name: string;
  version: string;
  stages: CapabilityStageMeta[];
  config_schema: Record<string, unknown>;
  default_model_role: string;
}

// ---- 题库（§7.4 / §8.2，对应 nnnu/services/question_bank/models.py） ----

export type QuestionType = "single" | "multi" | "short";

/** 题库筛选：全部 / 错题（wrong_count > 0）/ 未作答（还没作答过）。 */
export type QuestionFilter = "all" | "wrong" | "unanswered";

/** LLM 分类出的错因（固定枚举键；展示文案 questions.cause_*）。 */
export type ErrorCause =
  | "concept_unclear"
  | "misread"
  | "calculation"
  | "method_missing"
  | "memory_weak";

export interface Question {
  id: string;
  stem: string;
  options: string[]; // 裸文本，选项标签 A/B/C/D 按位置推
  answer: string; // 客观题是标签（多选升序拼接如 "AC"）；简答是参考答案
  explanation: string | null;
  source: string | null; // manual | tool | mastery | variant（旧数据还有 deep_question）
  tags: string[];
  note: string; // 用户笔记（Markdown；与 explanation 是两回事）
  note_updated_at: number | null;
  error_causes: ErrorCause[]; // LLM 分类的错因，可手改
  parent_id: string | null; // 变式题的出处题（软引用）
  mastery: number; // 0–1，界面按百分比显示
  wrong_count: number;
  last_attempt_at: number | null;
  created_at: number;
  type: QuestionType;
  knowledge_point: string;
  difficulty: string; // easy | medium | hard
  session_id: string | null;
  node_id: string | null; // 挂在学习路径的哪个节点上（软引用，节点删了就置空）
}

export interface QuestionAttempt {
  id: string;
  question_id: string;
  session_id: string | null;
  answer: string;
  correct: boolean;
  score: number;
  feedback: string | null;
  source: string; // deterministic | llm
  created_at: number;
}

/** 对应 GET /api/v1/questions：列表 + 三个筛选各自的条数（徽标用）。 */
export interface QuestionListResponse {
  questions: Question[];
  counts: { all: number; wrong: number; unanswered: number };
}

/** 对应 POST /api/v1/questions/{id}/attempt（本批新增的判分端点）。 */
export interface AttemptResponse {
  attempt: QuestionAttempt;
  question: Question;
  grading: {
    correct: boolean;
    score: number;
    feedback: string;
    source: string; // deterministic | llm
  };
}

// ---- 题库增强（P9：笔记 / AI 分类 / 举一反三） ----

/** 对应 GET /api/v1/questions/{id}/similar：题库内的相似题（零 LLM 排序）。 */
export interface SimilarItem {
  question: Question;
  score: number; // Jaccard 相似度 0–1（提示用，不是判重）
}

export interface SimilarResponse {
  question_id: string;
  low_confidence: boolean; // 题面太短：分数仅供参考
  items: SimilarItem[];
}

/** 对应 POST /api/v1/questions/{id}/classify：分类建议（题目本体已写回 question）。 */
export interface ClassifySuggestion {
  knowledge_point: string;
  tags: string[];
  error_causes: ErrorCause[];
  reason: string;
}

export interface ClassifyResponse {
  question: Question;
  suggestion: ClassifySuggestion;
}

/** 变式题草稿（预览态，无 id；采纳走 POST /questions/batch 才入库）。 */
export interface VariantDraft {
  stem: string;
  options: string[];
  answer: string;
  explanation: string;
  type: QuestionType;
  difficulty: string;
  knowledge_point: string;
  tags: string[];
  duplicate_score: number; // 与库内最像的题的相似度（同模板换数字天然很高，只作提示）
}

/** 知识库素材脚注（origin=kb 时有）。 */
export interface VariantSource {
  kb_id: string;
  doc_id: string;
  kb_name: string;
  filename: string;
  page: number | null;
  score: number;
  snippet: string;
}

export interface VariantsResponse {
  question_id: string;
  origin: "kb" | "ai"; // 实际用了哪条路
  degraded: boolean; // 想用知识库但没素材，降级为纯 AI
  sources: VariantSource[];
  variants: VariantDraft[];
}

/** 对应 POST /api/v1/questions/batch：批量入库（预览采纳入口）。 */
export interface BatchAdoptResponse {
  questions: Question[];
}

// ---- 学习路径（§7.5 / §8.2，对应 nnnu/services/learning/models.py） ----

/** 节点类型（批三重做后四类）：前两类定量过门（90 分），后两类定性过门（自己讲一遍）。 */
export type NodeType = "memory" | "procedure" | "concept" | "design";

/** 节点的门怎么算：quantitative 看分数、qualitative 看评定。 */
export type GateKind = "quantitative" | "qualitative";

/** 节点四态（§7.5：未开始 / 学习中 / 已过门 / 待复习）。 */
export type NodeState = "not_started" | "learning" | "mastered" | "reviewing";

/** 下一目标要做什么（服务端每回合现算；`complete` = 全部过门）。 */
export type NextAction = "answer_pending" | "review" | "probe" | "practice" | "assess" | "complete";

export interface LearningPath {
  id: string;
  topic: string;
  title: string;
  summary: string | null;
  // **没有 current_node_id**：门就是游标，下一步由服务端按「哪些节点已过门」现算
  session_id: string | null;
  created_at: number;
  updated_at: number;
}

export interface LearningNode {
  id: string;
  path_id: string;
  parent_id: string | null;
  title: string;
  node_type: NodeType;
  description: string;
  depth: number; // 树深（根 0），前端按它缩进
  sort_order: number;
  mastery: number; // 节点级 0–100（题级是 Question.mastery 的 0–1，两个别混）
  state: NodeState;
  review_stage: number; // 复习阶梯档位（下标）
  next_review_at: number | null;
  last_practiced_at: number | null;
  assess_passed: boolean; // 定性节点最近一次评定的结论
  assessed_at: number | null;
  // 下面四个是服务端算出来的（computed_field），前端**只读不镜像**
  gate: number | null; // 定量门（分数线）；定性节点为 null
  gate_kind: GateKind;
  cleared: boolean; // 是否已过门
  due: boolean; // 已过门且复习到期
  created_at: number;
  updated_at: number;
}

/** 看板汇总：`mastered`/`progress` 一律数「已过门」；`due` 与它正交（可重叠）。 */
export interface PathStats {
  total: number;
  mastered: number; // 已过门（含到期待复习的）
  learning: number; // 碰过但没过门
  not_started: number;
  due: number; // 已过门且到期
  progress: number; // 0–1 = 已过门 / total
  weak: number;
}

export interface WeakPoint {
  node_id: string;
  title: string;
  node_type: NodeType;
  mastery: number;
  gate: number; // 定量门；定性记 0（看板按定性样式渲染）
  gate_kind: GateKind;
  gap: number; // 距过门还差多少
  attempted: number;
  wrong: number;
}

export interface ReviewItem {
  node_id: string;
  title: string;
  node_type: NodeType;
  mastery: number;
  gate: number | null;
  gate_kind: GateKind;
  state: NodeState;
  review_stage: number;
  next_review_at: number | null;
  overdue: boolean;
}

/** 服务端现算的下一目标（状态块 / 工具输出 / 看板是同一个对象）。 */
export interface NextTarget {
  action: NextAction;
  node_id: string | null;
  node_title: string | null;
  node_type: NodeType | null;
  gate: number | null;
  gate_kind: GateKind | null;
  mastery: number;
  reason: string; // 人话一句，直接展示
  due_at: number | null;
}

/** 路径卡片：路径本体 + 汇总 + 「接着学什么」（列表端点）。 */
export interface LearningPathCard extends LearningPath {
  stats: PathStats;
  next_review_at: number | null;
  next_title: string | null;
  next_action: NextAction | null;
}

export interface LearningPathListResponse {
  paths: LearningPathCard[];
}

/** 路径详情：树 + 汇总 + 薄弱点 + 复习建议 + 下一目标（看板全部数据在一个响应里）。 */
export interface LearningPathDetail {
  path: LearningPath;
  nodes: LearningNode[];
  stats: PathStats;
  weak_points: WeakPoint[];
  reviews: ReviewItem[];
  next_target: NextTarget;
}

/** 对应 POST /learning/paths/{id}/session：只把这条路径的会话备好（绑定 + 改标题）。
 *  没有 turn_id——端点不跑回合，第一句由用户在输入框里打（对齐上游 launch-intent）。 */
export interface LearningSessionResponse {
  path_id: string;
  session_id: string;
}

/** 对应 POST /learning/paths/{id}/leave（对齐 `mastery_leave`）：只解开会话绑定，
 *  进度/题目/作答全留；会话本体还在（想接回去走 `mastery_switch`）。 */
export interface LearningLeaveResponse {
  left: string;
}

/** 跳过此题 / 重做路径的响应：路径详情 + 这一步动了多少（skipped 张卡 / reset 个节点）。 */
export interface LearningPathActionResponse extends LearningPathDetail {
  skipped?: number;
  reset?: number;
}

/** 跨路径的到期复习（`GET /learning/reviews`）：ReviewItem 加它属于哪条路径。 */
export interface DueReview extends ReviewItem {
  path_id: string;
  path_title: string;
}

export interface DueReviewListResponse {
  reviews: DueReview[];
}

/** 一次调研（`GET /research/runs`）：库里的那一行原样 + 会话标题（JOIN 来的）。
 *  报告正文不在列表里——它是要现查的，只在详情端点给（见 ResearchRunDetail.report）。 */
export type ResearchRunStatus = "confirming" | "researching" | "reported" | "partial" | "abandoned";
export type ResearchDepthValue = "quick" | "standard" | "deep";

export interface ResearchSubtopic {
  title: string;
  overview: string;
}

export interface ResearchRun {
  id: string;
  session_id: string;
  topic: string;
  refined_topic: string;
  mode: string;
  depth: string;
  subtopics: ResearchSubtopic[];
  status: ResearchRunStatus;
  failed_subtopics: string[];
  answer_message_id: string | null;
  created_at: number;
  updated_at: number;
  session_title: string;
}

export interface ResearchRunListResponse {
  runs: ResearchRun[];
  total: number;
}

/** 成稿那条助手消息：正文（`[n]` 已重排好）+ 来源（跟聊天页同一套 CitationSource 形状）。 */
export interface ResearchRunReport {
  message_id: string;
  content_md: string;
  citations: CitationSource[];
  created_at: number;
}

export interface ResearchRunDetail extends ResearchRun {
  report: ResearchRunReport | null;
}

// ---- 记忆（§7.10，手工镜像 nnnu/api/routers/memory.py + services/memory）----

export type MemoryLayer = "l1" | "l2" | "l3";

/** 条目来源（写者身份）：consolidator / model（write_memory 工具）/ human（工作台）。 */
export type MemoryOrigin = "consolidator" | "model" | "human";

/** L2/L3 文档里的一条 bullet（store.parse_entry + graph.entry_view 的展示视图）。 */
export interface MemoryEntryView {
  id: string | null; // mem-xxxxxxxx；null = 手写未纳管（audit 补 id 前只读展示）
  date: string; // YYYY-MM-DD
  text: string;
  refs: string[]; // ["L1:chat/2026-09.jsonl#123"] / ["L2:chat#mem-…"]
  stale: boolean; // audit 判引用失效后标注
  origin: MemoryOrigin | string;
  edited: boolean; // 人工编辑保护开关（state.json 权威）
  layer: "l2" | "l3";
  key: string;
  line: number; // 文件内 1-based 行号（仅供展示）
}

export interface MemoryDocStats {
  entries: number;
  stale: number;
  edited: number;
  anonymous: number; // 手写未纳管条数
}

/** 一份 L2/L3 文档：渲染文本 + 条目 + 计数。 */
export interface MemoryDocView {
  layer: "l2" | "l3";
  key: string;
  text: string;
  entries: MemoryEntryView[];
  stats: MemoryDocStats;
}

export interface MemoryL1Stats {
  files: string[]; // 月度文件（旧→新）
  lines: number;
  bytes: number;
  events: Record<string, number>; // 事件种类 → 行数
}

export interface MemoryConfigView {
  auto_enabled: boolean;
  auto_threshold_turns: number;
  inject_enabled: boolean;
  budget_update: number;
  budget_audit: number;
  budget_dedup: number;
  budget_extract: number;
  update_chunk_chars: number;
  trace_enabled: boolean;
}

/** 一次整合运行（state.json 的 last_run；202 回的 run 同形）。 */
export interface ConsolidationRun {
  id: string; // mrun-…
  trigger: "manual" | "auto" | string;
  status: "queued" | "running" | "ok" | "error" | "interrupted" | string;
  started_at: number;
  finished_at: number | null;
  stats: Record<string, number>;
  events: string[];
  error: string | null;
}

/** GET /api/v1/memory：三层概览（计数、水位、待整合回合、上次运行、是否在跑）。 */
export interface MemoryOverview {
  trace_enabled: boolean;
  surfaces: string[];
  l3_docs: string[];
  l1: Record<string, MemoryL1Stats>;
  l2: Record<string, MemoryDocStats>;
  l3: Record<string, MemoryDocStats>;
  watermarks: Record<string, { file: string; line: number }>;
  turns_since_consolidation: number;
  config: MemoryConfigView;
  last_run: ConsolidationRun | null;
  consolidating: boolean;
}

/** L1 轨迹一行（trace.py 的五键：ts/surface/session_id/event/data）。 */
export interface MemoryL1Row {
  ts: number;
  surface: string;
  session_id: string | null;
  event: string; // user_message | tool_call | assistant_done | ask_user | cost
  data: Record<string, unknown>;
}

export interface MemoryL1Page {
  surface: string;
  file: string;
  files: string[];
  total: number;
  offset: number;
  limit: number;
  rows: MemoryL1Row[];
}

export interface MemoryDocsResponse {
  layer: "l2" | "l3";
  docs: MemoryDocView[];
}

/** 图谱节点：L2/L3 条目或 L1 行；broken = 引用目标已失效（不静默消失）。 */
export interface MemoryGraphNode {
  id: string;
  layer: MemoryLayer;
  key: string;
  kind: string; // 条目节点恒为 "entry"；L1 节点是事件种类；断链是 "broken"
  text: string;
  date: string;
  ref: string;
  stale: boolean;
  edited: boolean;
  origin: MemoryOrigin | string;
  broken: boolean;
}

export interface MemoryGraphEdge {
  source: string;
  target: string;
}

export interface MemoryGraphPayload {
  root: string | null; // null = 全景
  depth: number;
  nodes: MemoryGraphNode[];
  edges: MemoryGraphEdge[];
}

/** 语义知识图谱实体（consolidator extract 模式产出；id 即实体名，同名已归并）。 */
export interface SemanticGraphNode {
  id: string;
  name: string;
  type: string; // person / topic / project / preference / other（未知一律 other）
  refs: string[]; // 证据条目 id（mem-…）；无有效引用的不落盘
  count: number; // 引用条数
  samples: string[]; // 前两条证据正文（截断）
}

/** 语义知识图谱关系：实体名之间的有向边。 */
export interface SemanticGraphEdge {
  source: string; // 实体 name
  target: string; // 实体 name
  type: string; // 关系短语（≤12 字）
  refs: string[];
  count: number;
}

export interface SemanticGraphStats {
  entities?: number;
  relations?: number;
  dropped_entities?: number;
  dropped_relations?: number;
  llm_calls?: number;
}

/** GET /api/v1/memory/graph?mode=semantic：派生工件视图（无工件 = 空态）。 */
export interface SemanticGraphPayload {
  mode: "semantic";
  updated_at: number;
  stale: boolean; // 源条目改过但还没重抽（实时比对，不落盘）
  nodes: SemanticGraphNode[];
  edges: SemanticGraphEdge[];
  stats: SemanticGraphStats;
}

export interface ConsolidateResponse {
  started: boolean;
  run: ConsolidationRun;
}
