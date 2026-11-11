"use client";

import React from "react";
import { Sidebar, MobileSidebar, MobileMenuButton } from "./Sidebar";
import { TopBar } from "./TopBar";
import { useState } from "react";

type LayoutProps = {
  children: React.ReactNode;
};

export function Layout({ children }: LayoutProps) {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  return (
    <div className="flex h-screen bg-background text-foreground">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <TopBar />
        <div className="flex-1 overflow-auto">
          <main className="p-4 md:p-6 lg:p-8">{children}</main>
        </div>
      </div>
      <MobileSidebar isOpen={mobileMenuOpen} onClose={() => setMobileMenuOpen(false)} />
      <div className="md:hidden fixed bottom-4 right-4 z-40">
        <MobileMenuButton onClick={() => setMobileMenuOpen(true)} />
      </div>
    </div>
  );
}
