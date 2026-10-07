import { describe, expect, it } from "vitest";

import { listAccountIds, resolveAccount } from "./channel.js";
import { eventToMsgContext, stripMention } from "./inbound.js";
import type { BridgeEvent } from "./client.js";

const baseCfg = {
  channels: {
    "wechatauto": {
      bridgeUrl: "http://127.0.0.1:19001/",
      bridgeToken: "tok",
      allowFrom: ["wxid_friend"],
      groupAllowFrom: ["项目群"],
    },
  },
} as any;

describe("resolveAccount", () => {
  it("reads the channel section and normalizes bridgeUrl", () => {
    const acc = resolveAccount(baseCfg, undefined);
    expect(acc.bridgeUrl).toBe("http://127.0.0.1:19001");
    expect(acc.bridgeToken).toBe("tok");
    expect(acc.allowFrom).toEqual(["wxid_friend"]);
    expect(acc.dmSecurity).toBe("pairing");
    expect(acc.requireMention).toBe(true);
  });

  it("falls back to defaults when unconfigured", () => {
    const acc = resolveAccount({ channels: {} } as any, undefined);
    expect(acc.bridgeUrl).toBe("http://127.0.0.1:18765");
    expect(acc.configured).toBe(true);
    expect(acc.groupPolicy).toBe("allowlist");
  });

  it("merges named accounts over the channel section", () => {
    const cfg = {
      channels: {
        "wechatauto": {
          bridgeToken: "top",
          accounts: { work: { bridgeUrl: "http://127.0.0.1:22222", bridgeToken: "work-tok" } },
        },
      },
    } as any;
    const acc = resolveAccount(cfg, "work");
    expect(acc.bridgeUrl).toBe("http://127.0.0.1:22222");
    expect(acc.bridgeToken).toBe("work-tok");
    expect(listAccountIds(cfg)).toEqual(["default", "work"]);
  });
});

describe("eventToMsgContext", () => {
  const ev: BridgeEvent = {
    id: "wxid_a:42",
    chat_id: "wxid_a",
    chat_type: "dm",
    chat_name: "Alice",
    sender_id: "wxid_a",
    sender_name: "Alice",
    is_self: false,
    type: "text",
    text: "hello",
    timestamp: 1700000000,
    local_id: 42,
    sort_seq: 4200,
  };

  it("maps a DM event", () => {
    const ctx = eventToMsgContext(ev, "default");
    expect(ctx.ChatType).toBe("direct");
    expect(ctx.To).toBe("wxid_a");
    expect(ctx.From).toBe("wxid_a");
    expect(ctx.MessageSidFull).toBe("wxid_a:42");
  });

  it("maps a group event with sender + subject", () => {
    const g: BridgeEvent = {
      ...ev,
      chat_id: "12345@chatroom",
      chat_type: "group",
      chat_name: "项目群",
      sender_id: "wxid_b",
      sender_name: "Bob",
    };
    const ctx = eventToMsgContext(g, "default");
    expect(ctx.ChatType).toBe("group");
    expect(ctx.To).toBe("12345@chatroom");
    expect(ctx.GroupSubject).toBe("项目群");
    expect(ctx.SenderId).toBe("wxid_b");
    expect(ctx.SenderName).toBe("Bob");
  });

  it("attaches local media path", () => {
    const ctx = eventToMsgContext(
      { ...ev, type: "image", media_path: "D:\\wx\\img1.jpg" },
      "default",
    );
    expect(ctx.MediaPath).toBe("D:\\wx\\img1.jpg");
    expect(ctx.MediaType).toBe("image/*");
  });
});

describe("stripMention", () => {
  it("detects and strips @nick with U+2005 tail", () => {
    const { mentioned, body } = stripMention("@小助手 在吗", "小助手");
    expect(mentioned).toBe(true);
    expect(body).toBe("在吗");
  });

  it("no mention without @nick", () => {
    expect(stripMention("随便聊聊", "小助手").mentioned).toBe(false);
  });
});
