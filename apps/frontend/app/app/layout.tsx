"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import {
  BarChart3,
  FileText,
  FlaskConical,
  KeyRound,
  LayoutDashboard,
  LogOut,
  MessagesSquare,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/app", label: "Overview", icon: LayoutDashboard },
  { href: "/app/playground", label: "Playground", icon: FlaskConical },
  { href: "/app/documents", label: "Documents", icon: FileText, soon: true },
  { href: "/app/chat", label: "Chat", icon: MessagesSquare, soon: true },
  { href: "/app/evals", label: "Evals", icon: BarChart3, soon: true },
  { href: "/app/api-keys", label: "API keys", icon: KeyRound },
];

export default function AppLayout({ children }: LayoutProps<"/app">) {
  const router = useRouter();
  const pathname = usePathname();
  const auth = useAuth();

  useEffect(() => {
    if (auth.status === "anonymous") router.replace("/login");
  }, [auth.status, router]);

  if (auth.status !== "authenticated") {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  return (
    <div className="flex flex-1">
      <aside className="flex w-60 shrink-0 flex-col border-r bg-sidebar p-3">
        <div className="px-2 py-3 font-semibold">AI Platform</div>
        <Separator className="mb-2" />
        <nav className="flex flex-1 flex-col gap-1">
          {NAV.map(({ href, label, icon: Icon, soon }) =>
            soon ? (
              <span
                key={href}
                className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground"
              >
                <Icon className="size-4" />
                {label}
                <Badge variant="outline" className="ml-auto text-[10px]">
                  soon
                </Badge>
              </span>
            ) : (
              <Link
                key={href}
                href={href}
                className={cn(
                  "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-sidebar-accent",
                  pathname === href && "bg-sidebar-accent font-medium",
                )}
              >
                <Icon className="size-4" />
                {label}
              </Link>
            ),
          )}
        </nav>
        <Separator className="my-2" />
        <div className="flex items-center gap-2 px-2">
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{auth.user.email}</div>
            <div className="text-xs text-muted-foreground capitalize">{auth.user.role}</div>
          </div>
          <Button variant="ghost" size="icon-sm" onClick={auth.logout} aria-label="Sign out">
            <LogOut />
          </Button>
        </div>
      </aside>
      <main className="flex-1 overflow-auto p-6">{children}</main>
    </div>
  );
}
