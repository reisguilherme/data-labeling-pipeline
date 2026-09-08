import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cx } from "../lib/format";

type Variant = "default" | "primary" | "danger" | "ghost";

const VARIANTS: Record<Variant, string> = {
  default:
    "bg-zinc-900 border-zinc-800 text-zinc-200 hover:bg-zinc-800 hover:border-zinc-700",
  primary:
    "bg-emerald-600 border-emerald-600 text-white hover:bg-emerald-500 hover:border-emerald-500",
  danger:
    "bg-zinc-900 border-red-900/60 text-red-400 hover:bg-red-950 hover:border-red-800",
  ghost: "bg-transparent border-transparent text-zinc-400 hover:text-zinc-100 hover:bg-zinc-900",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  kbd?: string;
}

export function Button({
  variant = "default",
  kbd,
  className,
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      className={cx(
        "inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm",
        "transition-colors disabled:cursor-not-allowed disabled:opacity-40",
        VARIANTS[variant],
        className,
      )}
    >
      {children}
      {kbd && <Kbd>{kbd}</Kbd>}
    </button>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded border border-zinc-700 bg-zinc-950/60 px-1 py-px font-sans text-[10px] leading-none text-zinc-400">
      {children}
    </kbd>
  );
}

export function Chip({
  active,
  onClick,
  children,
  count,
  tone = "zinc",
}: {
  active?: boolean;
  onClick?: () => void;
  children: ReactNode;
  count?: number;
  tone?: "zinc" | "emerald" | "amber";
}) {
  const activeTone =
    tone === "emerald"
      ? "bg-emerald-950 border-emerald-800 text-emerald-300"
      : tone === "amber"
        ? "bg-amber-950 border-amber-800 text-amber-300"
        : "bg-zinc-800 border-zinc-600 text-zinc-100";
  return (
    <button
      onClick={onClick}
      className={cx(
        "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors",
        active
          ? activeTone
          : "border-zinc-800 bg-zinc-900/60 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200",
      )}
    >
      {children}
      {count !== undefined && <span className="tnum opacity-60">{count}</span>}
    </button>
  );
}

export function Panel({
  title,
  right,
  children,
  className,
}: {
  title?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={cx("rounded-md border border-zinc-800 bg-zinc-900/50", className)}
    >
      {title && (
        <header className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
          <h2 className="text-xs font-medium tracking-wide text-zinc-400 uppercase">
            {title}
          </h2>
          {right}
        </header>
      )}
      <div className="p-3">{children}</div>
    </section>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg
      className={cx("animate-spin", className)}
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" opacity="0.2" />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function CheckBadge({ className }: { className?: string }) {
  return (
    <span
      className={cx(
        "flex h-6 w-6 items-center justify-center rounded-full bg-emerald-600 text-white shadow-sm ring-2 ring-zinc-950",
        className,
      )}
    >
      <svg viewBox="0 0 20 20" width="13" height="13" fill="currentColor">
        <path d="M7.6 13.6 4 10l1.3-1.3 2.3 2.3 6.1-6.1L15 6.2z" />
      </svg>
    </span>
  );
}
