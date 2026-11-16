export const settingsRoutes = {
  /** `/settings`. */
  settings: (): string => "/settings",
  /** `/settings#api-keys`: the API keys section (AGUI.13). */
  /** `/settings/providers`: the model providers the server can reach (read-only). */
  settingsProviders: (): string => "/settings/providers",
  settingsApiKeys: (): string => "/settings#api-keys",
};
