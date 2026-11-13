"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { Link2, MessagesSquare, PanelRight } from "lucide-react";
import { api, ApiError, type Agent, type AgentVersion } from "@/lib/api";
import { sidebarAttributeCollapsed } from "@/lib/sidebarPref";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { navCrumb } from "@/lib/nav";
import { agentTabOf, routes, type AgentTab } from "@/lib/routes";
import { PageHeader, type Crumb } from "@/components/frame/PageHeader";
import { useFrame } from "@/components/frame/FrameContext";
import { SidePanel, useSidePanel } from "@/components/frame/SidePanel";
import { shouldHandleShortcut } from "@/components/frame/keyboard";
import { useIsWide, useViewportWidth } from "@/components/frame/useMediaQuery";
import { IconButton } from "@/components/ui/IconButton";
import { toast } from "@/components/ui/Toaster";
import { TooltipProvider } from "@/components/ui/Tooltip";
import type { VersionRef } from "@/components/playground/runOptions";
import { AgentPageContext, type AgentPageValue } from "./AgentPageContext";
import { ConfigPanel } from "./ConfigPanel";
import { THREADS_TITLE } from "./AgentChatPanes";
import { CHAT_PANES_MIN_PX, chatPanelWidth, configOpenByDefault, threadsAutoCollapsed, threadsShown } from "./chatFit";
import { readConfigOpen, writeConfigOpen } from "./agentPageModel";

export const CONFIG_TITLE = "Config";

/** The frame's size right now (client only: called from effects and the panel's registration). */
function currentFrameSize(sidebarCollapsed: boolean) {
  return { viewport: window.innerWidth, sidebarCollapsed };
}

const TABS: { id: AgentTab; label: string; testId: string }[] = [
  { id: "chat", label: "Chat", testId: "agent-tab-chat" },
  { id: "traces", label: "Traces", testId: "agent-tab-traces" },
  { id: "editor", label: "Editor", testId: "agent-tab-editor" },
];

function loadErrorMessage(error: unknown): string {
  return error instanceof ApiError && error.status === 404 ? "Agent not found" : "Failed to load agent";
}

export interface AgentLayoutProps {
  agentId: string;
  children: React.ReactNode;
}

/**
 * The agent page's shared frame (`app/agents/[id]/layout.tsx`): loads the agent once and keeps it
 * across tabs. Header: the agent crumb with a switcher (to the same tab of another agent), the
 * Conversations toggle (Chat tab), the Config toggle and Copy link, and the Chat · Traces · Editor
 * tabs. The read-only Config panel is the frame's side panel: open by default on the Chat tab (wide
 * screens) when threads and chat fit beside it, a Sheet below 1024 px. Keys: `]` toggles Config, `{`
 * the conversations pane. Where Config does not leave room for both panes, the threads pane
 * collapses automatically while Config is open (chatFit.ts).
 */
export function AgentLayout({ agentId, children }: AgentLayoutProps) {
  const pathname = usePathname();
  const tab: AgentTab = agentTabOf(pathname) ?? "chat";
  const [agent, setAgent] = useState<Agent | null>(null);
  const [versions, setVersions] = useState<AgentVersion[] | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [configVersion, setConfigVersion] = useState<VersionRef>(null);
  const [requestContextSlot, setRequestContextSlot] = useState<HTMLElement | null>(null);
  const [copyStatus, setCopyStatus] = useState("");
  // The chat's address while on this agent, so Chat → Traces → Chat returns to the same conversation.
  const [chatHref, setChatHref] = useState<string | null>(null);
  const isWide = useIsWide();
  // The threads pane: expanded or collapsed on wide screens, a Sheet (closed by default) below.
  const [threadsExpanded, setThreadsExpanded] = useState(true);
  const [threadsSheetOpen, setThreadsSheetOpen] = useState(false);
  const threadsOpen = isWide ? threadsExpanded : threadsSheetOpen;
  const sidePanel = useSidePanel();
  const { available: configAvailable, toggle: toggleConfig } = sidePanel;

  // Does the Chat tab's threads + chat fit beside Config? (chatFit.ts.) The window width follows
  // resizes on wide screens only; the server and the hydration pass assume 1280 px, which only feeds
  // the client-only panes (they render after mount).
  const viewport = useViewportWidth(isWide);
  const sidebarCollapsed = useFrame()?.sidebarCollapsed ?? false;
  const frameSize = useMemo(() => ({ viewport, sidebarCollapsed }), [viewport, sidebarCollapsed]);
  const autoCollapsed =
    tab === "chat" && threadsAutoCollapsed({ isWide, configOpen: sidePanel.open, frame: frameSize, panelWidth: chatPanelWidth(sidePanel.width, frameSize) });
  const threadsVisible = isWide ? threadsShown(threadsExpanded, autoCollapsed) : threadsSheetOpen;
  // Marks a Config open or close made for the user rather than by them, so it is not saved as their
  // per-agent choice (the tab-change default, or closing Config to make room for threads).
  const autoToggleRef = useRef(false);

  useEffect(() => {
    if (isWide) setThreadsSheetOpen(false);
  }, [isWide]);

  useEffect(() => {
    let cancelled = false;
    setAgent(null);
    setVersions(null);
    setLoadError(null);
    setConfigVersion(null);
    setChatHref(null);
    api.agents
      .get(agentId)
      .then((loaded) => {
        if (!cancelled) setAgent(loaded);
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadError(loadErrorMessage(error));
      });
    api.versions
      .list(agentId)
      .then((items) => {
        if (!cancelled) setVersions(Array.isArray(items) ? items : []);
      })
      .catch(() => {
        if (!cancelled) setVersions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  // Every agent, for the switcher. A failure only leaves the switcher out.
  useEffect(() => {
    let cancelled = false;
    Promise.resolve()
      .then(() => api.agents.list())
      .then((items) => {
        if (!cancelled && Array.isArray(items)) setAgents(items);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  const reload = useCallback(async () => {
    try {
      const [loaded, items] = await Promise.all([api.agents.get(agentId), api.versions.list(agentId)]);
      setAgent(loaded);
      setVersions(Array.isArray(items) ? items : []);
      setLoadError(null);
    } catch {
      // The tabs report their own failures; the header keeps what it had.
    }
  }, [agentId]);

  // Config is open by default on the Chat tab (wide screens) when the panes fit beside it: applied
  // whenever the tab becomes Chat, not only when the page mounts, unless the user opened or closed it
  // there before (kept per agent).
  const { open: configOpen, setOpen: setConfigOpen } = sidePanel;
  // Only a tab change into Chat applies it: widening the window never opens the panel by itself (the
  // SL.4 rule), and the first visit is covered by the panel's own defaultOpen.
  const previousTabRef = useRef(tab);
  useEffect(() => {
    const enteredChat = tab === "chat" && previousTabRef.current !== "chat";
    previousTabRef.current = tab;
    if (!enteredChat || !isWide || !configAvailable) return;
    const wanted = configOpenByDefault(readConfigOpen(agentId), currentFrameSize(sidebarCollapsed));
    if (wanted === configOpen) return;
    autoToggleRef.current = true;
    setConfigOpen(wanted);
    // On the tab change only, not on every open, close or resize.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);
  // Remember a user's open or close on the Chat tab (the toggle, `]`, or the panel's Close button).
  const seenConfigOpenRef = useRef(configOpen);
  useEffect(() => {
    if (seenConfigOpenRef.current === configOpen) return;
    seenConfigOpenRef.current = configOpen;
    if (autoToggleRef.current) {
      autoToggleRef.current = false;
      return;
    }
    if (tab === "chat" && isWide && configAvailable) writeConfigOpen(agentId, configOpen);
  }, [agentId, configAvailable, configOpen, isWide, tab]);

  // A user's open or close of the threads pane. Opening it while it is auto-collapsed closes Config
  // (for them, not saved) so threads and chat get their room back.
  const setThreadsOpen = useCallback(
    (open: boolean) => {
      if (!isWide) {
        setThreadsSheetOpen(open);
        return;
      }
      if (open && autoCollapsed) {
        autoToggleRef.current = true;
        setConfigOpen(false);
      }
      setThreadsExpanded(open);
    },
    [autoCollapsed, isWide, setConfigOpen],
  );

  // `]` toggles Config; `{` the conversations pane (Chat tab). Ignored while typing or under a modal.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!shouldHandleShortcut(event)) return;
      if (event.key === "]" && configAvailable) {
        event.preventDefault();
        toggleConfig();
      } else if (event.key === "{" && tab === "chat") {
        event.preventDefault();
        setThreadsOpen(!threadsVisible);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [configAvailable, toggleConfig, setThreadsOpen, tab, threadsVisible]);

  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopyStatus("Link copied");
      toast.success("Link copied");
    } catch {
      setCopyStatus("Copying is not available here");
    }
  };

  const value = useMemo<AgentPageValue>(
    () => ({
      agentId,
      agent,
      versions,
      loadError,
      reload,
      configVersion,
      setConfigVersion,
      threadsOpen,
      setThreadsOpen,
      threadsAutoCollapsed: isWide && autoCollapsed,
      requestContextSlot,
      chatHref,
      setChatHref,
    }),
    [agentId, agent, versions, loadError, reload, configVersion, threadsOpen, setThreadsOpen, isWide, autoCollapsed, requestContextSlot, chatHref],
  );

  const title = agent?.name ?? (loadError ? "Agent" : "Loading agent…");
  const crumbs: Crumb[] = [
    navCrumb("agents"),
    {
      label: title,
      testId: "agent-page-title",
      switcher:
        agents.length > 0
          ? {
              label: "Switch agent",
              options: agents.map((item) => ({ value: item.id, label: item.name })),
              value: agentId,
              hrefFor: (id) => routes.agentTab(id, tab),
              testId: "agent-switcher",
            }
          : undefined,
    },
  ];

  const icon = (Icon: typeof Link2) => <Icon strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />;
  const actions = (
    <>
      {tab === "chat" ? (
        <IconButton
          label={THREADS_TITLE}
          shortcut="{"
          size="sm"
          aria-pressed={threadsVisible}
          onClick={() => setThreadsOpen(!threadsVisible)}
          icon={icon(MessagesSquare)}
          data-testid="agent-threads-toggle"
        />
      ) : null}
      <IconButton
        label={CONFIG_TITLE}
        shortcut="]"
        size="sm"
        aria-pressed={sidePanel.open}
        disabled={!sidePanel.available}
        onClick={sidePanel.toggle}
        icon={icon(PanelRight)}
        data-testid="agent-config-toggle"
      />
      <IconButton label="Copy link" size="sm" onClick={copyLink} icon={icon(Link2)} data-testid="agent-copy-link" />
      <span role="status" className="sr-only">
        {copyStatus}
      </span>
    </>
  );

  return (
    <TooltipProvider>
      <AgentPageContext.Provider value={value}>
        <div data-testid="agent-page" className="flex h-full min-h-0 min-w-0 flex-col">
          <PageHeader
            crumbs={crumbs}
            documentTitle={agent?.name}
            actions={actions}
            tabs={TABS.map((item) => ({
              href: item.id === "chat" && chatHref ? chatHref : routes.agentTab(agentId, item.id),
              label: item.label,
              active: item.id === tab,
              testId: item.testId,
            }))}
            tabsLabel="Agent sections"
          />
          <SidePanel
            title={CONFIG_TITLE}
            // Runs once on the client at registration, possibly during hydration's first pass, when the
            // frame still reports the expanded sidebar: read the pre-paint attribute too.
            defaultOpen={() =>
              tab === "chat" &&
              configOpenByDefault(readConfigOpen(agentId), currentFrameSize(sidebarCollapsed || sidebarAttributeCollapsed()))
            }
            mainMinWidth={tab === "chat" ? CHAT_PANES_MIN_PX : undefined}
          >
            {agent ? (
              <ConfigPanel
                agent={agent}
                versions={versions}
                versionRef={tab === "chat" ? configVersion : null}
                requestContextSlotRef={tab === "chat" ? setRequestContextSlot : undefined}
              />
            ) : (
              <p className="px-3 py-3 text-label text-muted-foreground">{loadError ?? "Loading configuration…"}</p>
            )}
          </SidePanel>
          {loadError && !agent ? (
            <div className="flex items-center justify-center py-12">
              <p role="alert" data-testid="agent-load-error" className="text-destructive-text">
                {loadError}
              </p>
            </div>
          ) : (
            <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto">{children}</div>
          )}
        </div>
      </AgentPageContext.Provider>
    </TooltipProvider>
  );
}
