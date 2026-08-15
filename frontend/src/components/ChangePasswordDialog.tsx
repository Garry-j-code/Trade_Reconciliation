import { useState, type FormEvent } from "react";
import { passwordPolicyHint, validateNewPassword } from "../auth/cognito";

type Props = {
  open: boolean;
  onClose: () => void;
  onSubmit: (previousPassword: string, proposedPassword: string) => Promise<void>;
};

export function ChangePasswordDialog({ open, onClose, onSubmit }: Props) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [busy, setBusy] = useState(false);

  if (!open) return null;

  function reset(): void {
    setCurrentPassword("");
    setNewPassword("");
    setConfirmPassword("");
    setError(null);
    setOk(false);
    setBusy(false);
  }

  function close(): void {
    reset();
    onClose();
  }

  async function handleSubmit(event: FormEvent): Promise<void> {
    event.preventDefault();
    setError(null);
    setOk(false);
    const policyError = validateNewPassword(newPassword);
    if (policyError) {
      setError(policyError);
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("New password and confirmation do not match.");
      return;
    }
    if (newPassword === currentPassword) {
      setError("Choose a new password that is different from the current one.");
      return;
    }
    setBusy(true);
    try {
      await onSubmit(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setOk(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not change password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={close}>
      <form
        className="modal-card"
        role="dialog"
        aria-labelledby="change-password-title"
        onClick={(e) => e.stopPropagation()}
        onSubmit={handleSubmit}
      >
        <h2 id="change-password-title">Change password</h2>
        <p className="muted">{passwordPolicyHint()}</p>
        {error && <div className="banner error">{error}</div>}
        {ok && <div className="banner ok">Password updated.</div>}
        <label className="field">
          <span>Current password</span>
          <input
            type="password"
            autoComplete="current-password"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            required
          />
        </label>
        <label className="field">
          <span>New password</span>
          <input
            type="password"
            autoComplete="new-password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            required
            minLength={12}
          />
        </label>
        <label className="field">
          <span>Confirm new password</span>
          <input
            type="password"
            autoComplete="new-password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            required
            minLength={12}
          />
        </label>
        <div className="modal-actions">
          <button className="btn" type="button" onClick={close}>
            Close
          </button>
          <button className="btn btn-primary" type="submit" disabled={busy}>
            {busy ? "Updating…" : "Update password"}
          </button>
        </div>
      </form>
    </div>
  );
}
