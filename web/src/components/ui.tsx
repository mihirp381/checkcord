"use client";

import { useEffect, useId, useRef, useState } from "react";
import { errorMessage } from "../lib/errors";
import { absoluteTime, relativeTime } from "../lib/format";

type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";

const BUTTON: Record<ButtonVariant, string> = {
  primary: "bg-accent text-accent-fg hover:bg-accent-strong border-transparent",
  secondary: "bg-panel text-foreground border-line hover:bg-subtle",
  danger: "bg-panel text-danger border-danger/50 hover:bg-danger-soft",
  ghost: "bg-transparent text-foreground border-transparent hover:bg-subtle",
};

export function Button({
  variant = "secondary",
  small = false,
  className = "",
  type = "button",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; small?: boolean }) {
  return (
    <button
      type={type}
      className={`inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${small ? "px-2 py-1 text-xs" : "px-3 py-1.5 text-sm"} ${BUTTON[variant]} ${className}`}
      {...props}
    />
  );
}

export const inputClass =
  "w-full rounded-md border border-line bg-panel px-2.5 py-1.5 text-sm text-foreground placeholder:text-muted";

export function Field({
  label,
  hint,
  children,
  className = "",
}: {
  label: string;
  hint?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`flex min-w-0 flex-col gap-1 text-sm ${className}`}>
      <label className="flex flex-col gap-1">
        <span className="font-medium">{label}</span>
        {children}
      </label>
      {hint && <span className="text-xs text-muted">{hint}</span>}
    </div>
  );
}

export function Checkbox({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: React.ReactNode;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="inline-flex items-center gap-2 text-sm">
      <input
        type="checkbox"
        className="h-4 w-4 accent-[var(--accent)]"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>{label}</span>
    </label>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold">{title}</h1>
        {description && <p className="mt-1 max-w-3xl text-sm text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Panel({
  title,
  actions,
  children,
  className = "",
}: {
  title?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`min-w-0 rounded-lg border border-line bg-panel p-4 ${className}`}>
      {(title || actions) && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          {title && <h2 className="text-sm font-semibold">{title}</h2>}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <p role="status" className="py-6 text-sm text-muted">
      {label}…
    </p>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3 rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-sm text-danger">
      <span className="min-w-0 break-words">{message}</span>
      {onRetry && (
        <Button small onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-line px-4 py-6 text-center text-sm">
      <p className="font-medium">{title}</p>
      {children && <div className="mt-1 text-muted">{children}</div>}
    </div>
  );
}

type Tone = "neutral" | "ok" | "warn" | "danger" | "accent";

const TONE: Record<Tone, string> = {
  neutral: "bg-subtle text-foreground",
  ok: "bg-ok-soft text-ok",
  warn: "bg-warn-soft text-warn",
  danger: "bg-danger-soft text-danger",
  accent: "bg-subtle text-accent",
};

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: React.ReactNode }) {
  return (
    <span className={`inline-flex items-center whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium ${TONE[tone]}`}>
      {children}
    </span>
  );
}

export function Notice({
  tone = "ok",
  children,
  onDismiss,
}: {
  tone?: Tone;
  children: React.ReactNode;
  onDismiss?: () => void;
}) {
  return (
    <div role="status" className={`mb-4 flex items-start justify-between gap-3 rounded-md px-3 py-2 text-sm ${TONE[tone]}`}>
      <div className="min-w-0 break-words">{children}</div>
      {onDismiss && (
        <button type="button" className="text-xs underline" onClick={onDismiss}>
          Dismiss
        </button>
      )}
    </div>
  );
}

export function Time({ iso, empty = "Never" }: { iso: string | null | undefined; empty?: string }) {
  if (!iso) return <span className="text-muted">{empty}</span>;
  const abs = absoluteTime(iso);
  return (
    <time dateTime={iso} title={abs} className="flex flex-col leading-tight">
      <span>{relativeTime(iso)}</span>
      <span className="text-xs text-muted">{abs}</span>
    </time>
  );
}

export function Table({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-md border border-line">
      <table aria-label={label} className="w-full border-collapse text-left text-sm">
        {children}
      </table>
    </div>
  );
}

export function Th({ children, className = "" }: { children?: React.ReactNode; className?: string }) {
  return (
    <th scope="col" className={`border-b border-line bg-subtle px-3 py-2 text-xs font-semibold text-muted ${className}`}>
      {children}
    </th>
  );
}

export function Td({ children, className = "" }: { children?: React.ReactNode; className?: string }) {
  return (
    <td className={`border-b border-line px-3 py-2 align-top break-words [overflow-wrap:anywhere] ${className}`}>
      {children}
    </td>
  );
}

export function Mono({ children }: { children: React.ReactNode }) {
  return <code className="font-mono text-xs [overflow-wrap:anywhere]">{children}</code>;
}

export function CopyField({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState<"yes" | "no" | null>(null);
  const id = useId();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied("yes");
    } catch {
      setCopied("no");
    }
  };
  return (
    <div className="flex flex-col gap-1 text-sm">
      <label htmlFor={id} className="font-medium">
        {label}
      </label>
      <div className="flex gap-2">
        <input
          id={id}
          readOnly
          value={value}
          className={`${inputClass} font-mono`}
          onFocus={(e) => e.currentTarget.select()}
        />
        <Button onClick={copy}>Copy</Button>
      </div>
      <span role="status" className="text-xs text-muted">
        {copied === "yes" ? "Copied." : copied === "no" ? "Could not copy. Select the text and copy it by hand." : ""}
      </span>
    </div>
  );
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Dialog({
  title,
  onClose,
  children,
  footer,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const box = ref.current;
    const first = box?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? box)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeRef.current();
        return;
      }
      if (e.key !== "Tab" || !box) return;
      const items = Array.from(box.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (items.length === 0) return;
      const [head, tail] = [items[0], items[items.length - 1]];
      if (e.shiftKey && document.activeElement === head) {
        e.preventDefault();
        tail.focus();
      } else if (!e.shiftKey && document.activeElement === tail) {
        e.preventDefault();
        head.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      opener?.focus?.();
    };
  }, []);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg border border-line bg-panel p-5 shadow-xl"
      >
        <h2 id={titleId} className="text-base font-semibold">
          {title}
        </h2>
        <div className="mt-3 text-sm">{children}</div>
        {footer && <div className="mt-5 flex flex-wrap justify-end gap-2">{footer}</div>}
      </div>
    </div>
  );
}

export function ConfirmDialog({
  title,
  confirmLabel,
  onConfirm,
  onClose,
  children,
  danger = true,
}: {
  title: string;
  confirmLabel: string;
  onConfirm: () => Promise<unknown>;
  onClose: () => void;
  children: React.ReactNode;
  danger?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
      onClose();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={title}
      onClose={busy ? () => {} : onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant={danger ? "danger" : "primary"} onClick={confirm} disabled={busy}>
            {busy ? "Working…" : confirmLabel}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-2">{children}</div>
      {error && (
        <div className="mt-3">
          <ErrorState message={error} />
        </div>
      )}
    </Dialog>
  );
}

export function More({
  list,
}: {
  list: { hasMore: boolean; loadMore: () => Promise<void>; loadingMore: boolean; moreError: string | null };
}) {
  return (
    <>
      {list.moreError && (
        <div className="mt-3">
          <ErrorState message={list.moreError} onRetry={() => void list.loadMore()} />
        </div>
      )}
      {list.hasMore && (
        <div className="mt-3 flex justify-center">
          <Button onClick={() => void list.loadMore()} disabled={list.loadingMore}>
            {list.loadingMore ? "Loading…" : "Load more"}
          </Button>
        </div>
      )}
    </>
  );
}

export function Stat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <div className="min-w-0 rounded-md border border-line bg-panel px-3 py-2">
      <p className="text-xs text-muted">{label}</p>
      <p className="mt-0.5 text-lg font-semibold [overflow-wrap:anywhere]">{value}</p>
      {hint && <p className="text-xs text-muted">{hint}</p>}
    </div>
  );
}
