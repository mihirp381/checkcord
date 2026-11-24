import { AdminOnly } from "@/components/ConsoleShell";
import Audiences from "@/components/sections/Audiences";

export default function AudiencesPage() {
  return (
    <AdminOnly>
      <Audiences />
    </AdminOnly>
  );
}
