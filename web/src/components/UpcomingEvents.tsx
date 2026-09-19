import { useEffect, useState } from "react";
import { api, CalendarEvent, CalendarResponse } from "../api";

// The schedule changes rarely. Refreshing every 10 minutes moves a release from "coming
// up" to "released" soon after it happens; the clock below keeps "in 3 h" current.
const REFRESH_MS = 10 * 60_000;
const SOON_HOURS = 48;
const HOUR = 3_600_000;

const WHAT: Record<string, string> = {
  us_cpi: "consumer price inflation",
  us_jobs: "US employment",
  us_pce: "the Fed's preferred inflation measure",
  fomc: "US interest-rate decision",
};

function span(ms: number) {
  const minutes = Math.round(ms / 60_000);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ${minutes % 60} min`;
  return `${Math.floor(hours / 24)} d ${hours % 24} h`;
}

function when(iso: string) {
  return new Date(iso).toLocaleString([], {
    weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit",
  });
}

function Row({ e, now, recentHours }: { e: CalendarEvent; now: number; recentHours?: number }) {
  const t = Date.parse(e.at);
  const past = t <= now;
  const soon = !past && t - now <= SOON_HOURS * HOUR;
  return (
    <div className={`event-row ${past ? "released" : soon ? "soon" : ""}`}>
      <span className="event-when mono">{when(e.at)}</span>
      <span className="event-what">
        {e.label}
        <span className="event-about"> · {WHAT[e.kind] ?? e.kind}</span>
      </span>
      <span className="event-rel">
        {past
          ? `released ${span(now - t)} ago · event setup window closes in ${span(t + (recentHours ?? 24) * HOUR - now)}`
          : `in ${span(t - now)}`}
      </span>
    </div>
  );
}

export function UpcomingEvents() {
  const [data, setData] = useState<CalendarResponse | null>(null);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const load = () => api.calendar().then(setData).catch(() => {});
    load();
    const refresh = window.setInterval(load, REFRESH_MS);
    const tick = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => {
      window.clearInterval(refresh);
      window.clearInterval(tick);
    };
  }, []);

  if (!data) return null;
  const recent = data.recent.filter((e) => now - Date.parse(e.at) < data.recent_hours * HOUR);
  const upcoming = data.upcoming.filter((e) => Date.parse(e.at) > now);
  if (!recent.length && !upcoming.length) return null;

  const inWindow = upcoming.filter((e) => Date.parse(e.at) - now <= data.window_days * 24 * HOUR);
  const soon = upcoming.some((e) => Date.parse(e.at) - now <= SOON_HOURS * HOUR);
  const checked = Math.min(...upcoming.map((e) => Date.parse(e.fetched_at ?? data.as_of)));

  return (
    <div className={`event-card ${soon ? "soon" : ""}`}>
      <div className="alert-head">
        <span className="alert-title">Scheduled US releases</span>
        {soon && <span className="event-flag">Within 48 h</span>}
      </div>

      {recent.map((e) => (
        <Row key={`${e.kind}-${e.at}`} e={e} now={now} recentHours={data.recent_hours} />
      ))}
      {inWindow.length > 0
        ? inWindow.map((e) => <Row key={`${e.kind}-${e.at}`} e={e} now={now} />)
        : upcoming.length > 0 && (
            <>
              <div className="alert-line">Nothing in the next {data.window_days} days. The next one:</div>
              <Row e={upcoming[0]} now={now} />
            </>
          )}

      <div className="alert-line event-note">
        Crypto often moves sharply around these releases, in either direction. The event setup is read
        only in the {data.recent_hours} hours after one. Times are in your time zone; dates come from FRED
        and the Federal Reserve.
      </div>
      {upcoming.length > 0 && now - checked > 24 * HOUR && (
        <div className="alert-line warn">
          This schedule was last confirmed {span(now - checked)} ago; its source hasn't been readable since.
        </div>
      )}
    </div>
  );
}
