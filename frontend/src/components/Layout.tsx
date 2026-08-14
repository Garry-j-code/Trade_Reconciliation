import { NavLink, Outlet } from "react-router-dom";
import { useEffect, useState } from "react";
import { ApiError, getHealth, runRecon } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import type { HealthResponse, ReconRunResponse } from "../api/types";

export function Layout() {
  const { tokens, logout, config } = useAuth();
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [reconMsg, setReconMsg] = useState<string | null>(null);
  const [reconErr, setReconErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getHealth()
      .then((h) => {
        if (!cancelled) {
          setHealth(h);
          setHealthError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setHealth(null);
          setHealthError(err instanceof ApiError ? err.detail : "API unreachable");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onRunRecon() {
    setRunning(true);
    setReconMsg(null);
    setReconErr(null);
    try {
      const result: ReconRunResponse = await runRecon({ replace: true });
      setReconMsg(
        `Recon finished in ${result.elapsed_seconds.toFixed(1)}s — ${result.match_count} matches, ${result.break_count} breaks, ${result.normalized_rows} normalized trades${result.db_loaded ? " (loaded to RDS)" : " (DB not loaded)"}.`,
      );
    } catch (err) {
      setReconErr(err instanceof ApiError ? err.detail : "Recon run failed");
    } finally {
      setRunning(false);
    }
  }

  const dbOk = health?.db === "connected";

  return (
    <div className="layout">
      <header className="topbar">
        <NavLink to="/" className="brand">
          <span className="brand-mark" aria-hidden />
          Trade Reconciliation
        </NavLink>
        <nav className="nav">
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            Dashboard
          </NavLink>
          <NavLink to="/breaks" className={({ isActive }) => (isActive ? "active" : "")}>
            Breaks
          </NavLink>
        </nav>
        <div className="topbar-actions">
          <span className="health" title={healthError ?? health?.db ?? ""}>
            <span className={`dot ${dbOk ? "ok" : "bad"}`} />
            <span>{healthError ? "API down" : dbOk ? "RDS connected" : "API / DB"}</span>
          </span>
          {tokens?.email && <span className="analyst-chip">{tokens.email}</span>}
          <button className="btn btn-primary" onClick={onRunRecon} disabled={running}>
            {running ? "Running…" : "Run reconciliation"}
          </button>
          {config && !config.authDisabled && (
            <button className="btn" type="button" onClick={logout}>
              Sign out
            </button>
          )}
        </div>
      </header>
      <main className="main">
        {healthError && (
          <div className="banner error">
            API unreachable ({healthError}). If you are local, start{" "}
            <span className="mono">uv run serve-api</span>. Hosted: confirm CloudFront{" "}
            <span className="mono">/health</span> and that you are signed in.
          </div>
        )}
        {reconErr && <div className="banner error">{reconErr}</div>}
        {reconMsg && <div className="banner ok">{reconMsg}</div>}
        <Outlet />
      </main>
    </div>
  );
}
