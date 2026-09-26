import { redirect } from "next/navigation";

// Land on the composer: asking a question is the product's front door.
export default function Home() {
  redirect("/reports/new");
}
