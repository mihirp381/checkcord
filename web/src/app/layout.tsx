import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { connection } from "next/server";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Checkcord",
  description: "Checkcord admin console and repository context graph",
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  await connection();
  const apiUrl =
    process.env.CHECKCORD_API_URL || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <head>
        <meta name="checkcord-api-url" content={apiUrl} />
      </head>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
