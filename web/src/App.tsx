import { FormEvent, useEffect, useState } from "react";
import { api, AnalysisReport, ApiError } from "./api";
import { AlertCenter } from "./components/AlertCenter";
import { Login } from "./components/Login";
import { ReportView } from "./components/ReportView";
import { UpcomingEvents } from "./components/UpcomingEvents";

type AuthState = "checking" | "in" | "out";

export default function App() {
  const [auth, setAuth] = useState<AuthState>("checking");
  const [symbol, setSymbol] = useState("");
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .authStatus()
      .then((r) => setAuth(r.authenticated ? "in" : "out"))
      .catch(() => setAuth("out"));
  }, []);

  useEffect(() => {
    if (auth === "in") refreshWatchlist();
  }, [auth]);

  async function refreshWatchlist() {
    try {
      const r = await api.instruments();
      setWatchlist(r.instruments);
    } catch {
      /* non-fatal */
    }
  }

  async function runReport(sym: string) {
    const clean = sym.trim().toUpperCase();
    if (!clean) return;
    setLoading(true);
    setError(null);
    try {
      const r = await api.report(clean);
      setReport(r);
      setSymbol(clean);
      refreshWatchlist();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not reach the server");
      setReport(null);
    } finally {
      setLoading(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    runReport(symbol);
  }

  async function openFromAlert(sym: string) {
    await runReport(sym);
    document.getElementById("report")?.scrollIntoView({ behavior: "smooth" });
  }

  async function logout() {
    await api.logout().catch(() => {});
    setAuth("out");
    setReport(null);
  }

  if (auth === "checking") {
    return (
      <div className="app-shell">
        <div className="state-msg">
          <span className="spinner" />
        </div>
      </div>
    );
  }

  if (auth === "out") {
    return (
      <div className="app-shell">
        <Login onSuccess={() => setAuth("in")} />
      </div>
    );
  }

  return (
    <div className="app-shell">
      <div className="topbar">
        <div className="brand">
          <span className="brand-mark">TS</span>
          TradeSafe
        </div>
        <button className="logout-btn" onClick={logout}>
          Log out
        </button>
      </div>

      <div className="disclaimer">
        This tool reports; it never advises. No position sizes, orders, price targets or directional
        recommendations are ever produced. Most of the time, the honest answer is that nothing qualifies.
        Nothing here is financial advice — verify everything yourself before acting on anything.
      </div>

      <form className="search-row" onSubmit={onSubmit}>
        <input
          className="search-input"
          placeholder="Instrument, e.g. BTC"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
        />
        <button className="btn btn-primary" disabled={loading || !symbol.trim()}>
          {loading ? <span className="spinner" /> : "Analyse"}
        </button>
      </form>

      {watchlist.length > 0 && (
        <div className="chip-row">
          {watchlist.map((sym) => (
            <button
              key={sym}
              className={`chip ${sym === report?.instrument ? "active" : ""}`}
              onClick={() => runReport(sym)}
            >
              {sym}
            </button>
          ))}
        </div>
      )}

      <UpcomingEvents />

      <AlertCenter onOpen={openFromAlert} />

      {error && <div className="error-box">{error}</div>}

      {!report && !error && !loading && (
        <div className="state-msg">Name an instrument above to run it through Gate U and Layer 0.</div>
      )}

      <div id="report">{report && <ReportView report={report} />}</div>
    </div>
  );
}
