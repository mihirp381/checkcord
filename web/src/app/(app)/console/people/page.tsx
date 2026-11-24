import { AdminOnly } from "@/components/ConsoleShell";
import People from "@/components/sections/People";

export default function PeoplePage() {
  return (
    <AdminOnly>
      <People />
    </AdminOnly>
  );
}
