import { AdminOnly } from "@/components/ConsoleShell";
import Keys from "@/components/sections/Keys";

export default function KeysPage() {
  return (
    <AdminOnly>
      <Keys />
    </AdminOnly>
  );
}
