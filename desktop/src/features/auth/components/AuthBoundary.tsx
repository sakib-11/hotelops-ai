import { useEffect } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { LoadingState } from "@/components/ui";
import { LoginScreen } from "./LoginScreen";
import { useAuthStore, useAuthStatus, useIsInitializing } from "../hooks/useAuthStore";
import "./AuthBoundary.css";

interface AuthBoundaryProps {
  /** If true, redirect authenticated users away (e.g., login page) */
  requireUnauthenticated?: boolean;
  /** Required permissions for access (future use) */
  requiredPermissions?: string[];
  /** Child elements to render when authenticated (e.g., Outlet) */
  children?: React.ReactNode;
}

/**
 * Authentication Boundary Component
 *
 * Protects routes by checking authentication status.
 * - Unauthenticated users → LoginScreen
 * - Initializing → LoadingState
 * - Authenticated → children (or Outlet if no children)
 * - Session expired → LoginScreen with session expired message
 */
export function AuthBoundary({
  requireUnauthenticated = false,
  requiredPermissions = [],
  children,
}: AuthBoundaryProps) {
  const status = useAuthStatus();
  const isInitializing = useIsInitializing();
  const location = useLocation();

  // Handle session expiration
  useEffect(() => {
    if (status === "session_expired") {
      console.warn("[AuthBoundary] Session expired, redirecting to login");
    }
  }, [status]);

  // If requiring unauthenticated (e.g., login page)
  if (requireUnauthenticated) {
    if (isInitializing) {
      return <LoadingState size="lg" variant="spinner" label="Checking session..." fullScreen />;
    }

    if (status === "authenticated") {
      // Redirect authenticated users to overview
      return <Navigate to="/overview" replace />;
    }

    // Show login for unauthenticated
    return <LoginScreen />;
  }

  // Protected route - require authentication
  if (isInitializing) {
    return <LoadingState size="lg" variant="spinner" label="Restoring session..." fullScreen />;
  }

  if (status === "authenticated") {
    // Check permissions if specified (future enhancement)
    if (requiredPermissions.length > 0) {
      // TODO: Implement permission check when backend provides user permissions
      // For now, allow access if authenticated
    }
    // Render children if provided, otherwise use Outlet for nested routes
    return children ?? <Outlet />;
  }

  // Not authenticated - redirect to login with return path
  return <Navigate to="/login" replace state={{ from: location }} />;
}

/**
 * Public route wrapper - for pages that should be accessible without auth
 * (e.g., landing page, password reset if implemented)
 */
export function PublicRoute({ children }: { children: React.ReactNode }) {
  const isInitializing = useIsInitializing();

  if (isInitializing) {
    return <LoadingState size="lg" variant="spinner" label="Loading..." fullScreen />;
  }

  return <>{children}</>;
}

/**
 * Hook to check if current route requires authentication
 * Useful for conditional rendering in layouts
 */
export function useRequiresAuth(): boolean {
  // All routes in the app shell require auth except login
  return true;
}

/**
 * Permission checking hook (for future use)
 */
export function usePermissions(): string[] {
  const user = useAuthStore((state) => state.user);
  // In real implementation, this would come from user.permissions
  // For now, return empty array
  return user?.permissions ?? [];
}

export function useHasPermission(permission: string): boolean {
  const permissions = usePermissions();
  return permissions.includes(permission);
}

export function useHasAnyPermission(permissions: string[]): boolean {
  const userPermissions = usePermissions();
  return permissions.some((p) => userPermissions.includes(p));
}

export function useHasRole(role: string): boolean {
  const user = useAuthStore((state) => state.user);
  return user?.role_name === role;
}
