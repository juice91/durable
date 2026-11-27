import React from "react";
import { AuthScreen } from "./AuthScreen";

/** Shown when the server's sign-in setting cannot work here. The setting at fault is named in the server log only. */
export function NoSignInMethod() {
  return (
    <AuthScreen>
      <div data-testid="no-sign-in" className="flex max-w-md flex-col gap-3">
        <h1 className="text-title font-semibold">No sign-in method is configured</h1>
        <p className="text-label text-muted-foreground">
          Nobody can sign in to this Studio yet. If you run it, set <code className="font-mono">EAP_STUDIO_AUTH</code> to{" "}
          <code className="font-mono">local</code> (one built-in admin account),{" "}
          <code className="font-mono">zitadel</code> (several users and roles) or{" "}
          <code className="font-mono">access</code> (Cloudflare Access in front), add that mode&apos;s settings and restart Studio.
        </p>
        <p className="text-label text-muted-foreground">
          The Studio server log says what is missing. Setup: <code className="font-mono">docs/admin-account.md</code>.
        </p>
      </div>
    </AuthScreen>
  );
}
