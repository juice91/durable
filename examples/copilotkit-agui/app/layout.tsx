import "@copilotkit/react-core/v2/styles.css";
import "./globals.css";

import type { ReactNode } from "react";

export const metadata = {
  title: "PydanticOS + CopilotKit",
  description: "A CopilotKit frontend for a published PydanticOS agent over AG-UI.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
