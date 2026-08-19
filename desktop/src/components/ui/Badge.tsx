import { forwardRef, type HTMLAttributes } from "react";
import "./Badge.css";

export type BadgeVariant =
  "default" | "primary" | "secondary" | "success" | "warning" | "error" | "info";
export type BadgeSize = "sm" | "md";

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
  size?: BadgeSize;
  dot?: boolean;
  removable?: boolean;
  onRemove?: () => void;
}

const Badge = forwardRef<HTMLSpanElement, BadgeProps>(
  (
    {
      variant = "default",
      size = "md",
      dot = false,
      removable = false,
      onRemove,
      children,
      className = "",
      ...props
    },
    ref,
  ) => {
    const classNames = [
      "badge",
      `badge--${variant}`,
      `badge--${size}`,
      dot ? "badge--dot" : "",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <span ref={ref} className={classNames} {...props}>
        {dot && <span className="badge__dot" aria-hidden="true" />}
        <span className="badge__text">{children}</span>
        {removable && (
          <button type="button" className="badge__remove" onClick={onRemove} aria-label={`Remove`}>
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              aria-hidden="true"
            >
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        )}
      </span>
    );
  },
);

Badge.displayName = "Badge";

export interface StatusBadgeProps extends Omit<BadgeProps, "variant"> {
  status:
    | "online"
    | "offline"
    | "healthy"
    | "degraded"
    | "stale"
    | "processing"
    | "completed"
    | "failed"
    | "warning"
    | "critical";
  label: string;
  showDot?: boolean;
}

const statusVariantMap: Record<StatusBadgeProps["status"], BadgeVariant> = {
  online: "success",
  offline: "default",
  healthy: "success",
  degraded: "warning",
  stale: "warning",
  processing: "info",
  completed: "success",
  failed: "error",
  warning: "warning",
  critical: "error",
};

const StatusBadge = forwardRef<HTMLSpanElement, StatusBadgeProps>(
  ({ status, label, showDot = true, size = "md", className = "", ...props }, ref) => {
    const variant = statusVariantMap[status];

    return (
      <Badge
        ref={ref}
        variant={variant}
        size={size}
        dot={showDot}
        className={`badge--status ${className}`}
        {...props}
      >
        {label}
      </Badge>
    );
  },
);

StatusBadge.displayName = "StatusBadge";

export { Badge, StatusBadge };
