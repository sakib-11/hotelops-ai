import { useState, useCallback } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { MainArea } from "./MainArea";
import { PageHeader, Badge } from "@/components/ui";
import { useWebSocketStatus } from "@/realtime";
import type { AppShellProps, NavItem } from "./types";
import {
  HomeIcon,
  LiveIcon,
  RecordingsIcon,
  AnalysisIcon,
  AlertsIcon,
  ApprovalsIcon,
  AdminIcon,
} from "./icons";
import "./AppShell.css";

const NAV_ITEMS: NavItem[] = [
  { id: "overview", label: "Overview", icon: <HomeIcon />, path: "/overview" },
  { id: "live", label: "Live", icon: <LiveIcon />, path: "/live" },
  { id: "recordings", label: "Recordings", icon: <RecordingsIcon />, path: "/recordings" },
  { id: "analysis", label: "Analysis", icon: <AnalysisIcon />, path: "/analysis" },
  { id: "alerts", label: "Alerts", icon: <AlertsIcon />, path: "/alerts" },
  { id: "approvals", label: "Approvals", icon: <ApprovalsIcon />, path: "/approvals" },
  { id: "admin", label: "Admin", icon: <AdminIcon />, path: "/admin" },
];

const ROUTE_TITLES: Record<string, string> = {
  "/overview": "Overview",
  "/live": "Live Monitoring",
  "/recordings": "Recordings",
  "/analysis": "Analysis",
  "/alerts": "Alerts",
  "/approvals": "Approvals",
  "/admin": "Administration",
};

const ROUTE_DESCRIPTIONS: Record<string, string> = {
  "/overview": "Real-time operational dashboard showing key metrics and system health.",
  "/live": "Real-time video feeds and live analytics from all connected cameras.",
  "/recordings": "Browse, search, and manage recorded video footage.",
  "/analysis": "Deep-dive analytical tools for occupancy trends and patterns.",
  "/alerts": "Real-time alert management with acknowledgment workflows.",
  "/approvals": "Review and approve AI-generated recommendations and evidence.",
  "/admin": "System configuration, user management, and tenant settings.",
};

const ROUTE_BREADCRUMBS: Record<string, { label: string; path?: string }[]> = {
  "/overview": [],
  "/live": [],
  "/recordings": [],
  "/analysis": [],
  "/alerts": [],
  "/approvals": [],
  "/admin": [],
};

const noop = () => {
  // Navigation is handled by NavLink
};

export function AppShell({ children }: AppShellProps) {
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const location = useLocation();

  const currentPath = location.pathname;
  const activePath =
    NAV_ITEMS.find((item) => currentPath.startsWith(item.path))?.path ?? "/overview";

  const handleSidebarToggle = useCallback(() => {
    setIsSidebarCollapsed((prev) => !prev);
  }, []);

  const pageTitle = ROUTE_TITLES[activePath] ?? "HotelOps AI";
  const pageDescription = ROUTE_DESCRIPTIONS[activePath];
  const breadcrumbs = ROUTE_BREADCRUMBS[activePath] ?? [];

  const sidebarWidth = isSidebarCollapsed
    ? "var(--sidebar-width-collapsed)"
    : "var(--sidebar-width-expanded)";

  const wsStatus = useWebSocketStatus();

  const getWsBadge = () => {
    switch (wsStatus) {
      case "CONNECTED":
        return (
          <Badge variant="success" size="sm" dot>
            Live
          </Badge>
        );
      case "CONNECTING":
      case "RECONNECTING":
        return (
          <Badge variant="warning" size="sm" dot>
            Reconnecting
          </Badge>
        );
      case "ERROR":
      case "DISCONNECTED":
      default:
        return (
          <Badge variant="default" size="sm" dot>
            Offline
          </Badge>
        );
    }
  };

  return (
    <div className="app-shell">
      <Sidebar
        isCollapsed={isSidebarCollapsed}
        onToggle={handleSidebarToggle}
        navItems={NAV_ITEMS}
        activePath={activePath}
        onNavigate={noop}
        user={{ name: "Demo User", email: "demo@hotelops.ai" }}
      />
      <div className="app-shell-main" style={{ marginLeft: sidebarWidth }}>
        <MainArea
          header={
            <PageHeader
              title={pageTitle}
              description={pageDescription}
              breadcrumbs={breadcrumbs}
              action={getWsBadge()}
            />
          }
        >
          {children}
          <Outlet />
        </MainArea>
      </div>
    </div>
  );
}
