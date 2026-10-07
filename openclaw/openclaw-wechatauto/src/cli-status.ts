/** `openclaw wechatauto status` — 打 bridge /health 的轻量健康检查。 */

import { BridgeClient } from "./client.js";

const DEFAULT_BRIDGE_URL = "http://127.0.0.1:18765";

export async function loadConfig(): Promise<void> {
  const url = process.env.WECHATAUTO_BRIDGE_URL ?? DEFAULT_BRIDGE_URL;
  const token = process.env.WECHATAUTO_BRIDGE_TOKEN ?? "";
  const client = new BridgeClient(url, token);
  try {
    const health = await client.health();
    console.log(
      `bridge OK  wxid=${health.wxid}  nick=${health.nickname}  media=${health.media}`,
    );
  } catch (err) {
    console.error(
      `bridge unreachable at ${url}: ${String(err)}\n` +
        `start it on the WeChat host: python -m wechatauto_channels.bridge --port ${new URL(url).port}`,
    );
    process.exitCode = 1;
  }
}
