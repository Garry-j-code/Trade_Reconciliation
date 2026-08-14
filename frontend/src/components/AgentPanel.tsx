import type { BreakDetailResponse } from "../api/types";
import { confidencePct, labelize } from "../lib/format";

function confidenceLevel(value: number | null | undefined): "low" | "mid" | "high" {
  if (value == null || Number.isNaN(value)) return "mid";
  if (value < 0.5) return "low";
  if (value >= 0.8) return "high";
  return "mid";
}

export function AgentPanel({ detail }: { detail: BreakDetailResponse }) {
  const suggestion = detail.suggestion ?? null;
  const evidence = suggestion?.evidence ?? [];
  const hasSuggestion = Boolean(
    suggestion?.root_cause || suggestion?.explanation || suggestion?.suggested_action,
  );
  const confidence = suggestion?.confidence ?? null;
  const level = confidenceLevel(confidence);
  const widthPct =
    confidence == null || Number.isNaN(confidence)
      ? 0
      : Math.max(0, Math.min(100, confidence * 100));

  return (
    <div className="panel">
      <h2>Agent suggestion</h2>
      <div className="meta-row" style={{ marginBottom: 14, padding: 0, border: "none", boxShadow: "none", background: "transparent" }}>
        <div className="meta-item">
          <div className="k">Routing</div>
          <div className="v">
            <span className="pill">{labelize(detail.review_routing)}</span>
            {suggestion?.inferred ? <span className="pill inferred">inferred</span> : null}
          </div>
        </div>
      </div>

      <div className="confidence-block">
        <div className="confidence-header">
          <span className="k">Confidence</span>
          <span className="v">{confidencePct(confidence)}</span>
        </div>
        <div className="confidence-track" aria-hidden>
          <div
            className={`confidence-fill${level === "mid" ? "" : ` ${level}`}`}
            style={{ width: `${widthPct}%` }}
          />
        </div>
      </div>

      {!hasSuggestion ? (
        <p className="placeholder">
          No investigation yet. Bedrock is optional — use Investigate to request a
          suggestion, or approve/reject with a note without one.
        </p>
      ) : (
        <>
          <div className="agent-field">
            <div className="k">Root cause</div>
            <div className="v">{labelize(suggestion?.root_cause)}</div>
          </div>
          <div className="agent-field">
            <div className="k">Suggested action</div>
            <div className="v">{labelize(suggestion?.suggested_action)}</div>
          </div>
          <div className="agent-field">
            <div className="k">Explanation</div>
            <div className="v agent-explanation">{suggestion?.explanation ?? "—"}</div>
          </div>
          <div className="agent-field">
            <div className="k">Evidence</div>
            {evidence.length === 0 ? (
              <p className="placeholder">No tool evidence attached.</p>
            ) : (
              <ul className="evidence">
                {evidence.map((item, i) => (
                  <li key={i}>
                    <div className="tool-name">{String(item.tool ?? "tool")}</div>
                    <div className="tool-result">
                      {String(item.result_summary ?? JSON.stringify(item))}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  );
}
