import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { BreakDetail } from "./pages/BreakDetail";
import { Breaks } from "./pages/Breaks";
import { Dashboard } from "./pages/Dashboard";
import { Login } from "./pages/Login";
import { ThemeProvider } from "./ThemeContext";
import type { ReactElement } from "react";

function RequireAuth({ children }: { children: ReactElement }) {
  const { ready, config, tokens } = useAuth();
  const location = useLocation();
  if (!ready) {
    return <p className="loading-state">Loading…</p>;
  }
  if (config && !config.authDisabled && !tokens) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return children;
}

/**
 * Shown instead of the console when the deployment is misconfigured. Failing
 * here is deliberate: booting would hide the login screen and 401 on every
 * request, which reads as "not authorised" rather than a broken publish.
 */
function ConfigErrorScreen({ message }: { message: string }) {
  return (
    <div className="login-shell">
      <div className="login-card">
        <div className="login-header">
          <div className="brand login-brand">
            <span className="brand-mark" aria-hidden />
            Trade Reconciliation
          </div>
          <p className="login-kicker">Analyst console</p>
          <h1 className="login-title">Configuration error</h1>
        </div>
        <div className="banner error">{message}</div>
        <p className="login-kicker">
          Redeploy with <code>cdk deploy TradeReconFrontend</code>. Publishing by
          copying <code>frontend/dist</code> to S3 skips the generated{" "}
          <code>config.json</code>.
        </p>
      </div>
    </div>
  );
}

function AppRoutes() {
  const { ready, configError } = useAuth();
  if (ready && configError) {
    return <ConfigErrorScreen message={configError} />;
  }
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route path="/" element={<Dashboard />} />
          <Route path="/breaks" element={<Breaks />} />
          <Route path="/breaks/:id" element={<BreakDetail />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

export function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </ThemeProvider>
  );
}
