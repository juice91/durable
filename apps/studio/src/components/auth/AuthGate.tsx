"use client";

import React, { useEffect, useState, useSyncExternalStore } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Button } from "@/components/ui/Button";
import { AuthFlowError, CALLBACK_PATH } from "@/lib/auth/oidc";
import { getTokens, subscribeTokens } from "@/lib/auth/session";
import { installAuthFetch, keepFresh, oidc, setGiveUpHandler, signOut, startLogin } from "@/lib/auth/controller";
import { AuthScreen } from "./AuthScreen";
import { LocalEntry } from "./LocalEntry";
import { SetupAdmin } from "./SetupAdmin";
import { routes } from "@/lib/routes";
import { localAuth, SETUP_PATH } from "@/lib/auth/local";

type Phase = "working" | "ready" | "error";

/**
 * Holds the page back until the person is signed in. Under `EAP_STUDIO_AUTH=zitadel` (`enabled`) it runs the
 * authorization-code + PKCE flow: no token -> redirect to ZITADEL; /auth/callback -> exchange the code and go back to
 * the page the person wanted. Under `local` it shows the built-in admin's setup page or sign-in form. With neither (Cloudflare Access,
 * and the SSH-tunnel loopback page) it renders its children untouched and does nothing.
 */
export function AuthGate({
  enabled, local = false, localSetup = false, children,
}: { enabled: boolean; local?: boolean; localSetup?: boolean; children: React.ReactNode }) {
  if (local) return <LocalGate>{children}</LocalGate>;
  if (!enabled) return localSetup ? <SetupRoute>{children}</SetupRoute> : <>{children}</>;
  return <ZitadelGate>{children}</ZitadelGate>;
}

/** The built-in admin: setup or the sign-in form until there is a session, and again as soon as there is none. */
function LocalGate({ children }: { children: React.ReactNode }) {
  const signedIn = useSyncExternalStore(subscribeTokens, () => getTokens() !== null, () => false);
  const [refused, setRefused] = useState<string | null>(null);

  useEffect(() => {
    setGiveUpHandler(setRefused);
    return () => setGiveUpHandler(null);
  }, []);

  useEffect(() => {
    if (!signedIn) return;
    installAuthFetch();
    keepFresh();
  }, [signedIn]);

  if (!signedIn) return <LocalEntry />;
  if (refused) {
    return (
      <AuthScreen>
        <div role="alert" className="flex max-w-sm flex-col items-start gap-3">
          <h1 className="text-title font-semibold">Sign-in did not complete</h1>
          <p data-testid="auth-error" className="text-label text-muted-foreground">{refused}</p>
          <Button type="button" onClick={() => { setRefused(null); void signOut(); }}>Sign in again</Button>
        </div>
      </AuthScreen>
    );
  }
  return <>{children}</>;
}

/**
 * `/setup` on a page that needs no sign-in (the loopback dev bearer, a development token): the deploy script's link must
 * still create the admin. Nobody is signed in by it; every other path is untouched.
 */
function SetupRoute({ children }: { children: React.ReactNode }) {
  const onSetup = usePathname() === SETUP_PATH;
  const router = useRouter();
  const [view, setView] = useState<"checking" | "setup" | "done">("checking");

  useEffect(() => {
    if (!onSetup) return;
    let live = true;
    void localAuth()?.status().then((status) => {
      if (!live) return;
      if (status === "setup") return setView("setup");
      setView("done");
      router.replace(routes.agents());
    });
    return () => {
      live = false;
    };
    // The check runs when the path becomes /setup.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onSetup]);

  if (!onSetup || view === "done") return <>{children}</>;
  if (view === "setup") return <SetupAdmin signIn={false} onDone={() => { setView("done"); router.replace(routes.agents()); }} />;
  return (
    <AuthScreen>
      <p data-testid="auth-working" role="status" className="text-label text-muted-foreground">Loading…</p>
    </AuthScreen>
  );
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
    <AuthScreen>
      {phase === "error" ? (
        <div role="alert" className="flex max-w-sm flex-col items-start gap-3">
          <h1 className="text-title font-semibold">Sign-in did not complete</h1>
          <p data-testid="auth-error" className="text-label text-muted-foreground">{message}</p>
          <Button type="button" onClick={() => void startLogin("/").catch(() => undefined)}>Sign in again</Button>
        </div>
      ) : (
        <p data-testid="auth-working" role="status" className="text-label text-muted-foreground">Signing in…</p>
      )}
    </AuthScreen>
  );
}
