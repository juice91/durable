export const SETTINGS_TABS = ["appearance", "chat", "shortcuts", "providers", "secrets", "connections", "api-keys", "browser-data", "about"] as const;
export type SettingsTab = (typeof SETTINGS_TABS)[number];

export const settingsRoutes = {
  /** `/settings`: the Appearance tab. */
  settings: (): string => "/settings",
  /** `/settings?tab=<tab>`: one Settings tab (the selected tab lives in the URL). */
  settingsTab: (tab: SettingsTab): string => `/settings?tab=${tab}`,
  /** `/settings?tab=providers`: the model providers the server can reach (read-only). */
  settingsProviders: (): string => "/settings?tab=providers",
  /** `/settings?tab=api-keys`: the API keys tab (AGUI.13). */
  settingsApiKeys: (): string => "/settings?tab=api-keys",
};
