import type { Metadata } from "next";
import { Geist_Mono, Inter } from "next/font/google";
import { Providers } from "@/lib/providers";
import "./globals.css";

// Inter is the design system's primary typeface. next/font self-hosts it at
// build time, so there is no render-blocking request to Google and no layout
// shift when it swaps in.
const inter = Inter({ variable: "--font-inter", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Clinetics — AI Clinic Scheduling",
  description:
    "Forecast appointment demand, generate optimal schedules with CP-SAT, and query it all in natural language.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${inter.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="min-h-full bg-surface font-sans text-on-primary-container">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
