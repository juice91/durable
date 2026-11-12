function getGrafanaUrl(): string {
  return process.env.NEXT_PUBLIC_GRAFANA_URL || "http://localhost:3002";
}

export function buildTraceLink(traceId: string): string {
  const query = {
    schemaVersion: 1,
    panes: JSON.stringify({
      a: {
        datasource: "tempo",
        queries: [
          {
            refId: "A",
            datasource: { type: "tempo", uid: "tempo" },
            queryType: "traceql",
            query: traceId,
          },
        ],
        range: { from: "now-24h", to: "now" },
      },
    }),
  };
  return `${getGrafanaUrl()}/explore?schemaVersion=1&panes=${encodeURIComponent(query.panes)}`;
}

export function buildLogsLink(traceId: string): string {
  const query = {
    schemaVersion: 1,
    panes: JSON.stringify({
      a: {
        datasource: "loki",
        queries: [
          {
            refId: "A",
            datasource: { type: "loki", uid: "loki" },
            queryType: "logql",
            query: `{service_name=~"eap-.*"} |= "${traceId}"`,
          },
        ],
        range: { from: "now-24h", to: "now" },
      },
    }),
  };
  return `${getGrafanaUrl()}/explore?schemaVersion=1&panes=${encodeURIComponent(query.panes)}`;
}

export function buildAllPlatformLogsLink(): string {
  const query = {
    schemaVersion: 1,
    panes: JSON.stringify({
      a: {
        datasource: "loki",
        queries: [
          {
            refId: "A",
            datasource: { type: "loki", uid: "loki" },
            queryType: "logql",
            query: `{service_name=~"eap-.*"}`,
          },
        ],
        range: { from: "now-24h", to: "now" },
      },
    }),
  };
  return `${getGrafanaUrl()}/explore?schemaVersion=1&panes=${encodeURIComponent(query.panes)}`;
}
