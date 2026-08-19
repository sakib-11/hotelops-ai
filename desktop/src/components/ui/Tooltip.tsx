import {
  useState,
  useRef,
  useEffect,
  type HTMLAttributes,
  forwardRef,
  type ReactNode,
  type ReactElement,
} from "react";
import "./Tooltip.css";

export type TooltipPlacement = "top" | "bottom" | "left" | "right";

interface TooltipBaseProps {
  content: ReactNode;
  children: ReactElement;
  placement?: TooltipPlacement;
  delay?: number;
  disabled?: boolean;
}

type TooltipProps = TooltipBaseProps & Omit<HTMLAttributes<HTMLDivElement>, "children" | "content">;

const Tooltip = forwardRef<HTMLDivElement, TooltipProps>(
  (
    {
      content,
      children,
      placement = "top",
      delay = 200,
      disabled = false,
      className = "",
      ...props
    },
    ref,
  ) => {
    const [visible, setVisible] = useState(false);
    const [position, setPosition] = useState({ top: 0, left: 0 });
    const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const triggerRef = useRef<HTMLElement>(null);
    const tooltipRef = useRef<HTMLDivElement>(null);

    const updatePosition = () => {
      if (!triggerRef.current || !tooltipRef.current) return;

      const triggerRect = triggerRef.current.getBoundingClientRect();
      const tooltipRect = tooltipRef.current.getBoundingClientRect();
      const gap = 8;

      let top = 0;
      let left = 0;

      switch (placement) {
        case "top":
          top = triggerRect.top - tooltipRect.height - gap;
          left = triggerRect.left + (triggerRect.width - tooltipRect.width) / 2;
          break;
        case "bottom":
          top = triggerRect.bottom + gap;
          left = triggerRect.left + (triggerRect.width - tooltipRect.width) / 2;
          break;
        case "left":
          top = triggerRect.top + (triggerRect.height - tooltipRect.height) / 2;
          left = triggerRect.left - tooltipRect.width - gap;
          break;
        case "right":
          top = triggerRect.top + (triggerRect.height - tooltipRect.height) / 2;
          left = triggerRect.right + gap;
          break;
      }

      setPosition({ top, left });
    };

    const show = () => {
      if (disabled) return;
      timeoutRef.current = setTimeout(() => {
        setVisible(true);
        requestAnimationFrame(updatePosition);
      }, delay);
    };

    const hide = () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      setVisible(false);
    };

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") hide();
    };

    useEffect(() => {
      if (visible) {
        updatePosition();
        window.addEventListener("scroll", hide, { passive: true });
        window.addEventListener("resize", hide, { passive: true });
        document.addEventListener("keydown", handleKeyDown);
      }
      return () => {
        window.removeEventListener("scroll", hide);
        window.removeEventListener("resize", hide);
        document.removeEventListener("keydown", handleKeyDown);
      };
    }, [visible]);

    const child = children;

    return (
      <div
        ref={ref}
        className={`tooltip-trigger ${className}`}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        {...props}
      >
        {child}
        {visible && (
          <div
            ref={tooltipRef}
            className={`tooltip tooltip--${placement}`}
            style={{ top: position.top, left: position.left }}
            role="tooltip"
            aria-hidden="false"
          >
            <div className="tooltip__content">{content}</div>
            <div className="tooltip__arrow" aria-hidden="true" />
          </div>
        )}
      </div>
    );
  },
);

Tooltip.displayName = "Tooltip";

export { Tooltip };
