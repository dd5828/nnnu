"use client";

/** 聊天状态（§4：WS 状态入 Zustand）：事件流归并成 ActiveTurn，终局后以服务器为事实源重取。 */

import { create } from "zustand";
import { useLanguageStore } from "@/i18n/language-store";
import { apiFetch } from "@/lib/api";
import { mergeMessages, messagesThroughLastUser } from "@/lib/chat-messages";
import {
  CAPABILITY_MATH_ANIMATOR,
  CAPABILITY_RESEARCH,
  CAPABILITY_VISUALIZE,
  isKnownCapability,
  type AnimatorQuality,
  type ResearchDepth,
  type ResearchMode,
  type VisualizeRenderType,
} from "@/lib/capabilities";
import { ChatSocket, type SocketStatus } from "@/lib/ws";
import type {
  AskUserOption,
  CitationSource,
  CostSummaryPayload,
  StreamEventEnvelope,
} from "@/types/stream";

/** 随消息下发的回合参数（§7.6）：服务端 `TurnRequest.config` 是直通的自由 dict，
 *  能力自己从里面取自己认识的键——研究档位、模式，以及确认按钮的 `research_action`。 */
export type TurnConfig = Record<string, string>;

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
  detail: Record<string, unknown> | null; // 结构化展示数据（如 imagegen 的 data URI）
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
  options: AskUserOption[];
  ask_id: string;
  /** 卡片是否收自由文本（定性评定要学习者自己写一段讲解） */
  allowFreeText: boolean;
  /** 服务端拼的展示副标题（如「节点《…》· 第 2/3 题」），空则不渲染 */
  context: string;
}

export interface ActiveTurn {
  turnId: string;
  sessionId: string | null;
  model: string | null;
  stage: string | null;
  /** 本回合已观测到的阶段（§7.3 步骤条）：去重保序，多阶段能力依次点亮 */
  stages: string[];
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
    stages: [],
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
  /** 会话级知识库选择（§7.9 粘性）：随每条消息发给后端，由后端全量替换并落库 */
  kbIds: string[];
  /** 会话级模型选择（§6.10 粘性，'provider:model'）：null = 跟随设置默认 */
  modelRef: string | null;
  /** 会话级能力选择（§6.4 粘性）：见 lib/capabilities 的能力清单，随每条消息下发并落库 */
  capability: string;
  /** 深度研究档位与产出模式（§7.6）：只在 capability=deep_research 时用得上，
   *  同样随每条消息的 config 下发（服务端按它拆子问题、选成稿提示词） */
  researchDepth: ResearchDepth;
  researchMode: ResearchMode;
  /** 可视化渲染类型（§7.7）：'auto' = 让分析段自己挑；只在 capability=visualize 时下发 */
  visualizeRenderType: VisualizeRenderType;
  /** 数学动画画质（§7.8）：只在 capability=math_animator 时下发 */
  animatorQuality: AnimatorQuality;

  init: () => void;
  refreshSessions: () => Promise<void>;
  ensureSession: () => Promise<string>;
  newSession: () => Promise<void>;
  selectSession: (id: string) => Promise<void>;
  attachSession: (id: string) => void;
  setKbIds: (ids: string[]) => void;
  setModelRef: (ref: string | null) => void;
  setCapability: (value: string) => void;
  setResearchDepth: (value: ResearchDepth) => void;
  setResearchMode: (value: ResearchMode) => void;
  setVisualizeRenderType: (value: VisualizeRenderType) => void;
  setAnimatorQuality: (value: AnimatorQuality) => void;
  renameSession: (id: string, title: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  send: (text: string, attachments: AttachmentRef[], extraConfig?: TurnConfig) => Promise<void>;
  stop: () => void;
  regenerate: () => void;
  replyAskUser: (answer: string) => void;
  clearTopError: () => void;
}

const socket = new ChatSocket();
let initialized = false;

/** 卡片选项归一：契约形状是 {label, description}；裸字符串（旧模型输出）兜成 label-only。 */
function normalizeAskOptions(raw: unknown): AskUserOption[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw
    .map((item) => {
      if (typeof item === "string") {
        return { label: item, description: "" };
      }
      const record = (item ?? {}) as Record<string, unknown>;
      return {
        label: String(record.label ?? ""),
        description: String(record.description ?? ""),
      };
    })
    .filter((option) => option.label !== "");
}

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
      // 小结构 detail 也落库（答题结果卡刷新后仍是卡）；超限的没落，用 null 兜住
      detail: (call.detail as Record<string, unknown>) ?? null,
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
    detail?: unknown; // 只落小结构（见 core/agent_loop.py 的 8KB 上限）
  }[];
  citations: CitationSource[];
  cost: { tokens: number; cost: number } | null;
  created_at: number;
}

/** hydrateKb/hydrateModel/hydrateCapability：切会话时顺带水合库、模型与能力选择；
 * 回合结束的重取不带——用户可能刚改过选择，服务器那边还是本回合发过去的旧值，
 * 回灌会把用户的改动抹掉（能力尤其：mid-turn 改不了，但下一回合前改得动）。 */
async function refreshMessages(
  set: (fn: (s: ChatState) => Partial<ChatState>) => void,
  sessionId: string,
  {
    hydrateKb = false,
    hydrateModel = false,
    hydrateCapability = false,
  }: { hydrateKb?: boolean; hydrateModel?: boolean; hydrateCapability?: boolean } = {}
): Promise<void> {
  try {
    const detail = await apiFetch<{
      messages: RawMessage[];
      kb_ids?: string[];
      model?: string | null;
      capability?: string;
    }>(`/api/v1/sessions/${sessionId}`);
    set((s) => ({
      // 按 id 复用没变的旧消息对象：终局重取时 memo 过的历史消息原地不动，
      // 不然每回合结束全列表都要重新 parse 一遍 Markdown（长会话肉眼可见卡一下）
      messages: mergeMessages(s.messages, toUiMessages(detail.messages ?? [])),
      ...(hydrateKb ? { kbIds: detail.kb_ids ?? [] } : {}),
      ...(hydrateModel ? { modelRef: detail.model ?? null } : {}),
      // 已下线能力的历史会话（解题/出题）回落 chat：本地选择器与后端注册表都没有这个名字
      ...(hydrateCapability
        ? {
            capability: isKnownCapability(detail.capability ?? "")
              ? (detail.capability as string)
              : "chat",
          }
        : {}),
    }));
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
          const stageKey = (payload.stage as string) ?? "";
          set(() => ({
            active: {
              ...turn,
              stage: ((payload.message as string) || stageKey) ?? null,
              // 步骤条按观测顺序点亮（多阶段能力；重复的 stage 不重复入列）
              stages:
                stageKey && !turn.stages.includes(stageKey)
                  ? [...turn.stages, stageKey]
                  : turn.stages,
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
            detail: null,
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
              detail: (payload.detail as Record<string, unknown>) ?? null,
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
                // 选项是 {label, description} 对象（§12.1 契约）；宽容旧形状的裸字符串
                options: normalizeAskOptions(payload.options),
                ask_id: String(payload.ask_id ?? ""),
                allowFreeText: Boolean(payload.allow_free_text),
                context: String(payload.context ?? ""),
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
          // 服务端拒了请求（error 帧不带 turn_id，如 regenerate 被回合互斥挡下）：
          // 本地可能已经先动过消息（乐观删除），拉一次服务器状态把视图对齐回来
          const sid = env.session_id ?? state.sessionId;
          if (sid) {
            void refreshMessages(set, sid);
          }
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
  kbIds: [],
  modelRef: null,
  capability: "chat",
  researchDepth: "standard",
  researchMode: "report",
  visualizeRenderType: "auto",
  animatorQuality: "medium",

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
      body: JSON.stringify({
        capability: get().capability,
        language: useLanguageStore.getState().lang,
      }),
    });
    socket.setSession(session.id);
    // 能力选择不清：那是输入区里的一档「模式」，用户没换就一直是它（切已有会话才水合）
    set(() => ({ sessionId: session.id, messages: [], active: null, kbIds: [], modelRef: null }));
    await refreshSessions(set);
  },

  ensureSession: async () => {
    const current = get().sessionId;
    if (current) {
      return current;
    }
    const session = await apiFetch<SessionMeta>("/api/v1/sessions", {
      method: "POST",
      body: JSON.stringify({
        capability: get().capability,
        language: useLanguageStore.getState().lang,
      }),
    });
    socket.setSession(session.id);
    // 不动 kbIds/modelRef/capability：空状态页上用户可以先把库、模型和能力选好再发
    // 第一条消息，这里清掉的话选择会被静默吞掉（会话建出来时选择照常随消息下发）
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
    await refreshMessages(set, id, {
      hydrateKb: true,
      hydrateModel: true,
      hydrateCapability: true,
    });
  },

  attachSession: (id: string) => {
    // REST 起的回合（学习会话、regenerate 式端点）不会自己进订阅：显式 resume 一次，
    // 从本会话已收到的最新 seq 往后补（同会话重复 attach 不会把正文灌两遍）
    socket.resume(id);
    set(() => ({ sessionId: id, topError: null }));
    // 回合刚起：用户那条消息已经落库了（编排器先落消息再流式），先取回来，
    // 免得讲解在流、用户看不到自己发的那句
    void refreshMessages(set, id);
  },

  setKbIds: (ids: string[]) => set(() => ({ kbIds: ids })),

  setModelRef: (ref: string | null) => set(() => ({ modelRef: ref })),

  setCapability: (value: string) => set(() => ({ capability: value })),

  setResearchDepth: (value: ResearchDepth) => set(() => ({ researchDepth: value })),

  setResearchMode: (value: ResearchMode) => set(() => ({ researchMode: value })),

  setVisualizeRenderType: (value: VisualizeRenderType) =>
    set(() => ({ visualizeRenderType: value })),

  setAnimatorQuality: (value: AnimatorQuality) => set(() => ({ animatorQuality: value })),

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
      set(() => ({ sessionId: null, messages: [], active: null, kbIds: [], modelRef: null }));
    }
    await refreshSessions(set);
  },

  send: async (text: string, attachments: AttachmentRef[], extraConfig?: TurnConfig) => {
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
      // 知识库选择随消息全量下发（§7.9 粘性）：空数组也是明确意思——取消全部选择
      kb_ids: get().kbIds,
      // 模型选择同款全量下发（§6.10 粘性）：null 也是明确意思——跟随设置默认
      model: get().modelRef,
      // 能力同款（§6.4 粘性）：后端按请求里的值跑本回合并写回会话
      capability: get().capability,
      // 回合参数（§7.6）：档位/模式**只在研究能力下发**——config.mode 是各能力共用的键名，
      // 只发给认得它的能力，免得别的能力拿到不认识的键被判成非法参数。
      // 确认按钮那次再叠一个 research_action（extraConfig 由调用方给）
      config: {
        ...(get().capability === CAPABILITY_RESEARCH
          ? { depth: get().researchDepth, mode: get().researchMode }
          : {}),
        // 可视化同理：render_type 是它独有的键，只在它这档下发（'auto' 也是合法值）
        ...(get().capability === CAPABILITY_VISUALIZE
          ? { render_type: get().visualizeRenderType }
          : {}),
        ...(get().capability === CAPABILITY_MATH_ANIMATOR
          ? { quality: get().animatorQuality }
          : {}),
        ...extraConfig,
      },
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
    const { sessionId, messages } = get();
    if (!sessionId) {
      return;
    }
    // 乐观删除（§6.8 语义对齐）：旧回复先本地切掉，新回合流式期间不残留。
    // 请求被服务端拒（error 帧）/终局重取时都会从服务器拉回真实状态，自愈。
    const sliced = messagesThroughLastUser(messages);
    if (sliced) {
      set(() => ({ messages: sliced }));
    }
    socket.send({ type: "regenerate", session_id: sessionId });
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
