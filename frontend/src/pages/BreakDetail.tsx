import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApiError,
  approveBreak,
  getBreak,
  investigateBreak,
  rejectBreak,
} from "../api/client";
import { TERMINAL_STATUSES, type BreakDetailResponse } from "../api/types";
import { AgentPanel } from "../components/AgentPanel";
import { TradeDiff } from "../components/TradeDiff";
import { formatDateTime, formatTradeTimestamp, formatUsd, labelize, shortId } from "../lib/format";

export function BreakDetail() {
  const { id } = useParams<{ id: string }>();
  const [detail, setDetail] = useState<BreakDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | "investigate" | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  async function load() {
    if (!id) return;
    const row = await getBreak(id);
    setDetail(row);
    setError(null);
  }

  useEffect(() => {
    let cancelled = false;
    if (!id) return;
    getBreak(id)
      .then((row) => {
        if (!cancelled) {
          setDetail(row);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.detail : "Failed to load break");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (error && !detail) {
    return (
      <div className="page-enter">
        <Link to="/breaks" className="back-link">
          ← Breaks
        </Link>
        <div className="banner error" style={{ marginTop: 12 }}>
          {error}
        </div>
      </div>
    );
  }
  if (!detail || !id) {
    return <p className="loading-state">Loading break…</p>;
  }

  const breakId = id;
  const locked = TERMINAL_STATUSES.has(detail.status);

  async function onApprove() {
    setBusy("approve");
    setFlash(null);
    setError(null);
    try {
      const res = await approveBreak(breakId, {
        note: note.trim() || null,
      });
      setFlash(
        `Applied suggested fix. Status ${res.status}. Audit ${shortId(res.audit_id)}.`,
      );
      setNote("");
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Approve failed");
    } finally {
      setBusy(null);
    }
  }

  async function onReject() {
    const trimmed = note.trim();
    if (!trimmed) {
      setError("A note is required to reject.");
      return;
    }
    setBusy("reject");
    setFlash(null);
    setError(null);
    try {
      const res = await rejectBreak(breakId, {
        note: trimmed,
      });
      setFlash(`Rejected. Status ${res.status}. Audit ${shortId(res.audit_id)}.`);
      setNote("");
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Reject failed");
    } finally {
      setBusy(null);
    }
  }

  async function onInvestigate() {
    setBusy("investigate");
    setFlash(null);
    setError(null);
    try {
      await investigateBreak(breakId);
      setFlash("Investigation finished. Suggestion panel updated.");
      await load();
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : "Investigate failed";
      setError(
        `Investigate failed${err instanceof ApiError && err.status ? ` (HTTP ${err.status})` : ""}: ${msg}. Bedrock may be unavailable — the rest of the console still works.`,
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="page-enter">
      <div className="page-header">
        <div>
          <Link to="/breaks" className="back-link">
            ← Breaks
          </Link>
          <h1>
            {labelize(detail.break_type)} · {detail.symbol ?? "no symbol"}
          </h1>
          <p className="mono break-id">{detail.break_id}</p>
        </div>
        <span className={`pill ${detail.status}`}>{detail.status}</span>
      </div>

      {error && <div className="banner error">{error}</div>}
      {flash && <div className="banner ok">{flash}</div>}

      <div className="meta-row">
        <div className="meta-item">
          <div className="k">Desk</div>
          <div className="v">{detail.desk ?? "—"}</div>
        </div>
        <div className="meta-item">
          <div className="k">Trade date</div>
          <div className="v">{formatTradeTimestamp(detail.executed_at, detail.trade_date)}</div>
        </div>
        <div className="meta-item">
          <div className="k">Notional at risk</div>
          <div className="v">{formatUsd(detail.notional_at_risk)}</div>
        </div>
        <div className="meta-item">
          <div className="k">Pair</div>
          <div className="v mono">{detail.pair_id ?? "—"}</div>
        </div>
      </div>

      <div className="detail-grid">
        <TradeDiff detail={detail} />
        <div>
          <AgentPanel detail={detail} />
          <div className="panel">
            <h2>Human decision</h2>
            <p className="muted">
              Approve applies the suggested fix to the books (for example copies the
              broker price onto the desk, or voids the extra duplicate), then marks
              the break resolved and writes an audit log. Reject records your note
              and does not change trades.
            </p>
            <div className="field grow" style={{ marginTop: 10 }}>
              <label htmlFor="note">Note</label>
              <textarea
                id="note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Required for reject. Optional for approve."
                disabled={locked}
              />
            </div>
            <div className="actions-row">
              <button
                className="btn btn-ok"
                disabled={locked || busy !== null}
                onClick={onApprove}
              >
                {busy === "approve" ? "Applying…" : "Apply suggested fix and resolve"}
              </button>
              <button
                className="btn btn-danger"
                disabled={locked || busy !== null}
                onClick={onReject}
              >
                {busy === "reject" ? "Rejecting…" : "Reject (do not change books)"}
              </button>
              <button
                className="btn"
                disabled={busy !== null}
                onClick={onInvestigate}
              >
                {busy === "investigate" ? "Investigating…" : "Investigate"}
              </button>
            </div>
            {locked && (
              <p className="placeholder">This break is {detail.status} and cannot be decided again.</p>
            )}
          </div>
          {(detail.decisions ?? []).length > 0 ? (
            <div className="panel">
              <h2>Decision history</h2>
              <ul className="evidence">
                {(detail.decisions ?? []).map((d) => (
                  <li key={d.audit_id}>
                    <div className="tool-name">
                      {labelize(d.action)} · {d.actor} · {formatDateTime(d.created_at)}
                    </div>
                    <div className="tool-result">
                      {d.override_note ? `Note: ${d.override_note}` : "No comment."}
                      {d.suggested_action
                        ? ` · Suggestion: ${labelize(d.root_cause)} → ${labelize(d.suggested_action)}`
                        : ""}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
