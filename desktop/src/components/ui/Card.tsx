import { forwardRef, type HTMLAttributes, type ForwardRefExoticComponent } from "react";
import "./Card.css";

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  variant?: "default" | "elevated" | "outlined";
  padding?: "none" | "sm" | "md" | "lg";
  hoverable?: boolean;
}

export interface CardHeaderProps extends HTMLAttributes<HTMLDivElement> {
  title: string;
  description?: string;
  action?: React.ReactNode;
}

export type CardContentProps = HTMLAttributes<HTMLDivElement>;

export type CardFooterProps = HTMLAttributes<HTMLDivElement>;

interface CardCompoundComponent extends ForwardRefExoticComponent<HTMLAttributes<HTMLDivElement>> {
  Header: ForwardRefExoticComponent<CardHeaderProps>;
  Content: ForwardRefExoticComponent<CardContentProps>;
  Footer: ForwardRefExoticComponent<CardFooterProps>;
}

const Card = forwardRef<HTMLDivElement, CardProps>(
  (
    { variant = "default", padding = "md", hoverable = false, className = "", children, ...props },
    ref,
  ) => {
    const classNames = [
      "card",
      `card--${variant}`,
      `card--padding-${padding}`,
      hoverable ? "card--hoverable" : "",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <div ref={ref} className={classNames} {...props}>
        {children}
      </div>
    );
  },
);

Card.displayName = "Card";

const CardHeader = forwardRef<HTMLDivElement, CardHeaderProps>(
  ({ title, description, action, className = "", ...props }, ref) => {
    return (
      <div ref={ref} className={`card__header ${className}`} {...props}>
        <div className="card__header-content">
          <h3 className="card__title">{title}</h3>
          {description && <p className="card__description">{description}</p>}
        </div>
        {action && <div className="card__header-action">{action}</div>}
      </div>
    );
  },
);

CardHeader.displayName = "CardHeader";

const CardContent = forwardRef<HTMLDivElement, CardContentProps>(
  ({ className = "", children, ...props }, ref) => {
    return (
      <div ref={ref} className={`card__content ${className}`} {...props}>
        {children}
      </div>
    );
  },
);

CardContent.displayName = "CardContent";

const CardFooter = forwardRef<HTMLDivElement, CardFooterProps>(
  ({ className = "", children, ...props }, ref) => {
    return (
      <div ref={ref} className={`card__footer ${className}`} {...props}>
        {children}
      </div>
    );
  },
);

CardFooter.displayName = "CardFooter";

const CardCompound = Card as CardCompoundComponent;
CardCompound.Header = CardHeader;
CardCompound.Content = CardContent;
CardCompound.Footer = CardFooter;

export { CardCompound as Card, CardHeader, CardContent, CardFooter };
