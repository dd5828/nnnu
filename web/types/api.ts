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
  effect: "instant" | "restart";
  choices: string[] | null;
  current: unknown;
}

/** 对应 nnnu/api/routers/settings.py 的 GET /api/v1/settings/{area} 响应。 */
export interface SettingsAreaResponse {
  area: string;
  values: Record<string, unknown>;
  fields: SettingsFieldMeta[];
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
  capabilities: { name: string; display_name?: string }[];
}
