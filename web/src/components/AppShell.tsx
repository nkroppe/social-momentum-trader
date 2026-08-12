import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchHealth } from "../api";
import { setToken } from "../auth";

const links = [
  { to: "/", label: "Overview" },
  { to: "/positions", label: "Positions" },
  { to: "/history", label: "History" },
  { to: "/strategies", label: "Strategies" },
  { to: "/decisions", label: "Decisions" },
  { to: "/risk", label: "Risk" },
];

export function AppShell({ onSignOut }: { onSignOut: () => void }) {
  const health = useQuery({ queryKey: ["health"], queryFn: fetchHealth, refetchInterval: 15_000 });
  const h = health.data;

  return (
    <div className="app">
      <nav className="nav">
        <div className="brand">
          SMT <span>MONITOR</span>
        </div>
        {links.map((link) => (
          <NavLink key={link.to} to={link.to} end={link.to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>
            {link.label}
          </NavLink>
        ))}
        <div className="nav-foot">
          <div>
            {h ? `${h.mode} · soak ${h.soak_days.toFixed(1)}d` : "…"}
            {h?.kill_active ? " · KILL" : ""}
          </div>
          <button
            type="button"
            onClick={() => {
              setToken("");
              onSignOut();
            }}
          >
            Sign out
          </button>
        </div>
      </nav>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
