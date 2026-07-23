import {
  CloudCog,
  Database,
  Disc3,
  Gauge,
  Layers3,
  ListChecks,
  LockKeyhole,
  LogOut,
  Menu,
  Plus,
  KeyRound,
  Server,
  Settings2,
  Network,
} from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";
import { useState } from "react";
import { api } from "../api/client";
import { useAuth } from "../store/auth";
import { PageErrorBoundary } from "./PageErrorBoundary";
import { useLocation } from "react-router-dom";
const links = [
  ["/", Gauge, "Overview"],
  ["/instances", Server, "Instances"],
  ["/amis", Disc3, "AMIs"],
  ["/volumes", Database, "Volumes"],
  ["/security-groups", LockKeyhole, "Security groups"],
  ["/key-pairs", KeyRound, "Key pairs"],
  ["/launch-templates", Layers3, "Launch templates"],
  ["/autoscaling", Settings2, "Autoscaling"],
  ["/load-balancers", Network, "Load balancers"],
  ["/operations", ListChecks, "Operations"],
] as const;
export function Layout() {
  const [open, setOpen] = useState(false);
  const user = useAuth((s) => s.user);
  const clear = useAuth((s) => s.clear);
  const location = useLocation();
  return (
    <div className="shell">
      <aside className={open ? "sidebar open" : "sidebar"}>
        <div className="brand">
          <CloudCog />
          <span>LocalEC2</span>
        </div>
        <nav>
          {links.map(([to, Icon, label]) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              onClick={() => setOpen(false)}
            >
              <Icon />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="profile">
          <div className="avatar">
            {user?.display_name.slice(0, 1).toUpperCase()}
          </div>
          <div>
            <strong>{user?.display_name}</strong>
            <small>{user?.email}</small>
          </div>
          <button
            aria-label="Log out"
            onClick={() => api.post("/auth/logout").finally(clear)}
          >
            <LogOut />
          </button>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <button className="menu" onClick={() => setOpen((v) => !v)}>
            <Menu />
          </button>
          <span>Local cloud workspace</span>
          <NavLink className="quick" to="/instances/new">
            <Plus />
            Launch instance
          </NavLink>
        </header>
        <div className="content">
          <PageErrorBoundary resetKey={location.pathname}>
            <Outlet />
          </PageErrorBoundary>
        </div>
      </main>
    </div>
  );
}
