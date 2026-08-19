import { useEffect } from "react";
import { BrowserRouter, Routes, Route, Navigate, Outlet } from "react-router-dom";
import { AppShell } from "./shell";
import { AuthBoundary } from "@/features/auth/components/AuthBoundary";
import { LoginScreen } from "@/features/auth/components/LoginScreen";
import {
  OverviewPage,
  LivePage,
  RecordingsPage,
  AnalysisPage,
  AlertsPage,
  ApprovalsPage,
  AdminPage,
} from "./shell";
import { useAuthStore } from "@/features/auth/hooks/useAuthStore";
import { ErrorBoundary } from "@/errors/ErrorBoundary";
import { FeatureGate } from "@/config/featureFlags";

// Protected layout with AppShell and ErrorBoundary
function ProtectedLayout() {
  return (
    <ErrorBoundary featureName="Application Shell">
      <AppShell>
        <AuthBoundary>
          <Outlet />
        </AuthBoundary>
      </AppShell>
    </ErrorBoundary>
  );
}

function App() {
  const initialize = useAuthStore((state) => state.initialize);

  useEffect(() => {
    void initialize();
  }, [initialize]);

  return (
    <ErrorBoundary featureName="HotelOps AI Root">
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginScreen />} />
          <Route element={<ProtectedLayout />}>
            <Route path="overview" element={<OverviewPage />} />
            <Route
              path="live"
              element={
                <FeatureGate flag="liveMonitoring" fallback={<Navigate to="/overview" replace />}>
                  <LivePage />
                </FeatureGate>
              }
            />
            <Route
              path="recordings"
              element={
                <FeatureGate flag="recordings" fallback={<Navigate to="/overview" replace />}>
                  <RecordingsPage />
                </FeatureGate>
              }
            />
            <Route
              path="analysis"
              element={
                <FeatureGate flag="deepAnalysis" fallback={<Navigate to="/overview" replace />}>
                  <AnalysisPage />
                </FeatureGate>
              }
            />
            <Route
              path="alerts"
              element={
                <FeatureGate flag="alerts" fallback={<Navigate to="/overview" replace />}>
                  <AlertsPage />
                </FeatureGate>
              }
            />
            <Route
              path="approvals"
              element={
                <FeatureGate flag="approvals" fallback={<Navigate to="/overview" replace />}>
                  <ApprovalsPage />
                </FeatureGate>
              }
            />
            <Route
              path="admin"
              element={
                <FeatureGate flag="adminSettings" fallback={<Navigate to="/overview" replace />}>
                  <AdminPage />
                </FeatureGate>
              }
            />
            <Route index element={<Navigate to="overview" replace />} />
            <Route path="*" element={<Navigate to="overview" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ErrorBoundary>
  );
}

export default App;
