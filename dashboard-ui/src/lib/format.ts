// Money display is STRING arithmetic on the API's decimal strings.
// IEEE floats never touch a money value (Global Constraints).

export function fmtMoney(value: string | null | undefined, dp: number): string {
  if (value == null || value === "") return "—";
  let s = String(value);
  const neg = s.startsWith("-");
  if (neg) s = s.slice(1);
  if (!/^\d+(\.\d*)?$/.test(s)) return String(value);
  let [intPart, frac = ""] = s.split(".");
  frac = frac.padEnd(dp + 1, "0");
  const keep = frac.slice(0, dp);
  const roundUp = frac.charCodeAt(dp) - 48 >= 5;
  let digits = intPart + keep;
  if (roundUp) {
    const a = digits.split("");
    let k = a.length - 1;
    while (k >= 0) {
      if (a[k] === "9") { a[k] = "0"; k -= 1; }
      else { a[k] = String(+a[k] + 1); break; }
    }
    if (k < 0) a.unshift("1");
    digits = a.join("");
  }
  let ip = dp ? digits.slice(0, -dp) : digits;
  const fp = dp ? digits.slice(-dp) : "";
  ip = (ip.replace(/^0+(?=\d)/, "") || "0")
    .replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  // Rounding can carry a negative value to zero (e.g. "-0.001" at 2dp) —
  // suppress the sign once every kept digit is 0, so it reads "$0.00"
  // instead of the misleading "−$0.00".
  const isZero = !/[1-9]/.test(digits);
  return (neg && !isZero ? "−" : "") + "$" + ip + (dp ? "." + fp : "");
}

export function fmtSignedMoney(
  value: string | null | undefined,
  dp: number,
): { text: string; cls: "pos" | "neg" | "flat" } {
  if (value == null || value === "") return { text: "—", cls: "flat" };
  const raw = String(value);
  if (!/^-?\d+(\.\d*)?$/.test(raw)) return { text: raw, cls: "flat" };
  const rendered = fmtMoney(raw, dp);
  const isZero = !/[1-9]/.test(rendered);
  const cls = isZero ? "flat" : raw.startsWith("-") ? "neg" : "pos";
  return { text: cls === "pos" ? `+${rendered}` : rendered, cls };
}

export function fmtPct(
  ratio: string | null | undefined,
): { text: string; cls: "pos" | "neg" | "flat" } {
  if (ratio == null || ratio === "") return { text: "—", cls: "flat" };
  const v = Number(ratio) * 100; // a ratio, not money — float is fine
  if (!Number.isFinite(v)) return { text: "—", cls: "flat" };
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  const cls = v > 0 ? "pos" : v < 0 ? "neg" : "flat";
  return { text: sign + Math.abs(v).toFixed(2) + "%", cls };
}

export function fmtQty(q: string): string {
  // Round paper-fill precision noise to at most 2dp, then strip trailing
  // zeros so whole shares read "12" not "12.00".
  const bare = fmt2(q);
  if (bare === "—") return q;
  // fmt2 emits a Unicode minus (−) and thousands separators; quantities
  // read as plain ASCII "-" with no grouping commas.
  return bare.replace("−", "-").replace(/,/g, "").replace(/\.?0+$/, "");
}

// Bare 2dp number (no currency symbol) reusing fmtMoney's string-safe,
// float-free rounding. For prices/entries/stops that are decimal strings.
export function fmt2(value: string | null | undefined): string {
  const m = fmtMoney(value, 2);
  return m.startsWith("$") ? m.slice(1)
    : m.startsWith("−$") ? "−" + m.slice(2)
    : m;
}

export function relAge(iso: string | null | undefined, nowMs = Date.now()): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const d = (nowMs - t) / 1000;
  if (d < 45) return "just now";
  if (d < 3600) return Math.round(d / 60) + "m ago";
  if (d < 86400) return Math.round(d / 3600) + "h ago";
  return Math.round(d / 86400) + "d ago";
}

export function hhmmss(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function guardAge(sec: number | null | undefined): string {
  if (sec == null) return "—";
  if (sec < 90) return Math.round(sec) + "s";
  if (sec < 3600) return Math.round(sec / 60) + "m";
  return Math.round(sec / 3600) + "h";
}

export function hhmmET(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", {
    hour: "2-digit", minute: "2-digit", hour12: false,
    timeZone: "America/New_York",
  }) + " ET";
}
