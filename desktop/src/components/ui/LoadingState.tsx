import { forwardRef, type HTMLAttributes } from "react";
import "./LoadingState.css";

export type LoadingSize = "sm" | "md" | "lg";
export type LoadingVariant = "spinner" | "dots" | "pulse";

export interface LoadingStateProps extends HTMLAttributes<HTMLDivElement> {
  size?: LoadingSize;
  variant?: LoadingVariant;
  label?: string;
  overlay?: boolean;
  fullScreen?: boolean;
}

export interface SkeletonProps extends HTMLAttributes<HTMLDivElement> {
  variant?: "text" | "circular" | "rectangular";
  width?: string | number;
  height?: string | number;
  lines?: number;
  animated?: boolean;
}

const LoadingState = forwardRef<HTMLDivElement, LoadingStateProps>(
  (
    {
      size = "md",
      variant = "spinner",
      label,
      overlay = false,
      fullScreen = false,
      className = "",
      style,
      ...props
    },
    ref,
  ) => {
    const classNames = [
      "loading",
      `loading--${size}`,
      `loading--${variant}`,
      overlay ? "loading--overlay" : "",
      fullScreen ? "loading--fullscreen" : "",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    const sizeStyles: Record<LoadingSize, { width: string; height: string; borderWidth: string }> =
      {
        sm: { width: "16px", height: "16px", borderWidth: "2px" },
        md: { width: "24px", height: "24px", borderWidth: "2px" },
        lg: { width: "32px", height: "32px", borderWidth: "3px" },
      };

    const spinnerStyle = {
      ...sizeStyles[size],
      borderColor: `currentColor transparent currentColor transparent`,
    } as React.CSSProperties;

    if (fullScreen) {
      return (
        <div
          ref={ref}
          className={classNames}
          style={style}
          role="status"
          aria-live="polite"
          aria-busy="true"
          {...props}
        >
          <div className="loading__backdrop" />
          <div className="loading__container">
            <div className="loading__spinner" style={spinnerStyle} aria-hidden="true" />
            {label && <p className="loading__label">{label}</p>}
          </div>
        </div>
      );
    }

    return (
      <div
        ref={ref}
        className={classNames}
        style={style}
        role="status"
        aria-live="polite"
        aria-busy="true"
        {...props}
      >
        <div className="loading__spinner" style={spinnerStyle} aria-hidden="true" />
        {label && <p className="loading__label">{label}</p>}
      </div>
    );
  },
);

LoadingState.displayName = "LoadingState";

const Skeleton = forwardRef<HTMLDivElement, SkeletonProps>(
  (
    {
      variant = "rectangular",
      width = "100%",
      height,
      lines = 1,
      animated = true,
      className = "",
      style,
      ...props
    },
    ref,
  ) => {
    const classNames = [
      "skeleton",
      `skeleton--${variant}`,
      animated ? "skeleton--animated" : "",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    const baseStyle: React.CSSProperties = {
      width,
      ...(height && { height }),
      ...style,
    };

    if (variant === "text" && lines > 1) {
      return (
        <div ref={ref} className={classNames} style={baseStyle} {...props} aria-hidden="true">
          {Array.from({ length: lines }).map((_, i) => (
            <div
              key={i}
              className="skeleton__line"
              style={{
                width: i === lines - 1 ? "60%" : "100%",
                height: "12px",
                marginBottom: i < lines - 1 ? "8px" : 0,
              }}
            />
          ))}
        </div>
      );
    }

    return <div ref={ref} className={classNames} style={baseStyle} {...props} aria-hidden="true" />;
  },
);

Skeleton.displayName = "Skeleton";

export { LoadingState, Skeleton };
