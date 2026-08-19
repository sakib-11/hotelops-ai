import { forwardRef, type HTMLAttributes } from "react";
import "./Divider.css";

export type DividerOrientation = "horizontal" | "vertical";
export type DividerVariant = "default" | "dashed" | "dotted";

export interface DividerProps extends HTMLAttributes<HTMLDivElement> {
  orientation?: DividerOrientation;
  variant?: DividerVariant;
  label?: string;
  labelPosition?: "start" | "center" | "end";
  thickness?: "thin" | "medium" | "thick";
}

const Divider = forwardRef<HTMLDivElement, DividerProps>(
  (
    {
      orientation = "horizontal",
      variant = "default",
      label,
      labelPosition = "center",
      thickness = "thin",
      className = "",
      children: _children,
      ...props
    },
    ref,
  ) => {
    const classNames = [
      "divider",
      `divider--${orientation}`,
      `divider--${variant}`,
      `divider--${thickness}`,
      label ? `divider--with-label` : "",
      label ? `divider--label-${labelPosition}` : "",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    if (label) {
      return (
        <div ref={ref} className={classNames} {...props} role="separator">
          {labelPosition !== "end" && <span className="divider__line" aria-hidden="true" />}
          <span className="divider__label">{label}</span>
          {labelPosition !== "start" && <span className="divider__line" aria-hidden="true" />}
        </div>
      );
    }

    return <div ref={ref} className={classNames} {...props} role="separator" />;
  },
);

Divider.displayName = "Divider";

export { Divider };
