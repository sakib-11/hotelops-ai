import { forwardRef, type ButtonHTMLAttributes } from "react";
import "./Button.css";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "destructive";
export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  icon?: React.ReactNode;
  iconPosition?: "start" | "end";
  fullWidth?: boolean;
}

const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      variant = "primary",
      size = "md",
      loading = false,
      icon,
      iconPosition = "start",
      fullWidth = false,
      disabled,
      children,
      className = "",
      ...props
    },
    ref,
  ) => {
    const isDisabled = (disabled ?? false) || loading;

    const classNames = [
      "btn",
      `btn--${variant}`,
      `btn--${size}`,
      fullWidth ? "btn--full-width" : "",
      loading ? "btn--loading" : "",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <button
        ref={ref}
        className={classNames}
        disabled={isDisabled}
        aria-busy={loading}
        aria-disabled={isDisabled}
        {...props}
      >
        {loading && <span className="btn__spinner" aria-hidden="true" />}
        {!loading && icon && iconPosition === "start" && (
          <span className="btn__icon btn__icon--start">{icon}</span>
        )}
        <span className="btn__text">{children}</span>
        {!loading && icon && iconPosition === "end" && (
          <span className="btn__icon btn__icon--end">{icon}</span>
        )}
      </button>
    );
  },
);

Button.displayName = "Button";

export interface IconButtonProps extends Omit<
  ButtonProps,
  "icon" | "iconPosition" | "fullWidth" | "children"
> {
  "aria-label": string;
  children: React.ReactNode;
  size?: "sm" | "md" | "lg";
}

const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(
  ({ "aria-label": ariaLabel, children, size = "md", className = "", ...props }, ref) => {
    return (
      <Button
        ref={ref}
        variant="ghost"
        size={size}
        className={`btn--icon ${className}`}
        aria-label={ariaLabel}
        {...props}
      >
        <span className="btn__icon-only" aria-hidden="true">
          {children}
        </span>
      </Button>
    );
  },
);

IconButton.displayName = "IconButton";

export { Button, IconButton };
