import { useState, useEffect } from "react";
import { NavLink } from "react-router-dom";
import { ChevronLeftIcon, ChevronRightIcon, HotelIcon, LogoutIcon } from "./icons";
import { IconButton, Button } from "@/components/ui";
import { useAuthStore, useAuthActions } from "@/features/auth/hooks/useAuthStore";
import type { SidebarProps } from "./types";
import "./Sidebar.css";

export function Sidebar({ isCollapsed, onToggle, navItems, activePath, onNavigate }: SidebarProps) {
  const [mounted, setMounted] = useState(false);
  const user = useAuthStore((state) => state.user);
  const { logout } = useAuthActions();

  useEffect(() => {
    setMounted(true);
  }, []);

  const handleNavClick = (path: string) => {
    onNavigate(path);
  };

  const handleLogout = () => {
    void logout();
  };

  return (
    <aside
      className={`sidebar ${isCollapsed ? "collapsed" : "expanded"} ${mounted ? "mounted" : ""}`}
      role="navigation"
      aria-label="Main navigation"
    >
      <div className="sidebar-header">
        <div className="sidebar-brand">
          {!isCollapsed && (
            <>
              <HotelIcon className="sidebar-brand-icon" aria-hidden="true" />
              <span className="sidebar-brand-text">HotelOps AI</span>
            </>
          )}
          {isCollapsed && <HotelIcon className="sidebar-brand-icon collapsed" aria-hidden="true" />}
        </div>
        {!isCollapsed && (
          <IconButton
            onClick={onToggle}
            aria-label="Collapse sidebar"
            aria-expanded="false"
            size="sm"
          >
            <ChevronLeftIcon aria-hidden="true" />
          </IconButton>
        )}
      </div>

      <nav className="sidebar-nav" aria-label="Primary">
        <ul className="sidebar-nav-list" role="list">
          {navItems.map((item) => (
            <li key={item.id} className="sidebar-nav-item">
              <NavLink
                to={item.path}
                className={({ isActive }) => `sidebar-nav-link ${isActive ? "active" : ""}`}
                onClick={() => {
                  handleNavClick(item.path);
                }}
                aria-current={activePath === item.path ? "page" : undefined}
              >
                <span className="sidebar-nav-icon" aria-hidden="true">
                  {item.icon}
                </span>
                {!isCollapsed && <span className="sidebar-nav-label">{item.label}</span>}
              </NavLink>
              {!isCollapsed && activePath === item.path && (
                <span className="sidebar-nav-indicator" aria-hidden="true" />
              )}
            </li>
          ))}
        </ul>
      </nav>

      {user && !isCollapsed && (
        <div className="sidebar-footer">
          <div className="sidebar-user">
            <div className="sidebar-user-avatar" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                <circle cx="12" cy="7" r="4" />
              </svg>
            </div>
            <div className="sidebar-user-info">
              <span className="sidebar-user-name">{user.display_name}</span>
              <span className="sidebar-user-email">{user.email}</span>
            </div>
          </div>
          <Button
            variant="ghost"
            size="sm"
            fullWidth
            onClick={handleLogout}
            className="sidebar-logout-btn"
          >
            <LogoutIcon aria-hidden="true" />
            <span>Sign out</span>
          </Button>
        </div>
      )}

      {isCollapsed && (
        <IconButton onClick={onToggle} aria-label="Expand sidebar" aria-expanded="true" size="sm">
          <ChevronRightIcon aria-hidden="true" />
        </IconButton>
      )}
    </aside>
  );
}
