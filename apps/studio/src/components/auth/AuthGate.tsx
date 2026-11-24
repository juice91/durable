"use client";

import React, { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Button } from "@/components/ui/Button";
import { AuthFlowError, CALLBACK_PATH } from "@/lib/auth/oidc";
import { getTokens, subscribeTokens } from "@/lib/auth/session";
import { installAuthFetch, keepFresh, oidc, setGiveUpHandler, startLogin } from "@/lib/auth/controller";

type Phase = "working" | "ready" | "error";

/**
 * Holds the page back until the person is signed in, when the server runs `EAP_STUDIO_AUTH=zitadel` (`enabled`). It runs
 * the authorization-code + PKCE flow: no token -> redirect to ZITADEL; /auth/callback -> exchange the code and go back to
 * the page the person wanted. When `enabled` is false (the default `access` mode, and the SSH-tunnel loopback page) it
 * renders its children untouched and does nothing.
 */
export function AuthGate({ enabled, children }: { enabled: boolean; children: React.ReactNode }) {
  if (!enabled) return <>{children}</>;
  return <ZitadelGate>{children}</ZitadelGate>;
}

function ZitadelGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [phase, setPhase] = useState<Phase>(() => (getTokens() ? "ready" : "working"));
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    setGiveUpHandler((text) => {
      setMessage(text);
      setPhase("error");
    });
    return () => setGiveUpHandler(null);
  }, []);

  useEffect(() => {
    const auth = oidc();
    if (!auth) {
      setPhase("ready"); // no OIDC settings reached the browser: behave as `access`
      return;
    }
    let live = true;
    const fail = (reason: unknown) => {
      if (!live) return;
      setMessage(reason instanceof AuthFlowError ? reason.message : "Sign-in failed. Try again.");
      setPhase("error");
    };
    if (pathname === CALLBACK_PATH) {
      auth
        .completeLogin(window.location.search)
        .then((returnTo) => {
          if (!live) return;
          installAuthFetch();
          keepFresh();
          setPhase("ready");
          router.replace(returnTo);
        })
        .catch(fail);
    } else if (getTokens()) {
      installAuthFetch();
      keepFresh();
      setPhase("ready");
    } else {
      startLogin(window.location.pathname + window.location.search).catch(fail);
    }
    return () => {
      live = false;
    };
    // The callback runs once per mount; later navigations keep the tokens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(
    () =>
      subscribeTokens(() => {
        if (getTokens() === null && oidc() && !oidc()!.justLoggedIn()) {
          setPhase("working");
          startLogin(window.location.pathname + window.location.search).catch(() => setPhase("error"));
        }
      }),
    [],
  );

  if (phase === "ready") return <>{children}</>;
  return (
    <div data-testid="auth-gate" className="flex h-dvh items-center justify-center bg-canvas p-6 text-foreground">
      {phase === "error" ? (
        <div role="alert" className="flex max-w-sm flex-col items-start gap-3">
          <h1 className="text-title font-semibold">Sign-in did not complete</h1>
          <p data-testid="auth-error" className="text-label text-muted-foreground">{message}</p>
          <Button type="button" onClick={() => void startLogin("/").catch(() => undefined)}>Sign in again</Button>
        </div>
      ) : (
        <p data-testid="auth-working" role="status" className="text-label text-muted-foreground">Signing in…</p>
      )}
    </div>
  );
}
