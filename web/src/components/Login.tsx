import { FormEvent, useState } from "react";
import { api, ApiError } from "../api";

export function Login({ onSuccess }: { onSuccess: () => void }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(password);
      onSuccess();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not reach the server");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-screen">
      <div className="brand" style={{ justifyContent: "center" }}>
        <span className="brand-mark">TS</span>
        <span style={{ fontSize: 22 }}>TradeSafe</span>
      </div>
      <div className="login-title">Evidence, not advice</div>
      <div className="login-subtitle">
        Name an instrument. Get what the data supports, what it does not, and why — never a size, an order, or a
        recommendation.
      </div>
      <form onSubmit={submit}>
        {error && <div className="error-box">{error}</div>}
        <input
          className="search-input"
          style={{ width: "100%", marginBottom: 12, textTransform: "none" }}
          type="password"
          placeholder="Access password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoFocus
        />
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={busy || !password}>
          {busy ? <span className="spinner" /> : "Unlock"}
        </button>
      </form>
    </div>
  );
}
