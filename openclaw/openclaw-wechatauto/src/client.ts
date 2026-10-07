/**
 * HTTP client for the wechatauto-channels Python bridge.
 *
 * The bridge wraps wechatauto-replica on the same Windows host:
 *   GET  /health /chats /chat /resolve /events?cursor&timeout_ms
 *   POST /send /send_file
 */

export type BridgeEvent = {
  id: string;
  chat_id: string;
  chat_type: "dm" | "group";
  chat_name: string;
  sender_id: string;
  sender_name: string;
  is_self: boolean;
  type: string;
  text: string;
  timestamp: number;
  local_id: number;
  sort_seq: number;
  media_path?: string | null;
};

export type BridgeEventsResponse = {
  cursor: number;
  events: Array<{ seq: number; event: BridgeEvent }>;
};

export class BridgeError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "BridgeError";
  }
}

export class BridgeClient {
  constructor(
    readonly baseUrl: string,
    readonly token: string = "",
  ) {}

  private headers(): Record<string, string> {
    return this.token ? { Authorization: `Bearer ${this.token}` } : {};
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: {
        ...this.headers(),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const payload = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    if (!res.ok) {
      throw new BridgeError(
        typeof payload.error === "string" ? payload.error : `HTTP ${res.status}`,
        res.status,
      );
    }
    return payload as T;
  }

  async health(): Promise<{ ok: boolean; wxid: string; nickname: string; media: boolean }> {
    return this.request("GET", "/health");
  }

  async chats(): Promise<Array<{ id: string; type: string; name: string; unread: number }>> {
    const r = await this.request<{ chats: Array<{ id: string; type: string; name: string; unread: number }> }>(
      "GET", "/chats");
    return r.chats;
  }

  async resolve(handle: string): Promise<string | null> {
    const r = await this.request<{ username: string | null }>(
      "GET", `/resolve?handle=${encodeURIComponent(handle)}`);
    return r.username;
  }

  /** Long-poll: resolves after `timeoutMs` at the latest. Caller supplies AbortSignal. */
  async events(
    cursor: number,
    timeoutMs: number,
    signal?: AbortSignal,
  ): Promise<BridgeEventsResponse> {
    const res = await fetch(
      `${this.baseUrl}/events?cursor=${cursor}&timeout_ms=${timeoutMs}`,
      { headers: this.headers(), signal },
    );
    if (!res.ok) throw new BridgeError(`events HTTP ${res.status}`, res.status);
    return (await res.json()) as BridgeEventsResponse;
  }

  async sendText(to: string, text: string): Promise<{ ok: boolean; messageId?: string }> {
    const r = await this.request<{ ok: boolean; results?: Array<{ to?: string }> }>(
      "POST", "/send", { to, text });
    const target = r.results?.[r.results.length - 1]?.to ?? to;
    return { ok: r.ok, messageId: `wechatauto:${target}:${Date.now()}` };
  }

  async sendFile(
    to: string,
    filePath: string,
    opts?: { image?: boolean },
  ): Promise<{ ok: boolean; messageId?: string }> {
    const r = await this.request<{ ok: boolean; to?: string }>(
      "POST", "/send_file", { to, path: filePath, image: opts?.image ?? false });
    return { ok: r.ok, messageId: `wechatauto:${r.to ?? to}:${Date.now()}` };
  }
}
