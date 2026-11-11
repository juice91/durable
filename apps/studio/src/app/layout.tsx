import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";
import { navSections, allNavItems } from "@/lib/nav";

const pages = allNavItems.map((item) => item.href);

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
