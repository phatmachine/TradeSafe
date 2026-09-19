// Display-only tidying of the backend's stringified values: unwraps Python Decimal('…')
// reprs and dict-key quotes, and shortens long decimals. The full-precision string stays
// available (callers put it in a title attribute) — the JSON is still the record.
function shorten(n: string): string {
  const x = Number(n);
  if (!Number.isFinite(x)) return n;
  const s = Math.abs(x) >= 1 ? x.toFixed(2) : x.toPrecision(4);
  if (s.includes("e")) return n;
  return s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s;
}

export function readable(v: string | null | undefined): string {
  if (v == null) return "—";
  return v
    .replace(/Decimal\('([^']*)'\)/g, "$1")
    .replace(/'([A-Za-z_][A-Za-z0-9_]*)':/g, "$1:")
    .replace(/[{}]/g, "")
    .replace(/-?\d+\.\d{5,}/g, shorten);
}
