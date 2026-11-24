import { headers } from "next/headers";
import "./globals.css";
import { inter, jetbrainsMono } from "./fonts";
import { AppShell } from "@/components/frame/AppShell";
import { AuthGate } from "@/components/auth/AuthGate";
import { runtimeConfigScript, runtimeSettings } from "@/lib/runtimeConfigScript";
import { StudioKeyboard } from "@/components/shortcuts/StudioKeyboard";
import { ThemeProvider } from "@/components/theme/ThemeProvider";
import { ThemeScript } from "@/components/theme/ThemeScript";

// The runtime-config script below reads the server's environment per request, so the page is never prerendered.
export const dynamic = "force-dynamic";

export const metadata = {
  title: "AI Platform Studio",
  description: "Studio for building and managing AI agents",
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { config, oidc } = runtimeSettings((await headers()).get("host"));
  // No theme class here: the head script sets `dark` and `color-scheme` from the stored preference
  // or the OS before paint, which is why <html> suppresses the hydration warning. <body> does too:
  // browser extensions (Grammarly's data-gr-* attributes, for example) write to it before React
  // hydrates. suppressHydrationWarning covers only the element's own attributes, one level deep, so
  // mismatches inside the app are still reported.
  return (
    <html lang="en" className={`${inter.variable} ${jetbrainsMono.variable}`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: runtimeConfigScript(config) }} />
        <ThemeScript />
      </head>
      <body suppressHydrationWarning>
        <ThemeProvider>
          <AuthGate enabled={oidc}>
            <AppShell>
              <StudioKeyboard>{children}</StudioKeyboard>
            </AppShell>
          </AuthGate>
        </ThemeProvider>
      </body>
    </html>
  );
}
