import React from "react";

/** The page shown instead of the app while nobody is signed in: centred, and the page's only landmark. */
export function AuthScreen({ children }: { children: React.ReactNode }) {
  return (
    <main data-testid="auth-gate" className="flex h-dvh items-center justify-center bg-canvas p-6 text-foreground">
      {children}
    </main>
  );
}
