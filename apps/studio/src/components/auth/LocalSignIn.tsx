"use client";

import React, { useState } from "react";
import { Button } from "@/components/ui/Button";
import { localAuth, LocalAuthError } from "@/lib/auth/local";
import { AuthScreen } from "./AuthScreen";
import { Field, MIN_PASSWORD_LENGTH } from "./fields";

/**
 * The built-in admin's sign-in (EAP_STUDIO_AUTH=local): username and password, then, for an initial or reset password,
 * the forced change. What is typed lives in this component's state only and goes to the API.
 */
export function LocalSignIn({ notice }: { notice?: string }) {
  const [step, setStep] = useState<"sign-in" | "change">("sign-in");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const auth = localAuth();
    if (!auth || busy) return;
    if (step === "change") {
      if (next.length < MIN_PASSWORD_LENGTH) return setError(`The password must have at least ${MIN_PASSWORD_LENGTH} characters.`);
      if (next !== repeat) return setError("The two new passwords do not match.");
    }
    setBusy(true);
    setError(null);
    try {
      if (step === "sign-in") {
        if ((await auth.signIn(username, password)) === "change-required") setStep("change");
      } else {
        await auth.changePassword(username, password, next);
      }
    } catch (reason) {
      setError(reason instanceof LocalAuthError ? reason.message : "Sign-in failed. Try again.");
      if (step === "sign-in") setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthScreen>
      <form data-testid="local-sign-in-form" onSubmit={(event) => void submit(event)} className="flex w-full max-w-sm flex-col gap-3" method="post" noValidate>
        <h1 className="text-title font-semibold">{step === "sign-in" ? "Sign in" : "Choose a new password"}</h1>
        {notice && step === "sign-in" ? <p role="status" className="text-label text-muted-foreground">{notice}</p> : null}
        {step === "sign-in" ? (
          <>
            <Field id="username" label="Username" autoComplete="username" autoFocus value={username} onChange={(e) => setUsername(e.target.value)} />
            <Field id="password" label="Password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </>
        ) : (
          <>
            <p className="text-label text-muted-foreground">
              This password was set by the deployment or a reset. Choose your own, at least {MIN_PASSWORD_LENGTH} characters, to continue.
            </p>
            {/* Password managers attach the new password to this account. */}
            <input type="text" name="username" autoComplete="username" value={username} readOnly hidden />
            <Field id="new-password" label="New password" type="password" autoComplete="new-password" autoFocus value={next} onChange={(e) => setNext(e.target.value)} />
            <Field id="repeat-password" label="Repeat the new password" type="password" autoComplete="new-password" value={repeat} onChange={(e) => setRepeat(e.target.value)} />
          </>
        )}
        {error ? <p role="alert" data-testid="local-sign-in-error" className="text-label text-destructive-text">{error}</p> : null}
        <Button type="submit" variant="primary" size="lg" data-testid="local-sign-in-submit" disabled={busy}>
          {step === "sign-in" ? "Sign in" : "Change password and sign in"}
        </Button>
      </form>
    </AuthScreen>
  );
}
