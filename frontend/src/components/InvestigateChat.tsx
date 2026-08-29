import { useEffect, useRef, useState } from "react";
import { ApiError, getBreakInvestigateJob, startBreakInvestigate } from "../api/client";

const POLL_MS = 2000;
const STORAGE_PREFIX = "trade-recon.investigateChat.";

const SYSTEM_COPY =
  "Break details (trades, existing suggestion, and evidence) are attached automatically. Add a note if you want, or send with an empty box to investigate with that context only.";

type ChatRole = "system" | "analyst" | "agent" | "error";

interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
}

interface StoredThread {
  messages: ChatMessage[];
  jobId: string | null;
}

function storageKey(breakId: string): string {
  return `${STORAGE_PREFIX}${breakId}`;
}

function readThread(breakId: string): StoredThread | null {
  try {
    const raw = sessionStorage.getItem(storageKey(breakId));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const row = parsed as Partial<StoredThread>;
    if (!Array.isArray(row.messages)) return null;
    return {
      messages: row.messages as ChatMessage[],
      jobId: typeof row.jobId === "string" ? row.jobId : null,
    };
  } catch {
    return null;
  }
}

function writeThread(breakId: string, thread: StoredThread): void {
  sessionStorage.setItem(storageKey(breakId), JSON.stringify(thread));
}

function newId(): string {
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function shortError(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return "Investigate failed. Try again in a moment.";
}

export function InvestigateChat({
  breakId,
  open,
  onClose,
  onSuggestionReady,
}: {
  breakId: string;
  open: boolean;
  onClose: () => void;
  onSuggestionReady: () => Promise<void> | void;
}) {
  const saved = readThread(breakId);
  const [messages, setMessages] = useState<ChatMessage[]>(
    saved?.messages?.length
      ? saved.messages
      : [{ id: "system", role: "system", text: SYSTEM_COPY }],
  );
  const [jobId, setJobId] = useState<string | null>(saved?.jobId ?? null);
  const [busy, setBusy] = useState(Boolean(saved?.jobId));
  const [draft, setDraft] = useState("");
  const listRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const onReadyRef = useRef(onSuggestionReady);
  onReadyRef.current = onSuggestionReady;

  useEffect(() => {
    const stored = readThread(breakId);
    if (stored?.messages?.length) {
      setMessages(stored.messages);
      setJobId(stored.jobId);
      setBusy(Boolean(stored.jobId));
    } else {
      setMessages([{ id: "system", role: "system", text: SYSTEM_COPY }]);
      setJobId(null);
      setBusy(false);
    }
    setDraft("");
  }, [breakId]);

  useEffect(() => {
    writeThread(breakId, { messages, jobId });
  }, [breakId, messages, jobId]);

  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
    }
  }, [open]);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, busy, open]);

  useEffect(() => {
    if (!open || !jobId || !busy) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const job = await getBreakInvestigateJob(breakId, jobId);
        if (cancelled) return;
        if (job.status === "finished") {
          setBusy(false);
          setJobId(null);
          const reply = (job.reply || job.suggestion?.explanation || "").trim();
          setMessages((prev) => [
            ...prev.filter((m) => m.id !== "thinking"),
            {
              id: newId(),
              role: "agent",
              text: reply || "Investigation finished. The suggestion panel is updated.",
            },
          ]);
          await onReadyRef.current();
        } else if (job.status === "error") {
          setBusy(false);
          setJobId(null);
          setMessages((prev) => [
            ...prev.filter((m) => m.id !== "thinking"),
            {
              id: newId(),
              role: "error",
              text: job.error || "Investigation failed. Try again in a moment.",
            },
          ]);
        }
      } catch (err) {
        if (cancelled) return;
        setBusy(false);
        setJobId(null);
        setMessages((prev) => [
          ...prev.filter((m) => m.id !== "thinking"),
          { id: newId(), role: "error", text: shortError(err) },
        ]);
      }
    };
    void tick();
    const id = window.setInterval(() => {
      void tick();
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [open, jobId, busy, breakId]);

  async function onSend() {
    if (busy) return;
    const note = draft.trim();
    setDraft("");
    setMessages((prev) => [
      ...prev,
      {
        id: newId(),
        role: "analyst",
        text: note || "(Investigate with break context only)",
      },
      { id: "thinking", role: "agent", text: "Investigating…" },
    ]);
    setBusy(true);
    try {
      const accepted = await startBreakInvestigate(breakId, note);
      setJobId(accepted.job_id);
      if (accepted.status === "finished") {
        const job = await getBreakInvestigateJob(breakId, accepted.job_id);
        setBusy(false);
        setJobId(null);
        const reply = (job.reply || job.suggestion?.explanation || "").trim();
        setMessages((prev) => [
          ...prev.filter((m) => m.id !== "thinking"),
          {
            id: newId(),
            role: "agent",
            text: reply || "Investigation finished. The suggestion panel is updated.",
          },
        ]);
        await onReadyRef.current();
      } else if (accepted.status === "error") {
        const job = await getBreakInvestigateJob(breakId, accepted.job_id);
        setBusy(false);
        setJobId(null);
        setMessages((prev) => [
          ...prev.filter((m) => m.id !== "thinking"),
          {
            id: newId(),
            role: "error",
            text: job.error || "Investigation failed. Try again in a moment.",
          },
        ]);
      }
    } catch (err) {
      setBusy(false);
      setJobId(null);
      setMessages((prev) => [
        ...prev.filter((m) => m.id !== "thinking"),
        { id: newId(), role: "error", text: shortError(err) },
      ]);
    }
  }

  if (!open) return null;

  return (
    <aside className="investigate-chat" aria-label="Investigate">
      <header className="investigate-chat-header">
        <h2>Investigate</h2>
        <button type="button" className="btn btn-ghost investigate-chat-close" onClick={onClose}>
          Close
        </button>
      </header>
      <div className="investigate-chat-body" ref={listRef}>
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={`investigate-bubble investigate-bubble-${msg.role}${
              msg.id === "thinking" ? " is-thinking" : ""
            }`}
          >
            {msg.text}
          </div>
        ))}
      </div>
      <form
        className="investigate-chat-input"
        onSubmit={(e) => {
          e.preventDefault();
          void onSend();
        }}
      >
        <label className="sr-only" htmlFor="investigate-note">
          Analyst note
        </label>
        <textarea
          id="investigate-note"
          ref={inputRef}
          rows={2}
          value={draft}
          disabled={busy}
          placeholder="Add a note, or send empty to investigate this break…"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void onSend();
            }
          }}
        />
        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? "Working…" : "Send"}
        </button>
      </form>
    </aside>
  );
}
