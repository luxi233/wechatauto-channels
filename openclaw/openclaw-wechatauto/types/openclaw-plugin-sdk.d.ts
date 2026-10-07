/**
 * 本地 typecheck 用的最小声明 —— 不是 SDK 真身。
 * 装了 openclaw peer 后删掉本文件即可换真实类型。
 */

declare module "openclaw/plugin-sdk/core" {
  export interface OpenClawConfig {
    channels?: Record<string, unknown>;
    session?: { store?: string };
    [key: string]: unknown;
  }

  export interface PluginRuntime {
    channel: {
      routing: {
        resolveAgentRoute(opts: {
          cfg: OpenClawConfig;
          channel: string;
          accountId: string;
          peer: { kind: "direct" | "group"; id: string };
        }): { agentId?: string; sessionKey?: string; mainSessionKey?: string; accountId: string; dmScope?: string };
      };
      session: {
        resolveStorePath(store: unknown, opts: { agentId?: string }): string;
        recordInboundSession(opts: {
          storePath: string;
          sessionKey?: string;
          ctx: unknown;
          updateLastRoute?: Record<string, unknown>;
          onRecordError?: (err: unknown) => void;
        }): Promise<void>;
      };
      reply: {
        finalizeInboundContext(ctx: unknown): Record<string, unknown>;
        resolveHumanDelayConfig(cfg: OpenClawConfig, agentId?: string): unknown;
        createReplyDispatcherWithTyping(opts: {
          humanDelay?: unknown;
          typingCallbacks?: unknown;
          deliver: (payload: { text?: string; mediaUrl?: string; mediaUrls?: string[] }) => Promise<void>;
          onError?: (err: unknown, info: { kind: string }) => void;
        }): { dispatcher: unknown; replyOptions: Record<string, unknown>; markDispatchIdle: () => void };
        withReplyDispatcher(opts: { dispatcher: unknown; run: () => Promise<unknown> }): Promise<void>;
        dispatchReplyFromConfig(opts: {
          ctx: unknown;
          cfg: OpenClawConfig;
          dispatcher: unknown;
          replyOptions: Record<string, unknown>;
        }): Promise<unknown>;
      };
      [key: string]: unknown;
    };
    [key: string]: unknown;
  }

  export interface ChannelPlugin<TAccount = unknown> {
    id: string;
    meta?: Record<string, unknown>;
    configSchema?: Record<string, unknown>;
    capabilities?: Record<string, unknown>;
    streaming?: Record<string, unknown>;
    messaging?: Record<string, unknown>;
    agentPrompt?: { messageToolHints?: () => string[] };
    reload?: { configPrefixes?: string[] };
    config?: Record<string, unknown>;
    outbound?: Record<string, unknown>;
    gateway?: Record<string, unknown>;
    setup?: Record<string, unknown>;
    [key: string]: unknown;
  }
}

declare module "openclaw/plugin-sdk/channel-core" {
  import type { ChannelPlugin, OpenClawConfig } from "openclaw/plugin-sdk/core";

  export function createChatChannelPlugin<T>(opts: Record<string, unknown>): ChannelPlugin<T>;
  export function createChannelPluginBase(opts: Record<string, unknown>): Record<string, unknown>;
  export function defineChannelPluginEntry(opts: Record<string, unknown>): unknown;
  export function defineSetupPluginEntry(plugin: ChannelPlugin): unknown;
}
