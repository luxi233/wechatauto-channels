import { defineChannelPluginEntry } from "openclaw/plugin-sdk/channel-core";

import { wechatautoPlugin } from "./src/channel.js";
import { CHANNEL_ID } from "./src/inbound.js";

export default defineChannelPluginEntry({
  id: CHANNEL_ID,
  name: "WeChat Local",
  description:
    "Personal WeChat 4.x channel via the local wechatauto-channels bridge (wechatauto-replica): DMs + groups.",
  plugin: wechatautoPlugin,
  registerCliMetadata(api: any) {
    api.registerCli(
      ({ program }: any) => {
        program
          .command("wechatauto")
          .description("WeChat Local channel management")
          .command("status")
          .description("Check bridge health")
          .action(async () => {
            // 轻量健康检查：读配置→打 /health
            const { loadConfig } = await import("./src/cli-status.js");
            await loadConfig();
          });
      },
      {
        descriptors: [
          {
            name: "wechatauto",
            description: "WeChat Local channel management",
            hasSubcommands: true,
          },
        ],
      },
    );
  },
});
