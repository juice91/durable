import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  applyDecision,
  approvalFiltersToParams,
  approvalId,
  approvalLinks,
  approvalsQuery,
  decideToolApproval,
  findApproval,
  getApproval,
  isDecidable,
  EMPTY_APPROVAL_FILTERS,
  listApprovals,
  parseApprovalFilters,
  parseApprovalId,
  pendingBadgeLabel,
  PENDING_POLL_MS,
  pendingSnapshotForTests,
  raceNotice,
  refreshPendingApprovals,
  resetPendingApprovalsForTests,
  type Approval,
} from "./approvals";
import { routes } from "./routes";

const RUN = "11111111-2222-3333-4444-555555555555";
const ROOT = "99999999-2222-3333-4444-555555555555";
const AGENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";

export function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    run_id: RUN,
    kind: "tool",
    approval_key: "call_1",
    tool_call_id: "call_1",
    step_id: null,
    root_run_id: RUN,
    agent_id: AGENT,
    workflow_id: null,
    tool_name: "send_email",
    args: { to: "a@example.com" },
    prompt: null,
    status: "pending",
    requested_at: "2026-11-15T10:00:00+00:00",
    deadline: "2026-11-16T10:00:00+00:00",
    decided_at: null,
    decision: null,
    decided_by: null,
    run_status: "running",
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("approval filters and ids", () => {
  it("parses URL filters with safe defaults", () => {
    expect(parseApprovalFilters(new URLSearchParams())).toEqual(EMPTY_APPROVAL_FILTERS);
    expect(parseApprovalFilters(new URLSearchParams("status=decided&kind=step&agent=A"))).toEqual({ status: "decided", kind: "step", agent_id: "A" });
    expect(parseApprovalFilters(new URLSearchParams("status=bogus&kind=nope&agent=%20"))).toEqual(EMPTY_APPROVAL_FILTERS);
  });

  it("writes only non-default filters back to the URL", () => {
    expect(approvalFiltersToParams(EMPTY_APPROVAL_FILTERS).toString()).toBe("");
    expect(approvalFiltersToParams({ status: "decided", kind: "tool", agent_id: "A" }).toString()).toBe("status=decided&kind=tool&agent=A");
  });

  it("builds the API query (agent_id, cursor, limit)", () => {
    expect(approvalsQuery({ status: "pending", kind: "tool", agent_id: "A" }, { cursor: "c1", limit: 10 }).toString()).toBe(
      "status=pending&kind=tool&agent_id=A&cursor=c1&limit=10",
    );
  });

  it("round-trips approval ids, splitting at the first colon", () => {
    expect(approvalId(approval({ approval_key: "a:b" }))).toBe(`${RUN}:a:b`);
    expect(parseApprovalId(`${RUN}:a:b`)).toEqual({ runId: RUN, key: "a:b" });
    expect(parseApprovalId("nocolon")).toBeNull();
    expect(parseApprovalId(":x")).toBeNull();
    expect(parseApprovalId("x:")).toBeNull();
    expect(parseApprovalId(null)).toBeNull();
  });

  it("is decidable only while pending on a run that has not ended", () => {
    expect(isDecidable(approval())).toBe(true);
    expect(isDecidable(approval({ run_status: "failed" }))).toBe(false);
    expect(isDecidable(approval({ status: "decided", decision: "approved" }))).toBe(false);
  });

  it("applies a decision to a row", () => {
    const row = applyDecision(approval(), "denied", { decidedAt: "2026-11-15T11:00:00Z", decidedBy: "u1" });
    expect(row).toMatchObject({ status: "decided", decision: "denied", decided_at: "2026-11-15T11:00:00Z", decided_by: "u1" });
  });
});

describe("approval links", () => {
  it("links a plain agent tool call to its conversation", () => {
    expect(approvalLinks(approval(), { conversationId: "C1" })).toEqual({ conversation: routes.agentChat(AGENT, { conversation: "C1" }), workflowRun: null });
    expect(approvalLinks(approval(), {})).toEqual({ conversation: null, workflowRun: null });
  });

  it("links a workflow step, and a tool call inside a workflow agent step, to the workflow run", () => {
    const step = approval({ kind: "step", approval_key: "review", step_id: "review", tool_call_id: null, workflow_id: "W1", agent_id: null });
    expect(approvalLinks(step)).toEqual({ conversation: null, workflowRun: routes.workflowRun("W1", RUN) });
    const nested = approval({ root_run_id: ROOT, workflow_id: "W1" });
    expect(approvalLinks(nested, { conversationId: "C1" })).toEqual({ conversation: null, workflowRun: routes.workflowRun("W1", ROOT) });
  });
});

describe("approval API calls", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("lists approvals", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [approval()], next_cursor: null }));
    const page = await listApprovals({ status: "pending", kind: "step", agent_id: null });
    expect(page.items).toHaveLength(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/approvals?status=pending&kind=step&limit=50");
  });

  it("gets one approval by run, kind and key; null on 404, ApiError otherwise", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(approval()));
    expect(await getApproval(RUN, "tool", "c/1")).toMatchObject({ run_id: RUN });
    expect(String(fetchMock.mock.calls[0][0])).toContain(`/api/v1/approvals/${RUN}/tool/c%2F1`);
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "Approval not found" }, 404));
    expect(await getApproval(RUN, "step", "s")).toBeNull();
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "boom" }, 500));
    await expect(getApproval(RUN, "step", "s")).rejects.toMatchObject({ status: 500 });
  });

  it("finds an approval of either kind", async () => {
    fetchMock.mockImplementation(async (url: string) =>
      String(url).includes("/step/") ? jsonResponse(approval({ kind: "step", step_id: "s" })) : jsonResponse({ detail: "Approval not found" }, 404),
    );
    expect(await findApproval(RUN, "s")).toMatchObject({ kind: "step" });
    fetchMock.mockImplementation(async () => jsonResponse({ detail: "Approval not found" }, 404));
    expect(await findApproval(RUN, "s")).toBeNull();
  });

  it("decides a tool call with a trimmed comment, and throws ApiError with the status", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ run_id: RUN, tool_call_id: "c/1", decision: "denied", decided_by: "u", comment: "no", notified: true }));
    await decideToolApproval(RUN, "c/1", "deny", "  no  ");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(`/api/v1/runs/${RUN}/tool-approvals/c%2F1/deny`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ comment: "no" });

    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "tool call is not waiting for approval" }, 409));
    await expect(decideToolApproval(RUN, "c1", "approve")).rejects.toMatchObject({ status: 409 });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({});
  });
});

const pendingCountNow = () => pendingSnapshotForTests()?.count;

describe("race notice", () => {
  it("names who decided first and says this decision was not recorded", () => {
    expect(raceNotice(approval({ status: "decided", decision: "approved", decided_by: "bob", decided_at: "2026-11-15T10:05:00Z" }))).toMatch(
      /^Already approved by bob at .+\. Your decision was not recorded\.$/,
    );
    expect(raceNotice(approval({ status: "decided", decision: "denied", decided_by: null, decided_at: null }))).toBe("Already denied. Your decision was not recorded.");
    expect(raceNotice(approval({ status: "decided", decision: "timed_out", decided_at: "2026-11-15T10:05:00Z" }))).toMatch(/^No one decided in time: it timed out at .+\. Your decision was not recorded\.$/);
    expect(raceNotice(approval({ run_status: "needs_attention" }))).toBe("The run ended (needs attention) before a decision. Your decision was not recorded.");
    expect(raceNotice(approval())).toBe("This approval is no longer waiting. Your decision was not recorded.");
    expect(raceNotice(null)).toBe("This approval is no longer available. Your decision was not recorded.");
  });
});

describe("pending count", () => {
  it("labels the badge, hidden at 0, capped at 99+", () => {
    expect(pendingBadgeLabel(null)).toBeNull();
    expect(pendingBadgeLabel({ count: 0, more: false })).toBeNull();
    expect(pendingBadgeLabel({ count: 3, more: false })).toBe("3");
    expect(pendingBadgeLabel({ count: 100, more: false })).toBe("99+");
    expect(pendingBadgeLabel({ count: 5, more: true })).toBe("99+");
  });

  it("polls every 30 s", () => {
    expect(PENDING_POLL_MS).toBe(30_000);
  });

  describe("refresh", () => {
    const fetchMock = vi.fn();
    beforeEach(() => {
      resetPendingApprovalsForTests();
      fetchMock.mockReset();
      vi.stubGlobal("fetch", fetchMock);
      vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    });
    afterEach(() => {
      resetPendingApprovalsForTests();
      vi.unstubAllGlobals();
      vi.unstubAllEnvs();
    });

    it("a refresh asked for during a fetch ignores that fetch's answer and fetches once more", async () => {
      // The first poll was asked before a decision: its answer (3) is stale once a refresh is
      // requested, and every refresh asked meanwhile shares one follow-up fetch.
      let release!: (r: Response) => void;
      fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => (release = resolve)));
      fetchMock.mockResolvedValue(jsonResponse({ items: [{}], next_cursor: null }));
      const seen: Array<number | undefined> = [];
      const first = refreshPendingApprovals();
      const second = refreshPendingApprovals();
      const third = refreshPendingApprovals();
      expect(second).toBe(third);
      release(jsonResponse({ items: [{}, {}, {}], next_cursor: null }));
      await first;
      seen.push(pendingCountNow());
      await second;
      seen.push(pendingCountNow());
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(seen).toEqual([undefined, 1]);
      expect(String(fetchMock.mock.calls[0][0])).toContain("status=pending");
    });
  });
});
