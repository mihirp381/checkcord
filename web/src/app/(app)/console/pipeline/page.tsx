import { AdminOnly } from "@/components/ConsoleShell";
import Pipeline from "@/components/sections/Pipeline";

export default function PipelinePage() {
  return (
    <AdminOnly>
      <Pipeline />
    </AdminOnly>
  );
}
