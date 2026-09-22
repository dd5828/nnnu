"use client";

/** 聊天状态（§4：WS 状态入 Zustand）：事件流归并成 ActiveTurn，终局后以服务器为事实源重取。 */

import { create } from "zustand";
import { useLanguageStore } from "@/i18n/language-store";
import { apiFetch } from "@/lib/api";
import { ChatSocket, type SocketStatus } from "@/lib/ws";
import type { CitationSource, CostSummaryPayload, StreamEventEnvelope } from "@/types/stream";

export interface AttachmentRef {
  id: string;
  name: string;
  mime: string | null;
}

export interface SessionMeta {
  id: string;
  title: string;
  capability: string;
  created_at: number;
  updated_at: number;
}

export interface UiToolCall {
  name: string;
  call_id: string;
  args: Record<string, unknown>;
  ok: boolean | null; // null = 执行中
  summary: string;
}

export interface UiMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  thinking: string | null;
  tool_calls: UiToolCall[];
  citations: CitationSource[];
  cost: { tokens: number; cost: number } | null;
  created_at: number;
}

export interface AskUserPrompt {
  question: string;
  options: string[];
  ask_id: string;
}

export interface ActiveTurn {
  turnId: string;
  sessionId: string | null;
  model: string | null;
  stage: string | null;
  thinking: string;
  content: string;
  toolCalls: Record<string, UiToolCall>;
  citations: CitationSource[];
  cost: { tokens: number; cost: number } | null;
  askUser: AskUserPrompt | null;
  warning: string | null;
  terminal: boolean;
}

function emptyTurn(turnId: string, sessionId: string | null, model: string | null): ActiveTurn {
  return {
    turnId,
    sessionId,
    model,
    stage: null,
    thinking: "",
    content: "",
    toolCalls: {},
    citations: [],
    cost: null,
    askUser: null,
    warning: null,
    terminal: false,
  };
}

interface ChatState {
  socketStatus: SocketStatus;
  sessions: SessionMeta[];
  sessionId: string | null;
  messages: UiMessage[];
  active: ActiveTurn | null;
  topError: string | null;

  init: () => void;
  refreshSessions: () => Promise<void>;
  ensureSession: () => Promise<string>;
  newSession: () => Promise<void>;
  selectSession: (id: string) => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  send: (text: string, attachments: AttachmentRef[]) => Promise<void>;
  stop: () => void;
  regenerate: () => void;
  replyAskUser: (answer: string) => void;
  clearTopError: () => void;
}

const socket = new ChatSocket();
let initialized = false;

/** 服务器消息 → UI 消息：tool_calls 落库是 done 事件的 ToolTrace 形状
 *（tool_name 键），与实时回合的 UiToolCall（name 键）不同，这里归一化。 */
function toUiMessages(raw: RawMessage[]): UiMessage[] {
  return raw.map((message) => ({
    id: message.id,
    role: message.role,
    content: message.content ?? "",
    thinking: message.thinking ?? null,
    tool_calls: (message.tool_calls ?? []).map((call) => ({
      name: String(call.tool_name ?? call.name ?? "tool"),
      call_id: String(call.call_id ?? ""),
      args: (call.args as Record<string, unknown>) ?? {},
      ok: typeof call.ok === "boolean" ? call.ok : null,
      summary: String(call.summary ?? ""),
    })),
    citations: message.citations ?? [],
    cost: message.cost ?? null,
    created_at: message.created_at,
  }));
}

interface RawMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  thinking: string | null;
  tool_calls: {
    tool_name?: string;
    name?: string;
    call_id?: string;
    args?: unknown;
    ok?: unknown;
    summary?: unknown;
  }[];
  citations: CitationSource[];
  cost: { tokens: number; cost: number } | null;
  created_at: number;
}

async function refreshMessages(
  set: (fn: (s: ChatState) => Partial<ChatState>) => void,
  sessionId: string
): Promise<void> {
  try {
    const detail = await apiFetch<{ messages: RawMessage[] }>(`/api/v1/sessions/${sessionId}`);
    set(() => ({ messages: toUiMessages(detail.messages ?? []) }));
  } catch {
    // 会话已被删除等：保留本地视图
  }
}

function bindSocket(
  set: (fn: (s: ChatState) => Partial<ChatState>) => void,
  get: () => ChatState
): void {
  socket.onStatus((status) => set(() => ({ socketStatus: status })));
  socket.onEvent((env: StreamEventEnvelope) => {
    const payload = env.payload as Record<string, unknown>;
    const state = get();
    const turn = state.active;
    switch (env.type) {
      case "turn_start":
        set(() => ({
          sessionId: env.session_id ?? state.sessionId,
          active: emptyTurn(env.turn_id, env.session_id, (payload.model as string | null) ?? null),
        }));
        break;
      case "status":
        if (turn && env.turn_id === turn.turnId) {
          set(() => ({
            active: {
              ...turn,
              stage: ((payload.message as string) || (payload.stage as string)) ?? null,
            },
          }));
        }
        break;
      case "thinking_delta":
        if (turn && env.turn_id === turn.turnId) {
          set(() => ({
            active: { ...turn, thinking: turn.thinking + ((payload.text as string) ?? "") },
          }));
        }
        break;
      case "thinking_done":
        if (turn && env.turn_id === turn.turnId) {
          set(() => ({ active: { ...turn, thinking: (payload.text as string) ?? turn.thinking } }));
        }
        break;
      case "content_delta":
        if (turn && env.turn_id === turn.turnId) {
          set(() => ({
            active: { ...turn, content: turn.content + ((payload.text as string) ?? "") },
          }));
        }
        break;
      case "content_done":
        if (turn && env.turn_id === turn.turnId) {
          set(() => ({
            active: { ...turn, content: (payload.full_text as string) ?? turn.content },
          }));
        }
        break;
      case "tool_call": {
        if (turn && env.turn_id === turn.turnId) {
          const callId = String(payload.call_id ?? "");
          const next = { ...turn, toolCalls: { ...turn.toolCalls } };
          next.toolCalls[callId] = {
            name: String(payload.tool_name ?? "tool"),
            call_id: callId,
            args: (payload.args as Record<string, unknown>) ?? {},
            ok: null,
            summary: "",
          };
          set(() => ({ active: next }));
        }
        break;
      }
      case "tool_result": {
        if (turn && env.turn_id === turn.turnId) {
          const callId = String(payload.call_id ?? "");
          const call = turn.toolCalls[callId];
          if (call) {
            const next = { ...turn, toolCalls: { ...turn.toolCalls } };
            next.toolCalls[callId] = {
              ...call,
              ok: Boolean(payload.ok),
              summary: String(payload.summary ?? ""),
            };
            set(() => ({ active: next }));
          }
        }
        break;
      }
      case "citation":
        if (turn && env.turn_id === turn.turnId) {
          set(() => ({
            active: { ...turn, citations: (payload.sources as CitationSource[]) ?? [] },
          }));
        }
        break;
      case "ask_user":
        if (turn && env.turn_id === turn.turnId) {
          set(() => ({
            active: {
              ...turn,
              askUser: {
                question: String(payload.question ?? ""),
                options: (payload.options as string[]) ?? [],
                ask_id: String(payload.ask_id ?? ""),
              },
            },
          }));
        }
        break;
      case "warning":
        if (turn && env.turn_id === turn.turnId) {
          set(() => ({ active: { ...turn, warning: String(payload.message ?? "") } }));
        }
        break;
      case "cost_summary":
        if (turn && env.turn_id === turn.turnId) {
          const summary = payload as unknown as CostSummaryPayload;
          set(() => ({
            active: { ...turn, cost: { tokens: summary.tokens ?? 0, cost: summary.cost ?? 0 } },
          }));
        }
        break;
      case "error":
        if (turn && env.turn_id === turn.turnId) {
          set(() => ({
            active: { ...turn, warning: String(payload.message ?? ""), terminal: true },
          }));
        } else {
          set(() => ({ topError: String(payload.message ?? "") }));
        }
        break;
      case "done":
      case "stopped":
        set(() => ({ active: null }));
        if (env.session_id) {
          void refreshMessages(set, env.session_id);
          void refreshSessions(set);
        }
        break;
      case "heartbeat":
      default:
        break;
    }
  });
}

async function refreshSessions(
  set: (fn: (s: ChatState) => Partial<ChatState>) => void
): Promise<void> {
  try {
    const data = await apiFetch<{ sessions: SessionMeta[] }>("/api/v1/sessions");
    set(() => ({ sessions: data.sessions ?? [] }));
  } catch {
    // 后端未起：保留现有列表，状态由健康条体现
  }
}

export const useChatStore = create<ChatState>()((set, get) => ({
  socketStatus: "closed",
  sessions: [],
  sessionId: null,
  messages: [],
  active: null,
  topError: null,

  init: () => {
    if (initialized) {
      return;
    }
    initialized = true;
    bindSocket(set, get);
    const { sessionId } = get();
    socket.connect(sessionId);
    void refreshSessions(set);
  },

  refreshSessions: async () => {
    await refreshSessions(set);
  },

  newSession: async () => {
    const session = await apiFetch<SessionMeta>("/api/v1/sessions", {
      method: "POST",
      body: JSON.stringify({ capability: "chat", language: useLanguageStore.getState().lang }),
    });
    socket.setSession(session.id);
    set(() => ({ sessionId: session.id, messages: [], active: null }));
    await refreshSessions(set);
  },

  ensureSession: async () => {
    const current = get().sessionId;
    if (current) {
      return current;
    }
    const session = await apiFetch<SessionMeta>("/api/v1/sessions", {
      method: "POST",
      body: JSON.stringify({ capability: "chat", language: useLanguageStore.getState().lang }),
    });
    socket.setSession(session.id);
    set(() => ({ sessionId: session.id }));
    await refreshSessions(set);
    return session.id;
  },

  selectSession: async (id: string) => {
    const { active } = get();
    if (active) {
      return; // 回合进行中不允许切会话（§6.8 同会话互斥，切走会看不到结果）
    }
    socket.setSession(id);
    set(() => ({ sessionId: id, active: null, topError: null }));
    await refreshMessages(set, id);
  },

  renameSession: async (id: string, title: string) => {
    await apiFetch(`/api/v1/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
    await refreshSessions(set);
  },

  deleteSession: async (id: string) => {
    await apiFetch(`/api/v1/sessions/${id}`, { method: "DELETE" });
    if (get().sessionId === id) {
      socket.setSession(null);
      set(() => ({ sessionId: null, messages: [], active: null }));
    }
    await refreshSessions(set);
  },

  send: async (text: string, attachments: AttachmentRef[]) => {
    if (!text.trim() && attachments.length === 0) {
      return;
    }
    const sid = await get().ensureSession();
    // 本地先贴出用户气泡（服务器落库后终局重取会替换为正式消息）
    const optimistic: UiMessage = {
      id: `local-${Date.now()}`,
      role: "user",
      content: text,
      thinking: null,
      tool_calls: [],
      citations: [],
      cost: null,
      created_at: Date.now() / 1000,
    };
    set((s) => ({ messages: [...s.messages, optimistic], topError: null }));
    socket.send({
      type: "chat",
      session_id: sid,
      message: text,
      attachments: attachments.map((a) => ({ id: a.id, name: a.name, mime: a.mime })),
      language: useLanguageStore.getState().lang,
    });
  },

  stop: () => {
    const { active } = get();
    if (active) {
      socket.send({ type: "stop", turn_id: active.turnId });
    }
  },

  regenerate: () => {
    const { sessionId } = get();
    if (sessionId) {
      socket.send({ type: "regenerate", session_id: sessionId });
    }
  },

  replyAskUser: (answer: string) => {
    const { active } = get();
    if (!active?.askUser) {
      return;
    }
    socket.send({
      type: "ask_user_reply",
      turn_id: active.turnId,
      ask_id: active.askUser.ask_id,
      answer,
    });
    set(() => ({ active: { ...active, askUser: null } }));
  },

  clearTopError: () => set(() => ({ topError: null })),
}));
