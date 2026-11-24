import { AdminOnly } from "@/components/ConsoleShell";
import Overview from "@/components/sections/Overview";

export default function OverviewPage() {
  return (
    <AdminOnly home>
      <Overview />
    </AdminOnly>
  );
}
