/**
 * wechatauto ChannelPlugin — OpenClaw 渠道插件对象。
 *
 * 结构参照 @tencent-weixin/openclaw-weixin：账号=bridge 实例
 * （一台 Windows 机器一个已登录微信），出站经 bridge HTTP，入站经长轮询。
 */

import type {
  ChannelPlugin,
  OpenClawConfig,
} from "openclaw/plugin-sdk/core";

import { BridgeClient } from "./client.js";
import { CHANNEL_ID } from "./inbound.js";

export type ResolvedWeChatLocalAccount = {
  accountId: string;
  enabled: boolean;
  configured: boolean;
  bridgeUrl: string;
  bridgeToken: string;
  allowFrom: string[];
  dmSecurity: string;
  groupPolicy: string;
  groupAllowFrom: string[];
  requireMention: boolean;
  pollTimeoutMs: number;
};

const DEFAULT_BRIDGE_URL = "http://127.0.0.1:18765";
const DEFAULT_ACCOUNT_ID = "default";

type ChannelSection = {
  enabled?: boolean;
  bridgeUrl?: string;
  bridgeToken?: string;
  allowFrom?: string[];
  dmSecurity?: string;
  groupPolicy?: string;
  groupAllowFrom?: string[];
  requireMention?: boolean;
  pollTimeoutMs?: number;
  accounts?: Record<string, ChannelSection>;
};

function section(cfg: OpenClawConfig): ChannelSection {
  return ((cfg.channels as Record<string, unknown>)?.[CHANNEL_ID] ??
    {}) as ChannelSection;
}

function sectionFor(cfg: OpenClawConfig, accountId?: string | null): ChannelSection {
  const sec = section(cfg);
  if (accountId && accountId !== DEFAULT_ACCOUNT_ID && sec.accounts?.[accountId]) {
    return { ...sec, ...sec.accounts[accountId] };
  }
  return sec;
}

export function resolveAccount(
  cfg: OpenClawConfig,
  accountId?: string | null,
): ResolvedWeChatLocalAccount {
  const sec = sectionFor(cfg, accountId);
  const bridgeUrl = (sec.bridgeUrl ?? DEFAULT_BRIDGE_URL).replace(/\/+$/, "");
  return {
    accountId: accountId ?? DEFAULT_ACCOUNT_ID,
    enabled: sec.enabled !== false,
    configured: Boolean(bridgeUrl),
    bridgeUrl,
    bridgeToken: sec.bridgeToken ?? "",
    allowFrom: sec.allowFrom ?? [],
    dmSecurity: sec.dmSecurity ?? "pairing",
    groupPolicy: sec.groupPolicy ?? "allowlist",
    groupAllowFrom: sec.groupAllowFrom ?? [],
    requireMention: sec.requireMention !== false,
    pollTimeoutMs: sec.pollTimeoutMs ?? 30_000,
  };
}

export function listAccountIds(cfg: OpenClawConfig): string[] {
  const sec = section(cfg);
  const ids = Object.keys(sec.accounts ?? {});
  return [DEFAULT_ACCOUNT_ID, ...ids.filter((i) => i !== DEFAULT_ACCOUNT_ID)];
}

function clientFor(account: ResolvedWeChatLocalAccount): BridgeClient {
  return new BridgeClient(account.bridgeUrl, account.bridgeToken);
}

export const wechatautoPlugin: ChannelPlugin<ResolvedWeChatLocalAccount> = {
  id: CHANNEL_ID,
  meta: {
    id: CHANNEL_ID,
    label: "WeChat Local",
    selectionLabel: "WeChat Local (wechatauto bridge)",
    docsPath: "/channels/wechatauto",
    docsLabel: "wechatauto",
    blurb: "Local WeChat 4.x via wechatauto-channels bridge — DMs and groups.",
    order: 76,
  },
  capabilities: {
    chatTypes: ["direct", "group"],
    media: true,
    blockStreaming: false,
  },
  messaging: {
    targetResolver: {
      // wxid_xxx / xxx@chatroom / filehelper 都是直接 ID，跳过目录查找
      looksLikeId: (raw: string) =>
        /^wxid_/.test(raw) || /@chatroom$/.test(raw) || raw === "filehelper",
    },
  },
  agentPrompt: {
    messageToolHints: () => [
      "This channel is plain-text WeChat. Markdown is not rendered — keep replies in natural prose.",
      "To send an image or file, use the message tool with action='send' and set 'media' to an absolute local path (e.g. D:\\work\\photo.png). The bridge runs on the same Windows host as WeChat.",
      "In group chats, recipients are @-mentioned by display name, e.g. '@Alice'.",
      "For cron delivery, set delivery.to to the chat display name or wxid/chatroom id, and delivery.accountId to 'default'.",
    ],
  },
  reload: { configPrefixes: ["channels.wechatauto"] },
  config: {
    listAccountIds,
    resolveAccount,
    isConfigured: (account: ResolvedWeChatLocalAccount) => account.configured,
    describeAccount: (account: ResolvedWeChatLocalAccount) => ({
      accountId: account.accountId,
      name: "WeChat Local bridge",
      enabled: account.enabled,
      configured: account.configured,
    }),
  },
  outbound: {
    deliveryMode: "direct",
    textChunkLimit: 4000,
    sendText: async (ctx: {
      cfg: OpenClawConfig;
      to: string;
      text: string;
      accountId?: string | null;
    }) => {
      const account = resolveAccount(ctx.cfg, ctx.accountId);
      const client = clientFor(account);
      const result = await client.sendText(ctx.to, ctx.text ?? "");
      return { channel: CHANNEL_ID, messageId: result.messageId ?? "" };
    },
    sendMedia: async (ctx: {
      cfg: OpenClawConfig;
      to: string;
      text?: string;
      mediaUrl?: string;
      accountId?: string | null;
    }) => {
      const account = resolveAccount(ctx.cfg, ctx.accountId);
      const client = clientFor(account);
      const mediaUrl = ctx.mediaUrl ?? "";
      const filePath = mediaUrl.startsWith("file://")
        ? new URL(mediaUrl).pathname
        : mediaUrl;
      if (!filePath || filePath.includes("://")) {
        throw new Error(
          "wechatauto: remote media URLs are not supported by the local bridge — pass a local path",
        );
      }
      const isImage = /\.(jpe?g|png|gif|bmp|webp)$/i.test(filePath);
      const result = await client.sendFile(ctx.to, filePath, { image: isImage });
      if (ctx.text) {
        await client.sendText(ctx.to, ctx.text);
      }
      return { channel: CHANNEL_ID, messageId: result.messageId ?? "" };
    },
  },
  gateway: {
    startAccount: async (ctx: any) => {
      const account = resolveAccount(ctx.cfg, ctx.account?.accountId);
      const client = clientFor(account);

      ctx.setStatus?.({
        accountId: account.accountId,
        running: true,
        lastStartAt: Date.now(),
      });

      let health;
      try {
        health = await client.health();
      } catch (err) {
        ctx.setStatus?.({ accountId: account.accountId, running: false });
        throw new Error(
          `wechatauto: bridge unreachable at ${account.bridgeUrl} — ` +
            `start it on the WeChat host: python -m wechatauto_channels.bridge ` +
            `(${String(err)})`,
        );
      }
      ctx.log?.info?.(
        `[${account.accountId}] bridge up: wxid=${health.wxid} nick=${health.nickname} media=${health.media}`,
      );

      if (!ctx.channelRuntime) {
        const msg = "ctx.channelRuntime missing — host too old or plugin SDK contract violated";
        ctx.log?.error?.(`[${account.accountId}] ${msg}`);
        ctx.setStatus?.({ accountId: account.accountId, running: false });
        throw new Error(msg);
      }

      const { monitorWechatLocal } = await import("./monitor.js");
      return monitorWechatLocal({
        client,
        accountId: account.accountId,
        cfg: ctx.cfg,
        runtime: ctx.runtime,
        channelRuntime: ctx.channelRuntime as PluginRuntimeChannel,
        abortSignal: ctx.abortSignal,
        setStatus: ctx.setStatus,
        log: (m) => ctx.log?.info?.(`[${account.accountId}] ${m}`),
        errLog: (m) => ctx.log?.error?.(`[${account.accountId}] ${m}`),
        policy: {
          dmSecurity: account.dmSecurity,
          allowFrom: account.allowFrom,
          groupPolicy: account.groupPolicy,
          groupAllowFrom: account.groupAllowFrom,
          requireMention: account.requireMention,
          selfNick: health.nickname,
          selfWxid: health.wxid,
        },
        pollTimeoutMs: account.pollTimeoutMs,
      });
    },
    stopAccount: async (ctx: any) => {
      ctx.log?.info?.(`[${ctx.account?.accountId ?? "?"}] wechatauto stopped`);
    },
  },
};

// 类型别名，避免在签名处展开 PluginRuntime["channel"]
import type { PluginRuntime } from "openclaw/plugin-sdk/core";
type PluginRuntimeChannel = PluginRuntime["channel"];
