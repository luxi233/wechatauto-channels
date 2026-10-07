import { defineSetupPluginEntry } from "openclaw/plugin-sdk/channel-core";

import { wechatautoPlugin } from "./src/channel.js";

export default defineSetupPluginEntry(wechatautoPlugin);
