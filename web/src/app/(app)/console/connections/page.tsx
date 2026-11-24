import { AdminOnly } from "@/components/ConsoleShell";
import Connections from "@/components/sections/Connections";

export default function ConnectionsPage() {
  return (
    <AdminOnly>
      <Connections />
    </AdminOnly>
  );
}
