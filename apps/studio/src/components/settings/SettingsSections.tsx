"use client";

import React, { useCallback, useEffect, useState } from "react";
import { Monitor, Moon, Sun } from "lucide-react";
import { BASE_URL } from "@/lib/api";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { applySidebarAttribute, SIDEBAR_STORAGE_KEY } from "@/lib/sidebarPref";
import type { ThemePreference } from "@/lib/theme";
import { studioVersion } from "@/components/frame/Sidebar";
import { ShortcutTable } from "@/components/shortcuts/ShortcutTable";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { useTheme } from "@/components/theme/ThemeProvider";
import { Button } from "@/components/ui/Button";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { toast } from "@/components/ui/Toaster";
import { BROWSER_DATA, browserDataCounts, clearBrowserData, type ClearGroup } from "./browserData";

const iconProps = { strokeWidth: ICON_STROKE_WIDTH, "aria-hidden": true } as const;

const THEME_OPTIONS: { value: ThemePreference; label: string; icon: React.ReactNode }[] = [
  { value: "system", label: "System", icon: <Monitor {...iconProps} /> },
  { value: "light", label: "Light", icon: <Sun {...iconProps} /> },
  { value: "dark", label: "Dark", icon: <Moon {...iconProps} /> },
];

const CLEAR_COPY: Record<ClearGroup, { testId: string; button: string; title: string; description: string; done: string; none: string }> = {
  layout: {
    testId: "settings-clear-layout",
    button: "Clear layout preferences",
    title: "Clear layout preferences?",
    description: "The sidebar, panel and pane sizes and the last agent go back to their defaults. Your theme is kept.",
    done: "Layout preferences cleared",
    none: "There were no layout preferences to clear.",
  },
  presets: {
    testId: "settings-clear-presets",
    button: "Clear request-context presets",
    title: "Clear request-context presets?",
    description: "Every saved request-context value, for every agent, is removed from this browser. This cannot be undone.",
    done: "Request-context presets cleared",
    none: "There were no request-context presets to clear.",
  },
};

function Section({ id, testId, title, description, children }: { id: string; testId: string; title: string; description?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section aria-labelledby={`settings-${id}`} data-testid={testId} className="flex min-w-0 flex-col gap-3 rounded-lg border border-border p-4">
      <div className="flex min-w-0 flex-col gap-1">
        <h2 id={`settings-${id}`} className="text-title font-semibold text-foreground">
          {title}
        </h2>
        {description ? <p className="text-label text-muted-foreground">{description}</p> : null}
      </div>
      {children}
    </section>
  );
}

function AppearanceSection() {
  const { theme, setTheme } = useTheme();
  return (
    <Section id="appearance" testId="settings-appearance" title="Appearance" description="System follows your operating system's light or dark setting.">
      <div data-testid="settings-theme">
        <SegmentedControl<ThemePreference> label="Theme" options={THEME_OPTIONS} value={theme} onValueChange={setTheme} />
      </div>
    </Section>
  );
}

function plural(count: number, one: string, many: string) {
  return `${count} ${count === 1 ? one : many}`;
}

function BrowserDataSection() {
  // Read after mount: the server has no storage, and the first client render must match it.
  const [counts, setCounts] = useState<Record<string, number> | null>(null);
  const [confirming, setConfirming] = useState<ClearGroup | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const refresh = useCallback(() => setCounts(browserDataCounts()), []);
  useEffect(refresh, [refresh]);

  const clear = useCallback(
    (group: ClearGroup) => {
      setConfirming(null);
      const result = clearBrowserData(group);
      const copy = CLEAR_COPY[group];
      if (result === null || (result.removed.length === 0 && result.failed.length > 0)) {
        setStatus("Browser storage is not available, so nothing was cleared.");
        toast.error("Browser storage is not available");
      } else if (result.failed.length > 0) {
        const total = result.removed.length + result.failed.length;
        setStatus(`Cleared ${result.removed.length} of ${plural(total, "item", "items")}; the browser refused to remove the rest.`);
        toast.error(`${copy.done} only in part`);
      } else if (result.removed.length === 0) {
        setStatus(copy.none);
        toast(copy.none);
      } else {
        const message = `${copy.done} (${plural(result.removed.length, "item", "items")}).`;
        setStatus(message);
        toast.success(copy.done);
      }
      // The sidebar follows its attribute; resetting it shows the default now without writing storage.
      if (group === "layout" && result?.removed.includes(SIDEBAR_STORAGE_KEY)) applySidebarAttribute(false);
      refresh();
    },
    [refresh],
  );

  return (
    <Section
      id="browser-data"
      testId="settings-browser-data"
      title="Data in this browser"
      description="Studio keeps these in this browser's local storage, unencrypted, on this device only."
    >
      <div className="min-w-0 overflow-x-auto">
        <table className="w-full text-label" data-testid="browser-data-table">
          <thead>
            <tr className="border-b border-border text-left text-caption text-muted-foreground">
              <th scope="col" className="py-1.5 pr-3 font-medium">Item</th>
              <th scope="col" className="py-1.5 text-right font-medium">Stored</th>
            </tr>
          </thead>
          <tbody>
            {BROWSER_DATA.map((entry) => {
              const count = counts?.[entry.id];
              const prefixed = entry.prefixes.length > 0;
              return (
                <tr key={entry.id} data-browser-data={entry.id} className="border-b border-border last:border-b-0">
                  <td className="min-w-0 py-1.5 pr-3">
                    <div className="text-foreground">{entry.label}</div>
                    <div className="text-caption text-muted-foreground">{entry.description}</div>
                  </td>
                  <td className="whitespace-nowrap py-1.5 text-right tabular-nums text-muted-foreground">
                    {count === undefined ? "…" : count === 0 ? "Not set" : prefixed ? `${count} saved` : "Saved"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap gap-2">
        {(Object.keys(CLEAR_COPY) as ClearGroup[]).map((group) => (
          <Button key={group} variant="secondary" size="sm" onClick={() => setConfirming(group)} data-testid={CLEAR_COPY[group].testId}>
            {CLEAR_COPY[group].button}
          </Button>
        ))}
      </div>
      <p role="status" data-testid="settings-clear-status" className="min-h-5 text-label text-muted-foreground">
        {status}
      </p>
      {confirming ? (
        <ConfirmDialog
          title={CLEAR_COPY[confirming].title}
          description={CLEAR_COPY[confirming].description}
          confirmLabel="Clear"
          destructive
          onConfirm={() => clear(confirming)}
          onCancel={() => setConfirming(null)}
        />
      ) : null}
    </Section>
  );
}

function AboutSection() {
  const version = studioVersion();
  return (
    <Section id="about" testId="settings-about" title="About">
      <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-label">
        <dt className="text-muted-foreground">Studio version</dt>
        <dd data-testid="settings-version" className="min-w-0 break-words font-mono text-foreground">{version ?? "Not set"}</dd>
        <dt className="text-muted-foreground">API base URL</dt>
        <dd data-testid="settings-api-base" className="min-w-0 break-all font-mono text-foreground">{BASE_URL}</dd>
      </dl>
      <p className="text-caption text-muted-foreground">Both come from the build environment and cannot be changed here.</p>
    </Section>
  );
}

/** The Settings page body (decision "Settings page"). */
export function SettingsSections() {
  return (
    <div className="mx-auto flex w-full min-w-0 max-w-3xl flex-col gap-4 px-4 py-6">
      <AppearanceSection />
      <Section id="shortcuts" testId="settings-shortcuts" title="Keyboard shortcuts" description="Single keys work when focus is not in a text field. Press ? anywhere to see this list.">
        <ShortcutTable headingLevel={3} />
      </Section>
      <BrowserDataSection />
      <AboutSection />
    </div>
  );
}
