import { AdminOnly } from "@/components/ConsoleShell";
import Records from "@/components/sections/Records";

export default function RecordsPage() {
  return (
    <AdminOnly>
      <Records />
    </AdminOnly>
  );
}
