import { useEffect, useRef, useState } from "react";
import { AlertsResponse, api, ApiError, SetupAlert } from "../api";

// Server-side scans run every 15 minutes; checking twice a minute keeps delivery prompt.
// Browsers slow timers in background tabs to about once a minute, which is still fine.
const POLL_MS = 30_000;
const BASE_TITLE = document.title;

const CASE_WORD: Record<string, string> = {
  long: "Long-type setup",
  short: "Short-type setup",
  unclear: "Direction unclear",
};
const BIAS_WORD: Record<string, string> = { long: "Long", short: "Short", unclear: "No clear direction" };

function describe(a: SetupAlert) {
  if (a.is_test) {
    return { title: "TradeSafe test alert", body: "Alerts are working. A qualifying setup will arrive like this." };
  }
  const parts = [
    a.setup_case ? CASE_WORD[a.setup_case] : null,
    a.verdict_bias ? `Report direction: ${BIAS_WORD[a.verdict_bias]}` : null,
  ].filter(Boolean);
  return { title: `${a.instrument}: ${a.setup.replace(/_/g, " ")} qualified`, body: parts.join(" · ") };
}

function ago(iso: string) {
  const minutes = Math.floor((Date.now() - Date.parse(iso)) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${minutes % 60} min ago`;
}

function clock(iso: string) {
  return new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

// A two-note chime generated in the page, so there's no sound file to ship. Browsers only
// allow audio after someone has clicked on the page, so it's unlocked on the first click.
let audio: AudioContext | null = null;

function unlockSound() {
  try {
    audio ??= new AudioContext();
    void audio.resume();
  } catch {
    /* no Web Audio: alerts still show, just silently */
  }
}

function chime() {
  if (!audio || audio.state !== "running") return;
  const ctx = audio;
  const t = ctx.currentTime;
  [880, 1320].forEach((freq, i) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    const start = t + i * 0.18;
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.25, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.16);
    osc.connect(gain).connect(ctx.destination);
    osc.start(start);
    osc.stop(start + 0.17);
  });
}

type Permission = NotificationPermission | "unsupported";

function currentPermission(): Permission {
  return "Notification" in window ? Notification.permission : "unsupported";
}

export function AlertCenter({ onOpen }: { onOpen: (symbol: string) => void }) {
  const [recent, setRecent] = useState<SetupAlert[]>([]);
  const [toasts, setToasts] = useState<SetupAlert[]>([]);
  const [scanner, setScanner] = useState<AlertsResponse["scanner"] | null>(null);
  const [permission, setPermission] = useState<Permission>(currentPermission);
  const [problem, setProblem] = useState<string | null>(null);
  const lastSeen = useRef<number | null>(null);
  const polling = useRef(false);
  const unread = useRef(0);
  const openRef = useRef(onOpen);
  openRef.current = onOpen;

  function notify(a: SetupAlert) {
    if (currentPermission() !== "granted") return;
    const { title, body } = describe(a);
    try {
      const n = new Notification(title, { body, tag: `tradesafe-alert-${a.id}`, requireInteraction: !a.is_test });
      n.onclick = () => {
        window.focus();
        if (!a.is_test) openRef.current(a.instrument);
        n.close();
      };
    } catch {
      /* Android Chrome only allows notifications from a service worker; the banner still shows */
    }
  }

  function announce(alerts: SetupAlert[]) {
    chime();
    alerts.forEach(notify);
    setToasts((t) => [...alerts, ...t]);
    setRecent((r) => [...alerts, ...r].slice(0, 5));
    if (document.hidden) {
      unread.current += alerts.length;
      document.title = `(${unread.current}) ${BASE_TITLE}`;
    }
  }

  async function poll() {
    if (polling.current) return;
    polling.current = true;
    try {
      const r = await api.alerts(lastSeen.current ?? 0);
      setScanner(r.scanner);
      setProblem(null);
      if (lastSeen.current === null) setRecent(r.alerts.slice(0, 5));  // history on load, no fanfare
      else if (r.alerts.length) announce(r.alerts);
      lastSeen.current = r.latest_id;
    } catch (err) {
      setProblem(
        err instanceof ApiError && err.status === 401
          ? "You've been signed out, so alerts are paused. Log in again to resume them."
          : "Can't reach the server right now. Retrying.",
      );
    } finally {
      polling.current = false;
    }
  }

  useEffect(() => {
    poll();
    const timer = window.setInterval(poll, POLL_MS);
    const onVisible = () => {
      if (document.hidden) return;
      unread.current = 0;
      document.title = BASE_TITLE;
      poll();
    };
    document.addEventListener("visibilitychange", onVisible);
    document.addEventListener("pointerdown", unlockSound, { once: true });
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      document.removeEventListener("pointerdown", unlockSound);
    };
  }, []);

  async function enable() {
    unlockSound();
    if (!("Notification" in window)) return;
    setPermission(await Notification.requestPermission());
  }

  async function sendTest() {
    unlockSound();
    try {
      await api.testAlert();
      await poll();
    } catch {
      setProblem("Couldn't send a test alert. Is the server reachable?");
    }
  }

  function dismiss(id: number) {
    setToasts((t) => t.filter((a) => a.id !== id));
  }

  const lastRun = scanner?.last_run;
  const overdue = lastRun && scanner && Date.now() - Date.parse(lastRun.finished_at) > 2 * scanner.interval_minutes * 60_000;
  const state =
    permission === "granted"
      ? { className: "on", label: "Notifications on" }
      : permission === "denied"
        ? { className: "off", label: "Notifications blocked" }
        : permission === "unsupported"
          ? { className: "off", label: "On-page alerts only" }
          : { className: "off", label: "Notifications off" };

  return (
    <>
      {toasts.length > 0 && (
        <div className="toast-stack" role="status" aria-live="polite">
          {toasts.map((a) => {
            const d = describe(a);
            return (
              <div className={`toast ${a.is_test ? "test" : a.setup_case ?? ""}`} key={a.id}>
                <div className="toast-body">
                  <div className="toast-title">{d.title}</div>
                  <div className="toast-text">{d.body}</div>
                </div>
                {!a.is_test && (
                  <button
                    className="btn-small primary"
                    onClick={() => {
                      onOpen(a.instrument);
                      dismiss(a.id);
                    }}
                  >
                    View report
                  </button>
                )}
                <button className="toast-close" aria-label="Dismiss" onClick={() => dismiss(a.id)}>
                  ×
                </button>
              </div>
            );
          })}
        </div>
      )}

      <div className="alert-card">
        <div className="alert-head">
          <span className="alert-title">Setup alerts</span>
          <span className={`alert-state ${state.className}`}>{state.label}</span>
        </div>

        <div className="alert-line">
          {problem ??
            (lastRun
              ? `The server checks your ${scanner?.watched ?? ""} watched coins every ${scanner?.interval_minutes} min. Last check ${ago(lastRun.finished_at)}.`
              : "The server's first check runs about a minute after it starts.")}
        </div>
        {overdue && (
          <div className="alert-line warn">That's longer ago than expected, so the scanner may have stopped.</div>
        )}
        {lastRun?.errors && <div className="alert-line warn">Last check had errors: {lastRun.errors}</div>}
        <div className="alert-line">Keep this tab open to be alerted. Closing it stops alerts.</div>

        {permission === "denied" && (
          <div className="alert-line warn">
            Notifications are blocked for this site. Allow them in your browser's site settings, then reload.
          </div>
        )}
        {permission === "unsupported" && (
          <div className="alert-line">
            This browser can't show system notifications here (for example Safari on an iPhone). You'll still
            get a banner and a chime while this tab is open.
          </div>
        )}

        <div className="alert-actions">
          {permission === "default" && (
            <button className="btn-small primary" onClick={enable}>
              Turn on notifications
            </button>
          )}
          <button className="btn-small" onClick={sendTest}>
            Send test alert
          </button>
        </div>

        <div className="alert-recent">
          <div className="kv-label">Recent alerts</div>
          {recent.length === 0 ? (
            <div className="alert-line">No setup has qualified since alerts started.</div>
          ) : (
            recent.map((a) => (
              <button
                className="alert-row"
                key={a.id}
                disabled={a.is_test}
                onClick={() => !a.is_test && onOpen(a.instrument)}
              >
                <span className="alert-when mono">{clock(a.created_at)}</span>
                <span className="alert-what">
                  {a.is_test ? "Test alert" : `${a.instrument} · ${a.setup.replace(/_/g, " ")}`}
                </span>
                {!a.is_test && a.setup_case && (
                  <span className={`case-tag ${a.setup_case}`}>{CASE_WORD[a.setup_case]}</span>
                )}
              </button>
            ))
          )}
        </div>
      </div>
    </>
  );
}
