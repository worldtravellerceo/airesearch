import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";
import { ThemeToggle } from "@/components/ThemeToggle";

export const metadata: Metadata = {
  title: "AI Radar",
  description:
    "GitHub'daki yapay zeka ekosistemi: mutlak popülerlik ve momentum ayrı ayrı sıralanmış.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="tr" suppressHydrationWarning>
      <head>
        {/* Applied before paint so a dark-mode reader never sees a white flash.
            Storage can throw in private windows, so the whole thing is guarded
            and the OS preference is the fallback. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `try{var t=localStorage.getItem('airadar-theme');if(t==='light'||t==='dark'){document.documentElement.setAttribute('data-theme',t)}}catch(e){}`,
          }}
        />
      </head>
      <body className="min-h-screen">
        <header className="border-border bg-surface-1/80 sticky top-0 z-10 border-b backdrop-blur">
          <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3">
            <Link href="/" className="text-ink font-semibold tracking-tight">
              AI Radar
            </Link>
            <nav className="text-ink-secondary flex gap-4 text-sm">
              <Link href="/" className="hover:text-ink">
                Board&apos;lar
              </Link>
              <Link href="/compare" className="hover:text-ink">
                Karşılaştır
              </Link>
            </nav>
            <div className="ml-auto">
              <ThemeToggle />
            </div>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
        <footer className="text-ink-muted mx-auto max-w-7xl px-4 py-8 text-xs">
          Veri kaynağı: GitHub REST API{" "}
          <code>stargazers/history</code> (API sürümü 2026-03-10). Günde bir güncellenir.
        </footer>
      </body>
    </html>
  );
}
