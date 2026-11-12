import { describe, it, expect, beforeEach, afterEach } from "vitest";

describe("grafana link builders", () => {
  let originalEnv: string | undefined;

  beforeEach(() => {
    originalEnv = process.env.NEXT_PUBLIC_GRAFANA_URL;
  });

  afterEach(() => {
    if (originalEnv) {
      process.env.NEXT_PUBLIC_GRAFANA_URL = originalEnv;
    } else {
      delete process.env.NEXT_PUBLIC_GRAFANA_URL;
    }
  });

  it("builds trace link with Tempo datasource and default URL", async () => {
    delete process.env.NEXT_PUBLIC_GRAFANA_URL;
    const { buildTraceLink } = await import("./grafana.ts");
    const traceId = "abc123";
    const link = buildTraceLink(traceId);
    expect(link).to.include("http://localhost:3002/explore?schemaVersion=1&panes=");
    const panesPart = link.split("panes=")[1];
    const decoded = JSON.parse(decodeURIComponent(panesPart));
    expect(decoded.a.datasource).toBe("tempo");
    expect(decoded.a.queries[0].datasource.type).toBe("tempo");
    expect(decoded.a.queries[0].datasource.uid).toBe("tempo");
    expect(decoded.a.queries[0].queryType).toBe("traceql");
    expect(decoded.a.queries[0].query).toBe(traceId);
    expect(decoded.a.range.from).toBe("now-24h");
    expect(decoded.a.range.to).toBe("now");
  });

  it("builds trace link with custom Grafana URL", async () => {
    process.env.NEXT_PUBLIC_GRAFANA_URL = "http://grafana.example.com";
    const { buildTraceLink } = await import("./grafana.ts");
    const link = buildTraceLink("xyz789");
    expect(link).to.include("http://grafana.example.com/explore?");
  });

  it("builds logs link with Loki datasource and default URL", async () => {
    delete process.env.NEXT_PUBLIC_GRAFANA_URL;
    const { buildLogsLink } = await import("./grafana.ts");
    const traceId = "def456";
    const link = buildLogsLink(traceId);
    expect(link).to.include("http://localhost:3002/explore?schemaVersion=1&panes=");
    const panesPart = link.split("panes=")[1];
    const decoded = JSON.parse(decodeURIComponent(panesPart));
    expect(decoded.a.datasource).toBe("loki");
    expect(decoded.a.queries[0].datasource.type).toBe("loki");
    expect(decoded.a.queries[0].datasource.uid).toBe("loki");
    expect(decoded.a.queries[0].queryType).toBe("logql");
    expect(decoded.a.queries[0].query).toBe('{service_name=~"eap-.*"} |= "def456"');
  });

  it("builds logs link with custom Grafana URL", async () => {
    process.env.NEXT_PUBLIC_GRAFANA_URL = "https://my-grafana:3000";
    const { buildLogsLink } = await import("./grafana.ts");
    const link = buildLogsLink("ghi789");
    expect(link).to.include("https://my-grafana:3000/explore?");
  });

  it("builds all platform logs link with Loki datasource", async () => {
    delete process.env.NEXT_PUBLIC_GRAFANA_URL;
    const { buildAllPlatformLogsLink } = await import("./grafana.ts");
    const link = buildAllPlatformLogsLink();
    expect(link).to.include("http://localhost:3002/explore?schemaVersion=1&panes=");
    const panesPart = link.split("panes=")[1];
    const decoded = JSON.parse(decodeURIComponent(panesPart));
    expect(decoded.a.datasource).toBe("loki");
    expect(decoded.a.queries[0].datasource.type).toBe("loki");
    expect(decoded.a.queries[0].datasource.uid).toBe("loki");
    expect(decoded.a.queries[0].queryType).toBe("logql");
    expect(decoded.a.queries[0].query).toBe('{service_name=~"eap-.*"}');
  });

  it("handles trace IDs with special characters in logs query", async () => {
    delete process.env.NEXT_PUBLIC_GRAFANA_URL;
    const { buildLogsLink } = await import("./grafana.ts");
    const traceId = "abc/123:def";
    const link = buildLogsLink(traceId);
    const panesPart = link.split("panes=")[1];
    const decoded = JSON.parse(decodeURIComponent(panesPart));
    expect(decoded.a.queries[0].query).toBe('{service_name=~"eap-.*"} |= "abc/123:def"');
  });
});
