import { AdminOnly } from "@/components/ConsoleShell";
import Replacements from "@/components/sections/Replacements";

export default function ReplacementsPage() {
  return (
    <AdminOnly>
      <Replacements />
    </AdminOnly>
  );
}
