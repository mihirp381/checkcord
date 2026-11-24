const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
  ["second", 1],
];

const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const absolute = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });
const numbers = new Intl.NumberFormat("en");

export const relativeTime = (iso: string, now: number = Date.now()): string => {
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000);
  if (Math.abs(seconds) < 45) return "just now";
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return relative.format(Math.round(seconds / size), unit);
  }
  return "just now";
};

export const absoluteTime = (iso: string): string => absolute.format(new Date(iso));

export const formatNumber = (n: number | null | undefined): string =>
  n === null || n === undefined ? "–" : numbers.format(n);

export const formatCost = (n: number | null | undefined): string =>
  n === null || n === undefined
    ? "Not known"
    : n < 0.01 && n > 0
      ? "< $0.01"
      : `$${n.toFixed(2)}`;

export const formatAge = (seconds: number | null | undefined): string => {
  if (seconds === null || seconds === undefined) return "–";
  for (const [unit, size] of UNITS) {
    if (seconds >= size) {
      const n = Math.floor(seconds / size);
      return `${n} ${unit}${n === 1 ? "" : "s"}`;
    }
  }
  return "under a second";
};

export const plural = (n: number, one: string, many = `${one}s`): string =>
  `${formatNumber(n)} ${n === 1 ? one : many}`;

export const shortId = (id: string): string => id.slice(0, 8);
