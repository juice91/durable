import "./globals.css";
import { Layout } from "@/components/layout/Layout";

export const metadata = {
  title: "AI Platform Studio",
  description: "Studio for building and managing AI agents",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body>
        <Layout>{children}</Layout>
      </body>
    </html>
  );
}
