"use client";

import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { useFrame } from "@/components/frame/FrameContext";
import { useViewportWidth } from "@/components/frame/useMediaQuery";
import {
  CANVAS_INSET_PX,
  clampPanelWidth,
  clampTimelineHeight,
  DEFAULT_LAYOUT_PREFS,
  PANEL_KEY_STEP,
  PANEL_MIN_WIDTH,
  PANEL_RAIL_WIDTH,
  panelMaxWidth,
  readLayoutPrefs,
  workflowCanvasWidth,
  workflowLayout,
  writeLayoutPrefs,
  type WorkflowLayout,
  type WorkflowLayoutPrefs,
} from "./workflowLayout";

export interface WorkflowLayoutControl {
  layout: WorkflowLayout;
  canvasWidth: number;
  prefs: WorkflowLayoutPrefs;
  /** Change the layout preferences; `persist` false while dragging (saved on release). */
  update: (patch: Partial<WorkflowLayoutPrefs>, persist?: boolean) => void;
}

/**
 * The workflow pages' layout state: the saved preferences (`studio.workflow-layout.v1`, read after
 * mount so the server markup is the default) and the fit rule for the current window and sidebar.
 */
export function useWorkflowLayout(): WorkflowLayoutControl {
  const frame = useFrame();
  const viewport = useViewportWidth();
  const [prefs, setPrefs] = useState<WorkflowLayoutPrefs>(DEFAULT_LAYOUT_PREFS);
  const latest = useRef(prefs);
  latest.current = prefs;

  useEffect(() => {
    setPrefs(readLayoutPrefs());
  }, []);

  const update = useCallback((patch: Partial<WorkflowLayoutPrefs>, persist = true) => {
    const next = { ...latest.current, ...patch };
    latest.current = next;
    setPrefs(next);
    if (persist) writeLayoutPrefs(next);
  }, []);

  const canvasWidth = workflowCanvasWidth({ viewport, sidebarCollapsed: frame?.sidebarCollapsed ?? false, inFrame: frame !== null });
  const layout = useMemo(
    () => workflowLayout({ viewport, canvasWidth, panelWidth: prefs.panelWidth, panelCollapsed: prefs.panelCollapsed }),
    [viewport, canvasWidth, prefs.panelWidth, prefs.panelCollapsed],
  );
  return { layout, canvasWidth, prefs, update };
}

/** A titled card inside the floating panel. */
export function PanelCard({ title, titleId, actions, children, className, testId }: {
  title: React.ReactNode;
  titleId?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  testId?: string;
}) {
  const generated = useId();
  const id = titleId ?? `panel-card-${generated}`;
  return (
    <section aria-labelledby={id} data-testid={testId} className={cn("min-w-0 border-b border-border p-4 last:border-b-0", className)}>
      <div className="mb-3 flex min-w-0 items-center justify-between gap-2">
        <h2 id={id} className="min-w-0 truncate text-label font-semibold text-foreground">{title}</h2>
        {actions ? <div className="flex shrink-0 items-center gap-1">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

export interface WorkflowFloatingPanelProps {
  control: WorkflowLayoutControl;
  /** The panel's name ("Run", "Run details"): its landmark label, the Sheet title and the button names. */
  title: string;
  children: React.ReactNode;
}

/**
 * The floating left panel over the workflow canvas (decision "Workflows"): 380 px by default,
 * resizable from its right edge (pointer or Arrow keys), collapsible to a rail, with the width and
 * the collapsed state saved. Its maximum leaves GRAPH_MIN_WIDTH of graph visible. Below 768 px (or
 * where even its minimum would squeeze the graph) it is a bottom Sheet opened from a button on the
 * canvas, and the canvas fills the screen.
 */
export function WorkflowFloatingPanel({ control, title, children }: WorkflowFloatingPanelProps) {
  const { layout, canvasWidth, prefs, update } = control;
  const [sheetOpen, setSheetOpen] = useState(false);
  const bodyId = useId();
  // The panel's content lives in ONE subtree, portalled into this node, whatever shows it: the
  // floating body, the bottom Sheet, or nothing (collapsed, Sheet closed). The visible container
  // adopts the node, so collapsing, closing the Sheet or crossing 768 px never unmounts the content:
  // typed Run input, an in-flight submit (it still opens the run page) and open disclosures survive.
  // (A force-mounted closed modal Sheet would instead hide the page from screen readers.)
  //
  // Hydration: the node is created in an effect, so the server render and the client's hydration
  // pass both render no portal and match; the content mounts right after, and HostSlot's ref
  // callback (which changes with `host`) adopts the node then. Creating it during render would make
  // the client render a portal the server did not, and React would throw a hydration error.
  //
  // Events: while the bottom Sheet is open, the host node sits inside the Sheet's portal, so React
  // dispatches each event from the content twice: once from this portal (up through the page's
  // React tree) and once from the Sheet's portal (up through the Sheet's tree, then the page).
  // Handlers inside the content run once; handlers on ancestors above this panel run twice.
  // Radix relies on the second path: its "pointer down inside" check is a React capture handler on
  // the Sheet's tree, so blocking it (stopPropagation on the host, say) makes every tap inside the
  // Sheet close it. Keep page-level handlers above the panel idempotent, and block neither path.
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  useEffect(() => {
    const node = document.createElement("div");
    node.className = "min-w-0";
    node.dataset.testid = "workflow-panel-content";
    setHost(node);
    return () => node.remove();
  }, []);
  const content = host ? createPortal(children, host) : null;

  let frame: React.ReactNode;
  if (layout.panel === "sheet") {
    frame = (
      <>
        <Button
          size="sm"
          data-testid="workflow-panel-open"
          className="absolute bottom-3 left-3 z-10 shadow-raised"
          onClick={() => setSheetOpen(true)}
        >
          <PanelLeftOpen strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          {`Show ${title.toLowerCase()}`}
        </Button>
        <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
          <SheetContent
            size="full"
            data-testid="workflow-panel-sheet"
            aria-describedby={undefined}
            // A bottom sheet: the Sheet is a right drawer by default.
            style={{ top: "auto", height: "85dvh" }}
            className="rounded-t-xl border-l-0 border-t"
          >
            <SheetHeader title={title} />
            <SheetBody className="p-0">
              <HostSlot host={host} />
            </SheetBody>
          </SheetContent>
        </Sheet>
      </>
    );
  } else if (prefs.panelCollapsed) {
    frame = (
      <div
        data-testid="workflow-panel"
        data-collapsed="true"
        style={{ top: CANVAS_INSET_PX, left: CANVAS_INSET_PX, width: PANEL_RAIL_WIDTH }}
        className="absolute z-10 flex flex-col items-center rounded-lg border border-border bg-surface-raised p-1 shadow-raised"
      >
        <IconButton
          label={`Show ${title.toLowerCase()} panel`}
          title={`Show ${title.toLowerCase()} panel`}
          size="sm"
          tooltip={false}
          aria-expanded={false}
          data-testid="workflow-panel-toggle"
          icon={<PanelLeftOpen strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
          onClick={() => update({ panelCollapsed: false })}
        />
      </div>
    );
  } else {
    frame = (
      <section
        aria-label={title}
        data-testid="workflow-panel"
        data-collapsed="false"
        style={{ top: CANVAS_INSET_PX, left: CANVAS_INSET_PX, bottom: CANVAS_INSET_PX, width: layout.panelWidth }}
        className="absolute z-10 flex min-w-0 flex-col rounded-lg border border-border bg-surface-raised shadow-raised"
      >
        <div className="flex h-10 shrink-0 items-center justify-between gap-2 border-b border-border pl-4 pr-1">
          <span className="min-w-0 truncate text-caption font-medium uppercase tracking-wide text-muted-foreground">{title}</span>
          <IconButton
            label={`Hide ${title.toLowerCase()} panel`}
            title={`Hide ${title.toLowerCase()} panel`}
            size="sm"
            tooltip={false}
            aria-expanded
            aria-controls={bodyId}
            data-testid="workflow-panel-toggle"
            icon={<PanelLeftClose strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
            onClick={() => update({ panelCollapsed: true })}
          />
        </div>
        <div id={bodyId} data-testid="workflow-panel-body" className="min-h-0 min-w-0 flex-1 overflow-y-auto">
          <HostSlot host={host} />
        </div>
        <PanelResizeHandle
          label={`Resize ${title.toLowerCase()} panel`}
          width={layout.panelWidth}
          max={panelMaxWidth(canvasWidth)}
          onResize={(width, persist) => update({ panelWidth: clampPanelWidth(width, canvasWidth) }, persist)}
        />
      </section>
    );
  }

  return (
    <>
      {frame}
      {content}
    </>
  );
}

/** Where the panel's persistent content node is shown: adopts it on mount (moving it from wherever it was). */
function HostSlot({ host }: { host: HTMLElement | null }) {
  const adopt = useCallback(
    (node: HTMLDivElement | null) => {
      if (node && host && host.parentNode !== node) node.appendChild(host);
    },
    [host],
  );
  return <div ref={adopt} className="min-w-0" />;
}

/** The panel's right edge: ArrowRight widens, ArrowLeft narrows, Home and End jump to the limits. */
function PanelResizeHandle({ label, width, max, onResize }: { label: string; width: number; max: number; onResize: (width: number, persist: boolean) => void }) {
  const drag = useRef<{ startX: number; startWidth: number; last: number } | null>(null);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    let next: number | null = null;
    if (event.key === "ArrowRight") next = width + PANEL_KEY_STEP;
    else if (event.key === "ArrowLeft") next = width - PANEL_KEY_STEP;
    else if (event.key === "Home") next = PANEL_MIN_WIDTH;
    else if (event.key === "End") next = max;
    if (next === null) return;
    event.preventDefault();
    onResize(next, true);
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuemin={PANEL_MIN_WIDTH}
      aria-valuemax={max}
      aria-valuenow={width}
      tabIndex={0}
      data-testid="workflow-panel-resize"
      onKeyDown={onKeyDown}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        drag.current = { startX: event.clientX, startWidth: width, last: width };
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (!drag.current) return;
        const next = drag.current.startWidth + (event.clientX - drag.current.startX);
        drag.current.last = next;
        onResize(next, false);
      }}
      onPointerUp={(event) => {
        if (!drag.current) return;
        onResize(drag.current.last, true);
        drag.current = null;
        event.currentTarget.releasePointerCapture?.(event.pointerId);
      }}
      onPointerCancel={() => {
        if (drag.current) onResize(drag.current.last, true);
        drag.current = null;
      }}
      className="group absolute inset-y-2 -right-1 flex w-2 cursor-col-resize touch-none justify-center rounded-full outline-none"
    >
      <span className="w-px rounded-full bg-transparent transition-colors group-hover:bg-border-strong group-focus-visible:bg-ring motion-reduce:transition-none" />
    </div>
  );
}

/** The Timeline dock's top edge: ArrowUp grows it, ArrowDown shrinks it. */
export function DockResizeHandle({ height, onResize }: { height: number; onResize: (height: number, persist: boolean) => void }) {
  const drag = useRef<{ startY: number; startHeight: number; last: number } | null>(null);
  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    let next: number | null = null;
    if (event.key === "ArrowUp") next = height + 16;
    else if (event.key === "ArrowDown") next = height - 16;
    if (next === null) return;
    event.preventDefault();
    onResize(clampTimelineHeight(next), true);
  };
  return (
    <div
      role="separator"
      aria-orientation="horizontal"
      aria-label="Resize timeline"
      aria-valuemin={clampTimelineHeight(0)}
      aria-valuemax={clampTimelineHeight(Number.MAX_SAFE_INTEGER)}
      aria-valuenow={height}
      tabIndex={0}
      data-testid="workflow-timeline-resize"
      onKeyDown={onKeyDown}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        drag.current = { startY: event.clientY, startHeight: height, last: height };
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (!drag.current) return;
        // The dock grows upwards: moving the pointer up makes it taller.
        const next = clampTimelineHeight(drag.current.startHeight + (drag.current.startY - event.clientY));
        drag.current.last = next;
        onResize(next, false);
      }}
      onPointerUp={(event) => {
        if (!drag.current) return;
        onResize(drag.current.last, true);
        drag.current = null;
        event.currentTarget.releasePointerCapture?.(event.pointerId);
      }}
      onPointerCancel={() => {
        if (drag.current) onResize(drag.current.last, true);
        drag.current = null;
      }}
      className="group -my-1 flex h-2 shrink-0 cursor-row-resize touch-none items-center outline-none"
    >
      <span className="h-px w-full bg-transparent transition-colors group-hover:bg-border-strong group-focus-visible:bg-ring motion-reduce:transition-none" />
    </div>
  );
}
