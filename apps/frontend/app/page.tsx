import { redirect } from "next/navigation";

export default function Home() {
  // /app redirects to /login when there is no session.
  redirect("/app");
}
