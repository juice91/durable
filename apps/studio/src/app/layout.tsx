import "./globals.css";
import { inter, jetbrainsMono } from "./fonts";
import { Layout } from "@/components/layout/Layout";
import { ThemeProvider } from "@/components/theme/ThemeProvider";
import { ThemeScript } from "@/components/theme/ThemeScript";

export const metadata = {
  title: "AI Platform Studio",
  description: "Studio for building and managing AI agents",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // No theme class here: the head script sets `dark` and `color-scheme` from the stored preference
  // or the OS before paint, which is why <html> suppresses the hydration warning.
  return (
    <html lang="en" className={`${inter.variable} ${jetbrainsMono.variable}`} suppressHydrationWarning>
      <head>
        <ThemeScript />
      </head>
      <body>
        <ThemeProvider>
          <Layout>{children}</Layout>
        </ThemeProvider>
      </body>
    </html>
  );
}
