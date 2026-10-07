/**
 * Normalized ChannelEvent (bridge) → OpenClaw MsgContext.
 *
 * Shape mirrors the official @tencent-weixin/openclaw-weixin plugin
 * (src/messaging/inbound.ts) so finalizeInboundContext / dispatchReplyFromConfig
 * treat it identically; group fields follow the built-in IRC channel.
 */

import type { BridgeEvent } from "./client.js";

export const CHANNEL_ID = "wechatauto";

export type WeChatMsgContext = {
  Body: string;
  From: string;
  To: string;
  AccountId: string;
  OriginatingChannel: typeof CHANNEL_ID;
  OriginatingTo: string;
  MessageSid: string;
  MessageSidFull?: string;
  Timestamp?: number;
  Provider: typeof CHANNEL_ID;
  ChatType: "direct" | "group";
  GroupSubject?: string;
  SenderId?: string;
  SenderName?: string;
  /** Set after resolveAgentRoute so dispatchReplyFromConfig uses the resolved session. */
  SessionKey?: string;
  MediaPath?: string;
  MediaType?: string;
  CommandBody?: string;
  CommandAuthorized?: boolean;
};

let sidCounter = 0;

function messageSid(): string {
  return `${CHANNEL_ID}-${Date.now().toString(36)}-${(sidCounter++).toString(36)}`;
}

function mediaMime(type: string): string {
  switch (type) {
    case "image":
      return "image/*";
    case "voice":
      return "audio/*";
    case "video":
      return "video/*";
    default:
      return "application/octet-stream";
  }
}

export function eventToMsgContext(
  ev: BridgeEvent,
  accountId: string,
  opts?: { wasMentioned?: boolean },
): WeChatMsgContext {
  const isGroup = ev.chat_type === "group";
  const ctx: WeChatMsgContext = {
    Body: ev.text,
    // From = 会话标识（group=chatroom id）；To = 回复目标（DM=对方，群=群）
    From: isGroup ? ev.chat_id : ev.sender_id || ev.chat_id,
    To: ev.chat_id,
    AccountId: accountId,
    OriginatingChannel: CHANNEL_ID,
    OriginatingTo: ev.chat_id,
    MessageSid: messageSid(),
    MessageSidFull: ev.id,
    Timestamp: Math.round((ev.timestamp || Date.now() / 1000) * 1000),
    Provider: CHANNEL_ID,
    ChatType: isGroup ? "group" : "direct",
  };
  if (isGroup) {
    ctx.GroupSubject = ev.chat_name || ev.chat_id;
    ctx.SenderId = ev.sender_id;
    ctx.SenderName = ev.sender_name;
  }
  if (ev.media_path) {
    ctx.MediaPath = ev.media_path;
    ctx.MediaType = mediaMime(ev.type);
  }
  return ctx;
}

/** 群内 @机器人 检测：@昵称 后跟 U+2005/U+2006/空白；命中后从正文剥掉。 */
export function stripMention(text: string, selfNick: string): { mentioned: boolean; body: string } {
  if (!selfNick) return { mentioned: false, body: text };
  const marker = `@${selfNick}`;
  if (!text.includes(marker)) return { mentioned: false, body: text };
  const body = text
    .replace(new RegExp(`@${escapeRegExp(selfNick)}[\\u2005\\u2006 \\t]*`, "g"), "")
    .trim();
  return { mentioned: true, body };
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
