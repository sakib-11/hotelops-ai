import { forwardRef, type HTMLAttributes } from "react";
import "./NavigationItem.css";

export interface NavigationItemProps extends HTMLAttributes<HTMLButtonElement> {
  label: string;
  icon?: React.ReactNode;
  badge?: React.ReactNode;
  active?: boolean;
  disabled?: boolean;
  onClick?: () => void;
  variant?: "default" | "compact";
}

const NavigationItem = forwardRef<HTMLButtonElement, NavigationItemProps>(
  (
    {
      label,
      icon,
      badge,
      active = false,
      disabled = false,
      onClick,
      variant = "default",
      className = "",
      ...props
    },
    ref,
  ) => {
    const classNames = [
      "nav-item",
      `nav-item--${variant}`,
      active ? "nav-item--active" : "",
      disabled ? "nav-item--disabled" : "",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <button
        ref={ref}
        type="button"
        className={classNames}
        disabled={disabled}
        aria-current={active ? "page" : undefined}
        onClick={onClick}
        {...props}
      >
        {icon && (
          <span className="nav-item__icon" aria-hidden="true">
            {icon}
          </span>
        )}
        <span className="nav-item__label">{label}</span>
        {badge && (
          <span className="nav-item__badge" aria-hidden="true">
            {badge}
          </span>
        )}
        {active && <span className="nav-item__indicator" aria-hidden="true" />}
      </button>
    );
  },
);

NavigationItem.displayName = "NavigationItem";

export { NavigationItem };
