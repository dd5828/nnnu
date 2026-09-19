/**
 * 事件协议 TS 镜像（§12.1 契约检查源，与 src/nnnu/core/events.py 双向比对）。
 *
 * 格式约定（tests/test_contract_stream.py 用正则解析，勿改结构）：
 * - StreamEventType 联合类型放一行；
 * - 每个 payload 接口每字段一行（snake_case，与 pydantic 模型字段同名）。
 */

export type StreamEventType =
  | "turn_start"
  | "status"
  | "content_delta"
  | "content_done"
  | "thinking_delta"
  | "thinking_done"
  | "tool_call"
  | "tool_result"
  | "citation"
  | "ask_user"
  | "ask_user_reply"
  | "warning"
  | "error"
  | "cost_summary"
  | "done"
  | "stopped"
  | "heartbeat";

export interface StreamEventEnvelope {
  event_id: string;
  turn_id: string;
  session_id: string | null;
  type: StreamEventType;
  payload: unknown;
  ts: number;
  seq: number;
}

export interface TurnStartPayload {
  capability: string;
  model: string | null;
}

export interface StatusPayload {
  stage: string;
  message: string;
}

export interface ContentDeltaPayload {
  text: string;
}

export interface ContentDonePayload {
  full_text: string;
}

export interface ThinkingDeltaPayload {
  text: string;
}

export interface ThinkingDonePayload {
  text: string;
}

export interface ToolCallPayload {
  tool_name: string;
  args: Record<string, unknown>;
  call_id: string;
}

export interface ToolResultPayload {
  call_id: string;
  ok: boolean;
  summary: string;
  detail: Record<string, unknown> | null;
  usage: Record<string, unknown> | null;
}

export interface CitationSource {
  doc_id: string;
  kb: string;
  page: number | null;
  snippet: string;
}

export interface CitationPayload {
  sources: CitationSource[];
}

export interface AskUserPayload {
  question: string;
  options: string[];
  ask_id: string;
}

export interface AskUserReplyPayload {
  ask_id: string;
  answer: string;
}

export interface WarningPayload {
  message: string;
}

export interface ErrorPayload {
  message: string;
  recoverable: boolean;
}

export interface ModelCostBreakdown {
  provider: string;
  input_tokens: number;
  output_tokens: number;
  calls: number;
  cost: number;
}

export interface CostSummaryPayload {
  tokens: number;
  cost: number;
  per_model: Record<string, ModelCostBreakdown>;
}

export interface ToolTrace {
  tool_name: string;
  call_id: string;
  ok: boolean;
  summary: string;
}

export interface DonePayload {
  response: string;
  status: string;
  citations: CitationSource[];
  tool_calls: ToolTrace[];
  synthesized: boolean;
}

// 空 payload（§6.1）：无字段，接口形状由契约检查器约束
// eslint-disable-next-line @typescript-eslint/no-empty-object-type
export interface StoppedPayload {}

// eslint-disable-next-line @typescript-eslint/no-empty-object-type
export interface HeartbeatPayload {}
