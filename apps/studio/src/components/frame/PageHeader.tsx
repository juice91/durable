"use client";

import { navigateGuarded } from "./navigationGuard";
import React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ChevronRight, ChevronsUpDown } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { focusRing } from "@/components/ui/Button";
import { Combobox, type ComboboxOption } from "@/components/ui/Combobox";
import { LinkTabs, type LinkTab } from "@/components/ui/Tabs";
import { PRODUCT_NAME } from "./brand";
import { useDocumentTitle } from "./useDocumentTitle";


/** `"{title} · AI Platform Studio"`, or the product name alone. */
export function documentTitleFor(title: string | null | undefined): string {
  const trimmed = title?.trim();
  return trimmed ? `${trimmed} · ${PRODUCT_NAME}` : PRODUCT_NAME;
}

export interface EntitySwitcherProps {
  /** Accessible name of the trigger ("Switch agent"). */
  label: string;
  options: ComboboxOption[];
  /** The current entity's id. */
  value: string | null;
  /** Where choosing an entity goes; the switcher navigates there. */
  hrefFor?: (value: string) => string;
  /** Called instead of (or as well as) navigating. */
  onValueChange?: (value: string) => void;
  searchPlaceholder?: string;
  testId?: string;
}

/**
 * The breadcrumb's entity switcher: a small chevron button that opens a searchable list of the
 * same kind of entity (agents, workflows, MCP servers). Its own accessible name says what it does,
 * so the crumb text next to it stays the heading.
 */
export function EntitySwitcher({ label, options, value, hrefFor, onValueChange, searchPlaceholder, testId }: EntitySwitcherProps) {
  const router = useRouter();
  return (
    <Combobox
      label={label}
      options={options}
      value={value}
      searchPlaceholder={searchPlaceholder ?? `${label}…`}
      testId={testId}
      className="size-7 shrink-0 justify-center px-0 text-muted-foreground"
      onValueChange={(next) => {
        onValueChange?.(next);
        if (hrefFor && next !== value) {
          // Through the navigation guard: the editor may hold unsaved edits (SL.11).
          const href = hrefFor(next);
          navigateGuarded(href, (target) => router.push(target));
        }
      }}
    >
      <ChevronsUpDown className="size-icon-dense" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
    </Combobox>
  );
}

export interface Crumb {
  label: React.ReactNode;
  /** A link for every crumb but the last (the last is the page's h1). */
  href?: string;
  /** An entity switcher shown after the crumb. */
  switcher?: EntitySwitcherProps;
  testId?: string;
}

export interface PageHeaderProps {
  /** Ancestors first; the last crumb is the page's visible `<h1>`. */
  crumbs: Crumb[];
  /** Buttons and links on the right (the page's primary action, filters that belong to the title). */
  actions?: React.ReactNode;
  /** Route tabs under the title row. */
  tabs?: LinkTab[];
  /** Accessible name of the tab navigation. Default "{title} sections". */
  tabsLabel?: string;
  /** A pinned row under the header for search and filters. */
  actionRow?: React.ReactNode;
  /** `document.title` text; defaults to the last crumb when it is a string. */
  documentTitle?: string;
  /** A ref on the h1 (pages that move focus to their heading). */
  titleRef?: React.Ref<HTMLHeadingElement>;
  /** Extra props for the h1 (`tabIndex={-1}`, a test id). */
  titleProps?: Omit<React.HTMLAttributes<HTMLHeadingElement>, "children">;
  className?: string;
  testId?: string;
}

/**
 * The page header (decision "Page header and title"): 44 px, sticky at the top of the main card.
 * Breadcrumbs on the left, where the last crumb is the visible `<h1>`; actions on the right; optional
 * route tabs and a pinned action row below. It also sets `document.title`.
 */
export function PageHeader({ crumbs, actions, tabs, tabsLabel, actionRow, documentTitle, titleRef, titleProps, className, testId = "page-header" }: PageHeaderProps) {
  const last = crumbs[crumbs.length - 1];
  const ancestors = crumbs.slice(0, -1);
  const titleText = documentTitle ?? (typeof last?.label === "string" ? last.label : typeof last?.label === "number" ? String(last.label) : null);

  useDocumentTitle(titleText === null ? null : documentTitleFor(titleText));

  const { className: titleClassName, ...restTitleProps } = titleProps ?? {};
  return (
    <header data-testid={testId} className={cn("sticky top-0 z-10 shrink-0 border-b border-border bg-surface", className)}>
      <div className="flex min-h-header min-w-0 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-1.5">
        <nav aria-label="Breadcrumb" className="flex min-w-0 flex-1 basis-48">
          <ol className="flex min-w-0 items-center gap-1">
            {ancestors.map((crumb, index) => (
              <li key={index} className="flex min-w-0 shrink items-center gap-1">
                {crumb.href ? (
                  <Link
                    href={crumb.href}
                    data-testid={crumb.testId}
                    className={cn("truncate rounded-sm text-label text-muted-foreground hover:text-foreground", focusRing)}
                  >
                    {crumb.label}
                  </Link>
                ) : (
                  <span data-testid={crumb.testId} className="truncate text-label text-muted-foreground">
                    {crumb.label}
                  </span>
                )}
                {crumb.switcher ? <EntitySwitcher {...crumb.switcher} /> : null}
                <ChevronRight className="size-icon-dense shrink-0 text-muted-foreground" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
              </li>
            ))}
            {last ? (
              <li aria-current="page" className="flex min-w-0 items-center gap-1">
                <h1
                  ref={titleRef}
                  data-testid={last.testId}
                  {...restTitleProps}
                  className={cn("min-w-0 truncate text-title font-semibold text-foreground", titleClassName)}
                >
                  {last.label}
                </h1>
                {last.switcher ? <EntitySwitcher {...last.switcher} /> : null}
              </li>
            ) : null}
          </ol>
        </nav>
        {actions ? <div className="flex min-w-0 shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {tabs && tabs.length > 0 ? (
        <LinkTabs items={tabs} label={tabsLabel ?? `${titleText ?? "Page"} sections`} className="px-4" />
      ) : null}
      {actionRow ? <div className="flex min-w-0 flex-wrap items-center gap-2 border-t border-border px-4 py-2">{actionRow}</div> : null}
    </header>
  );
}
