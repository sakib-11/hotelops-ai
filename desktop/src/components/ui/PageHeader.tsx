import { forwardRef, type HTMLAttributes } from "react";
import "./PageHeader.css";

export interface PageHeaderProps extends HTMLAttributes<HTMLDivElement> {
  title: string;
  description?: string;
  icon?: React.ReactNode;
  action?: React.ReactNode;
  breadcrumbs?: { label: string; href?: string }[];
  variant?: "default" | "compact";
}

const PageHeader = forwardRef<HTMLDivElement, PageHeaderProps>(
  (
    {
      title,
      description,
      icon,
      action,
      breadcrumbs = [],
      variant = "default",
      className = "",
      ...props
    },
    ref,
  ) => {
    const classNames = ["page-header", `page-header--${variant}`, className]
      .filter(Boolean)
      .join(" ");

    return (
      <div ref={ref} className={classNames} {...props}>
        {breadcrumbs.length > 0 && (
          <nav className="page-header__breadcrumbs" aria-label="Breadcrumb">
            <ol className="page-header__breadcrumb-list">
              {breadcrumbs.map((crumb, index) => (
                <li key={crumb.href ?? index} className="page-header__breadcrumb-item">
                  {index > 0 && (
                    <span className="page-header__breadcrumb-separator" aria-hidden="true">
                      /
                    </span>
                  )}
                  {crumb.href ? (
                    <a href={crumb.href} className="page-header__breadcrumb-link">
                      {crumb.label}
                    </a>
                  ) : (
                    <span className="page-header__breadcrumb-current" aria-current="page">
                      {crumb.label}
                    </span>
                  )}
                </li>
              ))}
            </ol>
          </nav>
        )}
        <div className="page-header__content">
          <div className="page-header__main">
            {icon && (
              <div className="page-header__icon" aria-hidden="true">
                {icon}
              </div>
            )}
            <div className="page-header__text">
              <h1 className="page-header__title">{title}</h1>
              {description && <p className="page-header__description">{description}</p>}
            </div>
          </div>
          {action && <div className="page-header__action">{action}</div>}
        </div>
      </div>
    );
  },
);

PageHeader.displayName = "PageHeader";

export { PageHeader };
