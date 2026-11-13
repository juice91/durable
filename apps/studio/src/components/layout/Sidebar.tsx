import React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { navSections, type NavItem } from "@/lib/nav";

function usePathnameWrapper() {
  return usePathname();
}

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="hidden md:flex flex-col w-64 h-screen border-r border-border bg-background">
      <div className="p-4 border-b border-border">
        <h1 className="text-lg font-semibold text-foreground">AI Platform Studio</h1>
      </div>
      <nav className="flex-1 overflow-y-auto p-2 space-y-6">
        {navSections.map((section) => (
          <div key={section.title}>
            <h2 className="px-2 mb-2 text-xs font-medium text-muted-foreground uppercase tracking-wider">
              {section.title}
            </h2>
            <div className="space-y-1">
              {section.items.map((item) => {
                const isActive = pathname === item.href;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`flex items-center px-2 py-2 text-sm font-medium rounded-md transition-colors ${
                      isActive
                        ? "bg-accent text-accent-foreground"
                        : "text-foreground hover:bg-accent hover:text-accent-foreground"
                    }`}
                    aria-current={isActive ? "page" : undefined}
                  >
                    <item.icon className="w-4 h-4 mr-3" />
                    <span>{item.label}</span>
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </nav>
    </aside>
  );
}

export function MobileMenuButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="md:hidden p-2 rounded-md text-foreground hover:bg-accent"
      aria-label="Open sidebar menu"
    >
      <svg
        className="w-6 h-6"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
        xmlns="http://www.w3.org/2000/svg"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M4 6h16M4 12h16M4 18h16"
        />
      </svg>
    </button>
  );
}

export function MobileSidebar({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const pathname = usePathnameWrapper();

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 md:hidden">
      <div className="fixed inset-0 bg-canvas/80" onClick={onClose} />
      <div className="fixed inset-y-0 left-0 w-64 bg-background shadow-xl transform transition-transform duration-200 ease-in-out">
        <div className="flex items-center justify-between p-4 border-b border-border">
          <h1 className="text-lg font-semibold text-foreground">AI Platform Studio</h1>
          <button
            onClick={onClose}
            className="p-2 rounded-md text-foreground hover:bg-accent"
            aria-label="Close sidebar"
          >
            <svg
              className="w-6 h-6"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
              xmlns="http://www.w3.org/2000/svg"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>
        <nav className="p-4 space-y-2">
          {navSections.flatMap((section) => section.items).map((item) => {
            const isActive = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                onClick={onClose}
                className={`block px-4 py-3 text-sm font-medium rounded-md ${
                  isActive
                    ? "bg-accent text-accent-foreground"
                    : "text-foreground hover:bg-accent hover:text-accent-foreground"
                }`}
                aria-current={isActive ? "page" : undefined}
              >
                <item.icon className="inline-block w-4 h-4 mr-3" />
                {item.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </div>
  );
}
