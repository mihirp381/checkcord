import { AdminOnly } from "@/components/ConsoleShell";
import Audit from "@/components/sections/Audit";

export default function AuditPage() {
  return (
    <AdminOnly>
      <Audit />
    </AdminOnly>
  );
}
