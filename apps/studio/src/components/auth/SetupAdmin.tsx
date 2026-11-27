"use client";

import React, { useEffect, useState } from "react";
import { Check, Circle } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { localAuth, LocalAuthError } from "@/lib/auth/local";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { AuthScreen } from "./AuthScreen";
import { Field, MIN_PASSWORD_LENGTH } from "./fields";

export const NEW_CODE_COMMAND = "kubectl -n pydanticos exec deploy/api -- python -m eap_iam_localadmin setup-code";

/** The code of a `#code=…` link. A fragment is never sent to a server, so it reaches no access log. */
function codeFromLink(): string {
  if (typeof window === "undefined") return "";
  return /^#code=([A-Za-z0-9-]{1,100})$/.exec(window.location.hash)?.[1] ?? "";
}

function Rule({ met, children }: { met: boolean; children: React.ReactNode }) {
  const Icon = met ? Check : Circle;
  return (
    <li data-met={met} className={`flex items-center gap-1.5 ${met ? "text-foreground" : "text-muted-foreground"}`}>
      <Icon className="size-icon shrink-0" strokeWidth={ICON_STROKE_WIDTH} aria-hidden />
      <span>{children}</span>
      <span className="sr-only">{met ? " (done)" : " (not yet)"}</span>
    </li>
  );
}

/**
 * First run (EAP_STUDIO_AUTH=local, no admin yet): create the one admin account. The one-time setup code proves the
 * person runs this deployment, so the first visitor cannot claim it. With `signIn` the new admin is signed in at once
 * (with the password just chosen); else, or when that fails, `onDone` says what happened.
 */
export function SetupAdmin({ signIn, onDone }: { signIn: boolean; onDone: (created: boolean) => void }) {
  const [linkCode] = useState(codeFromLink);
  const [code, setCode] = useState(linkCode);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Out of the address bar and the history once it is in the field.
  useEffect(() => {
    if (linkCode) window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search);
  }, [linkCode]);

  const longEnough = password.length >= MIN_PASSWORD_LENGTH;
  const notTheUsername = password !== "" && password.toLowerCase() !== username.toLowerCase();
  const matching = password !== "" && password === repeat;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const auth = localAuth();
    if (!auth || busy) return;
    if (!code.trim()) return setError("Enter the setup code. The deploy script printed it; see “Where do I find the setup code?”.");
    if (!username) return setError("Choose a username.");
    if (!longEnough) return setError(`The password must have at least ${MIN_PASSWORD_LENGTH} characters.`);
    if (!notTheUsername) return setError("The password must not be the username.");
    if (!matching) return setError("The two passwords do not match. Type the same one twice.");
    setBusy(true);
    setError(null);
    try {
      if ((await auth.setUp(code, username, password)) === "closed") return onDone(false);
      // The account exists now; a failed sign-in just leads to the form.
      if (signIn && (await auth.signIn(username, password).catch(() => null)) === "signed-in") return;
      onDone(true);
    } catch (reason) {
      setError(reason instanceof LocalAuthError ? reason.message : "The setup failed. Try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthScreen>
      <form data-testid="setup-form" onSubmit={(event) => void submit(event)} className="flex w-full max-w-sm flex-col gap-3" method="post" noValidate>
        <h1 className="text-title font-semibold">Set up the admin account</h1>
        <p className="text-label text-muted-foreground">Welcome. Choose a username and password to finish.</p>
        <Field id="setup-code" label="Setup code" autoComplete="off" autoFocus={!linkCode} value={code} onChange={(e) => setCode(e.target.value)} />
        <details className="text-label text-muted-foreground">
          <summary className="cursor-pointer">Where do I find the setup code?</summary>
          <p className="mt-1">The deploy script prints it, with a link that fills it in, when it finishes. For a new one:</p>
          <code className="mt-1 block break-all font-mono text-caption">{NEW_CODE_COMMAND}</code>
          <p className="mt-1">Docker Compose: <code className="font-mono">docker compose exec api setup-link</code>.</p>
          <p className="mt-1">Local development: <code className="font-mono">bash scripts/dev-up.sh api</code>.</p>
        </details>
        <Field id="username" label="Username" autoComplete="username" autoFocus={Boolean(linkCode)} value={username} onChange={(e) => setUsername(e.target.value)} />
        <Field id="password" label="Password" type="password" autoComplete="new-password" aria-describedby="password-rules" value={password} onChange={(e) => setPassword(e.target.value)} />
        <Field id="repeat-password" label="Repeat the password" type="password" autoComplete="new-password" aria-describedby="password-rules" value={repeat} onChange={(e) => setRepeat(e.target.value)} />
        <ul id="password-rules" className="flex flex-col gap-0.5 text-caption">
          <Rule met={longEnough}>At least {MIN_PASSWORD_LENGTH} characters</Rule>
          <Rule met={notTheUsername}>Not your username</Rule>
          <Rule met={matching}>Both entries match</Rule>
        </ul>
        {error ? <p role="alert" data-testid="setup-error" className="text-label text-destructive-text">{error}</p> : null}
        <Button type="submit" variant="primary" size="lg" data-testid="setup-submit" disabled={busy}>Create account and continue</Button>
      </form>
    </AuthScreen>
  );
}
