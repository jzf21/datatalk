import { redirect } from "next/navigation";

/** House rules moved under Settings; keep old links working. */
export default function MemoryRedirect() {
  redirect("/settings/rules");
}
