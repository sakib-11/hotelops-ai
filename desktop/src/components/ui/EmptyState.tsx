import { forwardRef, type HTMLAttributes } from "react";
import { Button } from "./Button";
import "./EmptyState.css";

export interface EmptyStateProps extends HTMLAttributes<HTMLDivElement> {
  title: string;
  description?: string;
  icon?: React.ReactNode;
  action?: React.ReactNode;
  size?: "sm" | "md" | "lg";
}

export interface ErrorStateProps extends HTMLAttributes<HTMLDivElement> {
  title: string;
  message: string;
  code?: string;
  icon?: React.ReactNode;
  action?: React.ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
  size?: "sm" | "md" | "lg";
  dismissible?: boolean;
  onDismiss?: () => void;
}

const EmptyState = forwardRef<HTMLDivElement, EmptyStateProps>(
  ({ title, description, icon, action, size = "md", className = "", ...props }, ref) => {
    const classNames = ["empty-state", `empty-state--${size}`, className].filter(Boolean).join(" ");

    const defaultIcon = (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        aria-hidden="true"
      >
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <path d="M9 12h6" />
        <path d="M12 9v6" />
      </svg>
    );

    return (
      <div ref={ref} className={classNames} {...props}>
        <div className="empty-state__icon" aria-hidden="true">
          {icon ?? defaultIcon}
        </div>
        <h3 className="empty-state__title">{title}</h3>
        {description && <p className="empty-state__description">{description}</p>}
        {action && <div className="empty-state__action">{action}</div>}
      </div>
    );
  },
);

EmptyState.displayName = "EmptyState";

const ErrorState = forwardRef<HTMLDivElement, ErrorStateProps>(
  (
    {
      title,
      message,
      code,
      icon,
      action,
      onRetry,
      retryLabel = "Try again",
      size = "md",
      dismissible = false,
      onDismiss,
      className = "",
      ...props
    },
    ref,
  ) => {
    const classNames = ["error-state", `error-state--${size}`, className].filter(Boolean).join(" ");

    const defaultIcon = (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="10" />
        <path d="M12 8v4" />
        <path d="M12 16h.01" />
      </svg>
    );

    return (
      <div ref={ref} className={classNames} {...props}>
        {dismissible && onDismiss && (
          <button
            type="button"
            className="error-state__dismiss"
            onClick={onDismiss}
            aria-label="Dismiss error"
          >
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
        <div className="error-state__icon" aria-hidden="true">
          {icon ?? defaultIcon}
        </div>
        <h3 className="error-state__title">{title}</h3>
        <p className="error-state__message">{message}</p>
        {code && <p className="error-state__code">Error code: {code}</p>}
        {onRetry && (
          <Button variant="primary" size={size === "sm" ? "sm" : "md"} onClick={onRetry}>
            {retryLabel}
          </Button>
        )}
        {action && <div className="error-state__action">{action}</div>}
      </div>
    );
  },
);

ErrorState.displayName = "ErrorState";

export { EmptyState, ErrorState };
