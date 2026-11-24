import type { Metadata } from "next";
import { ConsoleShell } from "@/components/ConsoleShell";

export const metadata: Metadata = { title: "Checkcord console" };

export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  return <ConsoleShell>{children}</ConsoleShell>;
}
