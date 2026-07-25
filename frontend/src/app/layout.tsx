import type { Metadata } from "next";
import { IBM_Plex_Sans, IBM_Plex_Mono } from "next/font/google";
import "./globals.css";

import { AppShell } from "@/components/layout/app-shell";
import { SessionGate } from "@/components/auth/session-gate";
import { Providers } from "./providers";

// Plex was drawn for technical documentation, and Sans/Mono are one
// superfamily -- so the "citation typeface" rule reads as a register shift
// inside one voice rather than two fonts arguing.
const plexSans = IBM_Plex_Sans({
  variable: "--font-plex-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "DataTalk",
  description: "Ask your warehouse a question. Every number cites its query.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${plexSans.variable} ${plexMono.variable} h-full antialiased`}
    >
      <body className="min-h-full">
        <Providers>
          <SessionGate>
            <AppShell>{children}</AppShell>
          </SessionGate>
        </Providers>
      </body>
    </html>
  );
}
