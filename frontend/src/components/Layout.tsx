import { NavLink, Outlet } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { ApiError, getInvestigateStatus, runRecon } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import type { ReconRunResponse } from "../api/types";
import { useTheme } from "../ThemeContext";
import type { ThemePreference } from "../theme";
import { ChangePasswordDialog } from "./ChangePasswordDialog";

const INVESTIGATE_RUNNING_COPY =
  "Agent investigation is running in the background — refresh Breaks in a minute for new suggestions.";
const INVESTIGATE_FINISHED_COPY =
  "Agent investigation is finished — refresh the page for new suggestions.";
const BANNER_STORAGE_KEY = "trade-recon.reconBanners";
const INVESTIGATE_POLL_MS = 2000;

type InvestigatePhase = "running" | "finished";

interface ReconBannerState {
  rematchMsg: string | null;
  investigatePhase: InvestigatePhase | null;
  jobId: string | null;
}

function readBannerState(): ReconBannerState | null {
  try {
    const raw = sessionStorage.getItem(BANNER_STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const row = parsed as Partial<ReconBannerState>;
    const phase = row.investigatePhase;
    if (phase != null && phase !== "running" && phase !== "finished") return null;
    return {
      rematchMsg: typeof row.rematchMsg === "string" ? row.rematchMsg : null,
      investigatePhase: phase ?? null,
      jobId: typeof row.jobId === "string" ? row.jobId : null,
    };
  } catch {
    return null;
  }
}

function writeBannerState(state: ReconBannerState): void {
  sessionStorage.setItem(BANNER_STORAGE_KEY, JSON.stringify(state));
}

function clearBannerState(): void {
  sessionStorage.removeItem(BANNER_STORAGE_KEY);
}

function isFullPageReload(): boolean {
  const nav = performance.getEntriesByType("navigation")[0] as
    | PerformanceNavigationTiming
    | undefined;
  return nav?.type === "reload";
}

function rematchStatsMessage(result: ReconRunResponse): string {
  return `Rematch finished in ${result.elapsed_seconds.toFixed(1)}s — ${result.match_count} matches, ${result.break_count} breaks, ${result.normalized_rows} trades rematched.`;
}

const APPEARANCE_OPTIONS: { value: ThemePreference; label: string }[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

export function Layout() {
  const { logout, config, displayName, changePassword } = useAuth();
  const { preference, setPreference } = useTheme();
  const [menuOpen, setMenuOpen] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [reconMsg, setReconMsg] = useState<string | null>(null);
  const [reconErr, setReconErr] = useState<string | null>(null);
  const [investigatePhase, setInvestigatePhase] = useState<InvestigatePhase | null>(
    null,
  );
  const [investigateJobId, setInvestigateJobId] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const authDisabled = Boolean(config?.authDisabled);
  const identity = authDisabled ? "Local analyst" : displayName;

  useEffect(() => {
    if (isFullPageReload()) {
      clearBannerState();
      return;
    }
    const saved = readBannerState();
    if (!saved) return;
    setReconMsg(saved.rematchMsg);
    setInvestigatePhase(saved.investigatePhase);
    setInvestigateJobId(saved.jobId);
  }, []);

  useEffect(() => {
    if (!reconMsg && !investigatePhase) {
      return;
    }
    writeBannerState({
      rematchMsg: reconMsg,
      investigatePhase,
      jobId: investigateJobId,
    });
  }, [reconMsg, investigatePhase, investigateJobId]);

  useEffect(() => {
    if (investigatePhase !== "running") return;
    let cancelled = false;

    async function poll(): Promise<void> {
      try {
        const status = await getInvestigateStatus(investigateJobId);
        if (cancelled) return;
        if (status.status === "finished" || status.status === "idle") {
          setInvestigatePhase("finished");
        }
      } catch {
        // Keep the running banner; the next tick retries.
      }
    }

    void poll();
    const timer = window.setInterval(() => {
      void poll();
    }, INVESTIGATE_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [investigatePhase, investigateJobId]);

  useEffect(() => {
    function onDocClick(event: MouseEvent): void {
      if (!menuRef.current?.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  async function onRunRecon() {
    setRunning(true);
    setReconMsg(null);
    setReconErr(null);
    setInvestigatePhase(null);
    setInvestigateJobId(null);
    clearBannerState();
    try {
      const result: ReconRunResponse = await runRecon({ mode: "rematch" });
      const rematchMsg = rematchStatsMessage(result);
      setReconMsg(rematchMsg);
      if (result.investigate_status === "queued") {
        setInvestigatePhase("running");
        setInvestigateJobId(result.investigate_job_id ?? null);
      } else if (result.investigate_status === "finished") {
        setInvestigatePhase("finished");
        setInvestigateJobId(result.investigate_job_id ?? null);
      } else if (result.investigate_attempted != null) {
        setInvestigatePhase("finished");
        setInvestigateJobId(result.investigate_job_id ?? null);
      } else {
        setInvestigatePhase(null);
        setInvestigateJobId(null);
      }
      window.dispatchEvent(new Event("recon:complete"));
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Recon run failed";
      setReconErr(
        detail.includes("Missing broker trades")
          ? "Could not rematch the current book. Confirm the API can reach the trade database, then try again."
          : detail,
      );
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="layout">
      <header className="topbar">
        <NavLink to="/" className="brand">
          <span className="brand-mark" aria-hidden />
          <span className="brand-full">Trade Reconciliation</span>
          <span className="brand-short">Trade Recon</span>
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
          <div className="account-menu" ref={menuRef}>
            <button
              className="account-trigger"
              type="button"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((open) => !open)}
            >
              <span className="analyst-chip">{identity}</span>
              <span className="account-caret" aria-hidden>
                ▾
              </span>
            </button>
            {menuOpen && (
              <div className="account-dropdown" role="menu">
                <div className="account-theme" role="group" aria-label="Appearance">
                  <p className="account-theme-label">Appearance</p>
                  {APPEARANCE_OPTIONS.map((option) => (
                    <button
                      key={option.value}
                      className={`account-item${preference === option.value ? " is-active" : ""}`}
                      type="button"
                      role="menuitemradio"
                      aria-checked={preference === option.value}
                      onClick={() => setPreference(option.value)}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                {authDisabled ? (
                  <p className="muted account-note">
                    Change password is unavailable in local auth-disabled mode.
                  </p>
                ) : (
                  <button
                    className="account-item"
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setMenuOpen(false);
                      setPasswordOpen(true);
                    }}
                  >
                    Change password
                  </button>
                )}
                {config && !config.authDisabled && (
                  <button
                    className="account-item"
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setMenuOpen(false);
                      logout();
                    }}
                  >
                    Sign out
                  </button>
                )}
              </div>
            )}
          </div>
          <button className="btn btn-primary" onClick={onRunRecon} disabled={running}>
            {running ? (
              "Running…"
            ) : (
              <>
                <span className="btn-label-full">Run reconciliation</span>
                <span className="btn-label-short">Run recon</span>
              </>
            )}
          </button>
        </div>
      </header>
      <main className="main">
        {reconErr && <div className="banner error">{reconErr}</div>}
        {reconMsg && <div className="banner ok">{reconMsg}</div>}
        {investigatePhase === "running" && (
          <div className="banner info">{INVESTIGATE_RUNNING_COPY}</div>
        )}
        {investigatePhase === "finished" && (
          <div className="banner ok">{INVESTIGATE_FINISHED_COPY}</div>
        )}
        <Outlet />
      </main>
      {!authDisabled && (
        <ChangePasswordDialog
          open={passwordOpen}
          onClose={() => setPasswordOpen(false)}
          onSubmit={changePassword}
        />
      )}
    </div>
  );
}
