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

export function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
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
      </AuthProvider>
    </ThemeProvider>
  );
}
