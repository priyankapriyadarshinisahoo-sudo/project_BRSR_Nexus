"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { HealthDot } from "@/components/HealthDot";
import { NotificationBell } from "@/components/NotificationBell";
import { useAuth } from "@/components/AuthProvider";

const NAV: Record<string, { href: string; label: string }[]> = {
  ADMIN: [
    { href: "/", label: "Overview" },
    { href: "/entities", label: "Entities" },
    { href: "/framework", label: "Framework" },
    { href: "/assurance", label: "Assurance" },
    { href: "/audit", label: "Audit" },
  ],
  ESG_MANAGER: [
    { href: "/", label: "Control Tower" },
    { href: "/review", label: "Review Queue" },
    { href: "/exceptions", label: "Exceptions" },
    { href: "/assurance", label: "Assurance" },
    { href: "/consolidation", label: "Consolidation" },
    { href: "/reports", label: "Reports" },
    { href: "/trends", label: "Trends" },
    { href: "/entities", label: "Entities" },
    { href: "/framework", label: "Framework" },
    { href: "/audit", label: "Audit" },
  ],
  MANAGEMENT: [
    { href: "/", label: "Dashboard" },
    { href: "/trends", label: "Trends" },
    { href: "/reports", label: "Reports" },
    { href: "/consolidation", label: "KPIs" },
    { href: "/framework", label: "Framework" },
  ],
  REVIEWER: [
    { href: "/", label: "Overview" },
    { href: "/review", label: "Review Queue" },
    { href: "/exceptions", label: "Exceptions" },
    { href: "/assurance", label: "Assurance" },
    { href: "/framework", label: "Framework" },
  ],
  ASSESSOR: [
    { href: "/", label: "Overview" },
    { href: "/assurance", label: "Assurance Room" },
    { href: "/exceptions", label: "Exceptions" },
    { href: "/framework", label: "Framework" },
  ],
  DATA_OWNER: [
    { href: "/", label: "My Dashboard" },
    { href: "/assignments", label: "My Assignments" },
    { href: "/import", label: "Bulk Import" },
    { href: "/exceptions", label: "My Exceptions" },
    { href: "/assurance", label: "Assurance" },
    { href: "/framework", label: "Framework" },
  ],
};

export function AppShellNav() {
  const { user, loading, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!loading && !user && pathname !== "/login") router.push("/login");
  }, [loading, user, pathname, router]);

  if (loading || !user) return null;
  const items = NAV[user.role] ?? [];

  return (
    <header className="topbar">
      <span className="brand"><span className="brand-mark">BR</span>BRSR Portal</span>
      <HealthDot />
      <NotificationBell />
      <nav className="nav">
        {items.map((i) => (
          <Link key={i.href} href={i.href} className={pathname === i.href ? "active" : ""}>
            {i.label}
          </Link>
        ))}
      </nav>
      <span className="userchip">
        {user.full_name} · {user.role.replace("_", " ")}
      </span>
      <button className="ghostbtn" onClick={() => logout().then(() => router.push("/login"))}>
        Sign out
      </button>
    </header>
  );
}
