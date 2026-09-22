/** WebSocket 客户端（§7.20）：自动重连（指数退避）+ 心跳看护 + resume 断点补发 + 离线排队。 */

import type { StreamEventEnvelope } from "@/types/stream";

export type SocketStatus = "connecting" | "open" | "reconnecting" | "closed";

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 15000;
const HEARTBEAT_TIMEOUT_MS = 60_000; // 60s 没收到任何消息就当死连接（服务器 30s 心跳）
const OUTBOX_MAX = 20; // 离线排队上限，防无界增长

export class ChatSocket {
  private ws: WebSocket | null = null;
  private manuallyClosed = false;
  private reconnectAttempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private lastReceivedAt = 0;
  private lastSeq = 0;
  private turnId = "";
  private sessionId: string | null = null;
  private outbox: Record<string, unknown>[] = [];
  private eventListeners = new Set<(env: StreamEventEnvelope) => void>();
  private statusListeners = new Set<(status: SocketStatus) => void>();
  private currentStatus: SocketStatus = "closed";

  get status(): SocketStatus {
    return this.currentStatus;
  }

  onEvent(listener: (env: StreamEventEnvelope) => void): () => void {
    this.eventListeners.add(listener);
    return () => this.eventListeners.delete(listener);
  }

  onStatus(listener: (status: SocketStatus) => void): () => void {
    this.statusListeners.add(listener);
    listener(this.currentStatus);
    return () => this.statusListeners.delete(listener);
  }

  private setStatus(status: SocketStatus): void {
    if (this.currentStatus === status) {
      return;
    }
    this.currentStatus = status;
    for (const listener of this.statusListeners) {
      listener(status);
    }
  }

  /** 连接（幂等：已连/正在连时无操作）；sessionId 供重连后 resume 用。 */
  connect(sessionId: string | null = null): void {
    this.sessionId = sessionId;
    if (
      this.ws &&
      (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }
    this.manuallyClosed = false;
    this.open();
  }

  /** 切换当前会话（影响重连时的 resume 目标）。 */
  setSession(sessionId: string | null): void {
    this.sessionId = sessionId;
  }

  send(message: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
      return;
    }
    // 离线排队：重连成功后按序补发（chat/stop/regenerate 等；ping 不值得排队）
    if (message.type !== "ping" && this.outbox.length < OUTBOX_MAX) {
      this.outbox.push(message);
    }
  }

  close(): void {
    this.manuallyClosed = true;
    this.clearTimers();
    this.ws?.close();
    this.ws = null;
    this.setStatus("closed");
  }

  private clearTimers(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private open(): void {
    this.clearTimers();
    this.setStatus(this.reconnectAttempt === 0 ? "connecting" : "reconnecting");
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${protocol}://${window.location.host}/api/v1/ws`);
    this.ws = ws;

    ws.onopen = () => {
      this.reconnectAttempt = 0;
      this.setStatus("open");
      // 断点补发（§7.20）：回合中断线 → 按本回合已收到的 seq 重放
      if (this.sessionId && this.lastSeq > 0) {
        ws.send(
          JSON.stringify({ type: "resume", session_id: this.sessionId, after_seq: this.lastSeq })
        );
      }
      while (this.outbox.length > 0) {
        ws.send(JSON.stringify(this.outbox.shift()));
      }
    };
    ws.onmessage = (event: MessageEvent<string>) => {
      this.lastReceivedAt = Date.now();
      let env: StreamEventEnvelope;
      try {
        env = JSON.parse(event.data) as StreamEventEnvelope;
      } catch {
        return;
      }
      // seq 按回合从 1 起（§7.20）：换回合清零重算，回合终局不再 resume
      if (env.turn_id && env.turn_id !== this.turnId) {
        this.turnId = env.turn_id;
        this.lastSeq = 0;
      }
      if (env.seq > 0) {
        this.lastSeq = env.seq;
      }
      if (env.type === "done" || env.type === "error" || env.type === "stopped") {
        this.turnId = "";
        this.lastSeq = 0;
      }
      for (const listener of this.eventListeners) {
        listener(env);
      }
    };
    ws.onclose = () => {
      this.setStatus(this.manuallyClosed ? "closed" : "reconnecting");
      if (!this.manuallyClosed) {
        this.scheduleReconnect();
      }
    };
    ws.onerror = () => {
      // 不直接处理：close 事件随后到达，统一走重连
    };
    // 心跳看护：超时无消息 → 主动断开，触发重连
    this.heartbeatTimer = setInterval(() => {
      if (this.lastReceivedAt > 0 && Date.now() - this.lastReceivedAt > HEARTBEAT_TIMEOUT_MS) {
        ws.close();
      }
    }, 15_000);
  }

  private scheduleReconnect(): void {
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** this.reconnectAttempt, RECONNECT_MAX_MS);
    this.reconnectAttempt += 1;
    this.reconnectTimer = setTimeout(() => this.open(), delay);
  }
}
