interface ErrorResponse {
  status?: number;
  data?: { detail?: unknown };
}

const responseOf = (err: unknown): ErrorResponse | undefined =>
  (err as { response?: ErrorResponse } | null)?.response;

export const errorStatus = (err: unknown): number | undefined => responseOf(err)?.status;

export const errorDetail = (err: unknown, fallback: string): string => {
  const detail = responseOf(err)?.data?.detail;
  return typeof detail === "string" && detail ? detail : fallback;
};

export const isCanceled = (err: unknown): boolean => {
  const e = err as { name?: unknown; code?: unknown } | null;
  return e?.name === "CanceledError" || e?.code === "ERR_CANCELED";
};

const validationMessage = (detail: unknown): string | null => {
  if (!Array.isArray(detail) || detail.length === 0) return null;
  return detail
    .slice(0, 3)
    .map((d: { loc?: unknown[]; msg?: string }) => {
      const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : null;
      return field && field !== "body" ? `${field}: ${d.msg}` : String(d.msg);
    })
    .join("; ");
};

const stripPrefix = (detail: string) => detail.replace(/^(Unauthorized|Forbidden):\s*/, "");

const capitalise = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

export const errorMessage = (err: unknown): string => {
  if (isCanceled(err)) return "";
  const res = responseOf(err);
  if (!res?.status) {
    const code = (err as { code?: string } | null)?.code;
    if (code === "ECONNABORTED" || code === "ETIMEDOUT") {
      return "The Checkcord API took too long to answer. Try again.";
    }
    return "Cannot reach the Checkcord API. Check that it is running and that the console points at the right address.";
  }
  const { status } = res;
  const raw = res.data?.detail;
  const detail = typeof raw === "string" && raw ? stripPrefix(raw) : null;
  if (status === 401) return "Your sign-in has ended. Sign in again.";
  if (status === 403) {
    if (detail?.includes("CSRF")) return "The request was refused. Reload the page and try again.";
    if (detail?.includes("admin")) return "This needs admin rights.";
    return detail ? capitalise(detail) : "You do not have access to this.";
  }
  if (status === 422) return validationMessage(raw) ?? (detail ? capitalise(detail) : "The request was not valid.");
  if (status === 429) return "Too many requests. Wait a moment and try again.";
  if (status >= 500 && !detail) return `The Checkcord API failed (status ${status}). Check its logs.`;
  if (detail) return capitalise(detail);
  if (status === 404) return "Not found.";
  return `The request failed (status ${status}).`;
};
