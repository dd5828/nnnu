// 与后端协议对齐的 TS 类型（§7.21 手工镜像，注释指向后端模型）。

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

/** 记录类型：批一只有这三种（题库/研究记录随批二、P9 扩）。 */
export type NotebookRecordType = "chat" | "solve" | "note";

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
