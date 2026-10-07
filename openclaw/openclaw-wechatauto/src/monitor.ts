/**
 * Long-poll loop: bridge /events → normalize → channelRuntime dispatch.
 *
 * Follows the same pipeline shape as @tencent-weixin/openclaw-weixin's
 * monitor + processOneMessage: resolveAgentRoute → finalizeInboundContext →
 * recordInboundSession → createReplyDispatcherWithTyping → dispatchReplyFromConfig.
 */

import type { PluginRuntime } from "openclaw/plugin-sdk/core";
import type { OpenClawConfig } from "openclaw/plugin-sdk/core";

import type { BridgeClient, BridgeEvent } from "./client.js";
import { CHANNEL_ID, eventToMsgContext, stripMention, type WeChatMsgContext } from "./inbound.js";

export type MonitorDeps = {
  client: BridgeClient;
  accountId: string;
  cfg: OpenClawConfig;
  runtime: PluginRuntime;
  channelRuntime: PluginRuntime["channel"];
  abortSignal: AbortSignal;
  setStatus?: (patch: Record<string, unknown>) => void;
  log: (msg: string) => void;
  errLog: (msg: string) => void;
  policy: {
    dmSecurity: string;
    allowFrom: string[];
    groupPolicy: string;
    groupAllowFrom: string[];
    requireMention: boolean;
    selfNick: string;
    selfWxid: string;
  };
  pollTimeoutMs: number;
};

const RETRY_DELAY_MS = 5_000;

export async function monitorWechatLocal(deps: MonitorDeps): Promise<void> {
  let cursor = 0;
  deps.log(`[wechatauto] monitor started (bridge=${deps.client.baseUrl})`);

  while (!deps.abortSignal.aborted) {
    let batch;
    try {
      batch = await deps.client.events(cursor, deps.pollTimeoutMs, deps.abortSignal);
    } catch (err) {
      if (deps.abortSignal.aborted) return;
      deps.errLog(`[wechatauto] poll failed: ${String(err)} — retry in ${RETRY_DELAY_MS}ms`);
      await sleep(RETRY_DELAY_MS, deps.abortSignal);
      continue;
    }
    cursor = batch.cursor;
    for (const { event } of batch.events) {
      try {
        await processOneEvent(event, deps);
      } catch (err) {
        deps.errLog(`[wechatauto] dispatch failed for ${event.id}: ${String(err)}`);
      }
    }
    deps.setStatus?.({ lastEventAt: Date.now() });
  }
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(t);
      resolve();
    }, { once: true });
  });
}

function allowListed(list: string[], ...candidates: Array<string | undefined>): boolean {
  return candidates.some((c) => c && list.includes(c));
}

async function processOneEvent(ev: BridgeEvent, deps: MonitorDeps): Promise<void> {
  if (ev.is_self) return; // 本机发的消息不回环

  const { policy } = deps;
  const isGroup = ev.chat_type === "group";

  // 准入控制（在入站管线前先过本地闸门，省得每条消息都唤醒 agent）
  if (!isGroup) {
    if (policy.dmSecurity === "disabled") return;
    if (policy.dmSecurity === "allowlist" &&
        !allowListed(policy.allowFrom, ev.sender_id, ev.sender_name)) {
      deps.log(`[wechatauto] DM from ${ev.sender_id} not in allowFrom, skipped`);
      return;
    }
    // pairing/open 交给核心配对流程或放行
  } else {
    if (policy.groupPolicy === "disabled") return;
    if (policy.groupPolicy === "allowlist" &&
        !allowListed(policy.groupAllowFrom, ev.chat_id, ev.chat_name)) {
      return;
    }
  }

  // 群聊 @机器人 门控：atuserlist XML 是权威证据（文本 @昵称 可伪造）；
  // 引用我的消息视同@（stardome 生产语义）；都缺席时退回文本兜底。
  let body = ev.text;
  let mentioned = false;
  if (isGroup && policy.requireMention) {
    const quotedSelf =
      ev.quoted?.sender != null && ev.quoted.sender === policy.selfWxid;
    if (Array.isArray(ev.at_usernames)) {
      mentioned = quotedSelf || ev.at_usernames.includes(policy.selfWxid);
    } else {
      const stripped = stripMention(body, policy.selfNick);
      mentioned = quotedSelf || stripped.mentioned;
      body = stripped.body;
    }
    if (!mentioned) return;
    // 命中后仍要剥掉正文里的 @昵称 残留（atuserlist 命中时正文也带 @片段）
    body = stripMention(body, policy.selfNick).body || body;
  }
  if (!body.trim()) return;

  const ctx = eventToMsgContext({ ...ev, text: body }, deps.accountId);
  ctx.CommandBody = body;
  ctx.CommandAuthorized = true; // allowlist/mention 已过闸；精确授权由核心再判

  const route = deps.channelRuntime.routing.resolveAgentRoute({
    cfg: deps.cfg,
    channel: CHANNEL_ID,
    accountId: deps.accountId,
    peer: { kind: isGroup ? "group" : "direct", id: ev.chat_id },
  });
  ctx.SessionKey = route.sessionKey;

  const storePath = deps.channelRuntime.session.resolveStorePath(deps.cfg.session?.store, {
    agentId: route.agentId,
  });
  const finalized = deps.channelRuntime.reply.finalizeInboundContext(
    ctx as Parameters<typeof deps.channelRuntime.reply.finalizeInboundContext>[0],
  );

  await deps.channelRuntime.session.recordInboundSession({
    storePath,
    sessionKey: route.sessionKey,
    ctx: finalized as Parameters<typeof deps.channelRuntime.session.recordInboundSession>[0]["ctx"],
    updateLastRoute: {
      sessionKey: route.mainSessionKey,
      channel: CHANNEL_ID,
      to: ev.chat_id,
      accountId: deps.accountId,
    },
    onRecordError: (err) => deps.errLog(`recordInboundSession: ${String(err)}`),
  });

  const humanDelay = deps.channelRuntime.reply.resolveHumanDelayConfig(deps.cfg, route.agentId);
  const { dispatcher, replyOptions, markDispatchIdle } =
    deps.channelRuntime.reply.createReplyDispatcherWithTyping({
      humanDelay,
      deliver: async (payload: { text?: string; mediaUrl?: string; mediaUrls?: string[] }) => {
        const text = payload.text ?? "";
        const mediaUrl = payload.mediaUrl ?? payload.mediaUrls?.[0];
        if (mediaUrl) {
          const filePath = mediaUrl.startsWith("file://")
            ? new URL(mediaUrl).pathname
            : mediaUrl;
          const isImage = /\.(jpe?g|png|gif|bmp|webp)$/i.test(filePath);
          await deps.client.sendFile(ev.chat_id, filePath, { image: isImage });
          if (text) await deps.client.sendText(ev.chat_id, text);
        } else if (text) {
          await deps.client.sendText(ev.chat_id, text);
        }
      },
      onError: (err: unknown, info: { kind: string }) => {
        deps.errLog(`wechatauto reply ${info.kind}: ${String(err)}`);
      },
    });

  try {
    await deps.channelRuntime.reply.withReplyDispatcher({
      dispatcher,
      run: () =>
        deps.channelRuntime.reply.dispatchReplyFromConfig({
          ctx: finalized,
          cfg: deps.cfg,
          dispatcher,
          replyOptions,
        }),
    });
  } finally {
    markDispatchIdle();
  }
}
