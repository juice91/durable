"use client";

import React, { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { localAuth } from "@/lib/auth/local";
import { AuthScreen } from "./AuthScreen";
import { LocalSignIn } from "./LocalSignIn";
import { NoSignInMethod } from "./NoSignInMethod";
import { SetupAdmin } from "./SetupAdmin";

type View = "checking" | "setup" | "sign-in" | "unavailable" | "unreachable";

/** What a signed-out person sees under EAP_STUDIO_AUTH=local, as the API says: setup, sign-in, or why neither. */
export function LocalEntry() {
  const [view, setView] = useState<View>("checking");
  const [created, setCreated] = useState(false);

  const check = useCallback(() => {
    setView("checking");
    void localAuth()?.status().then(setView);
  }, []);
  useEffect(check, [check]);

  if (view === "setup") {
    return (
      <SetupAdmin
        signIn
        onDone={(made) => {
          setCreated(made);
          setView("sign-in");
        }}
      />
    );
  }
  if (view === "sign-in") return <LocalSignIn notice={created ? "The admin account is created. Sign in with it." : undefined} />;
  if (view === "unavailable") return <NoSignInMethod />;
  return (
    <AuthScreen>
      {view === "checking" ? (
        <p data-testid="auth-working" role="status" className="text-label text-muted-foreground">Loading…</p>
      ) : (
        <div role="alert" className="flex max-w-sm flex-col items-start gap-3">
          <h1 className="text-title font-semibold">Studio cannot reach the API</h1>
          <p className="text-label text-muted-foreground">
            Wait a moment and try again. If it stays, see why the API does not start:{" "}
            <code className="break-all font-mono">kubectl -n pydanticos logs deploy/api --tail=50</code>
            {" "}(local development: <code className="font-mono">.dev/logs/api.log</code>).
          </p>
          <Button type="button" onClick={check}>Try again</Button>
        </div>
      )}
    </AuthScreen>
  );
}
