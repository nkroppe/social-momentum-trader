import { FormEvent, useState } from "react";
import { AuthError, fetchHealth } from "../api";
import { setToken } from "../auth";

export function TokenGate({ onAuthed }: { onAuthed: () => void }) {
  const [token, setLocal] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setToken(token.trim());
    try {
      await fetchHealth();
      onAuthed();
    } catch (err) {
      setToken("");
      setError(err instanceof AuthError ? "Token rejected." : "Could not reach the dashboard API.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gate">
      <form className="gate-card" onSubmit={submit}>
        <h1>SMT MONITOR</h1>
        <p>Enter the dashboard token from your environment. The API is read-only.</p>
        <input
          type="password"
          autoComplete="off"
          placeholder="DASHBOARD_TOKEN"
          value={token}
          onChange={(e) => setLocal(e.target.value)}
        />
        {error ? <div className="err">{error}</div> : null}
        <button type="submit" disabled={busy || !token.trim()}>
          {busy ? "Checking…" : "Open dashboard"}
        </button>
      </form>
    </div>
  );
}
