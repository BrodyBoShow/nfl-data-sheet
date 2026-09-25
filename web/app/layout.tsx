import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import Link from "next/link";
import type { ReactNode } from "react";

import { StatusLine } from "@/components/status-line";
import { formatEtStamp } from "@/lib/format";

import "./tokens.css";
import "./base.css";

// Self-hosted at build time by next/font, so there's no runtime call to Google.
const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-sans",
  display: "swap",
});
const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: { default: "NFL Data Sheet", template: "%s · NFL Data Sheet" },
  description:
    "Read-only NFL reference: team efficiency, market, and environment signals, and " +
    "per-game matchup cards with their backtest record attached.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      <body>
        <header className="site-header">
          <div className="shell header-row">
            <Link href="/" className="wordmark t-head">
              NFL Data Sheet
            </Link>
            <nav className="site-nav t-cap" aria-label="Site">
              <Link href="/method">Method</Link>
              <Link href="/sources">Sources</Link>
            </nav>
          </div>
          <StatusLine />
        </header>
        <main className="shell">{children}</main>
        <footer className="site-footer">
          <div className="shell footer-row t-small">
            <Link href="/method">Method and backtest</Link>
            <Link href="/sources">Sources and licenses</Link>
            <span>Not affiliated with the NFL.</span>
            <span className="mono">page built {formatEtStamp(new Date())}</span>
          </div>
        </footer>
      </body>
    </html>
  );
}
