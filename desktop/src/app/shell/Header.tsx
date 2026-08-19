import type { HeaderProps } from "./types";
import "./Header.css";

export function Header({ title, breadcrumbs = [], actions }: HeaderProps) {
  return (
    <header className="header" role="banner">
      <div className="header-left">
        <h1 className="header-title">{title}</h1>
        {breadcrumbs.length > 0 && (
          <nav className="header-breadcrumbs" aria-label="Breadcrumb">
            <ol className="header-breadcrumb-list">
              {breadcrumbs.map((crumb, index) => (
                <li key={crumb.path ?? index} className="header-breadcrumb-item">
                  {index > 0 && (
                    <span className="header-breadcrumb-separator" aria-hidden="true">
                      /
                    </span>
                  )}
                  {crumb.path ? (
                    <a href={crumb.path} className="header-breadcrumb-link">
                      {crumb.label}
                    </a>
                  ) : (
                    <span className="header-breadcrumb-current" aria-current="page">
                      {crumb.label}
                    </span>
                  )}
                </li>
              ))}
            </ol>
          </nav>
        )}
      </div>
      <div className="header-right">
        {actions && <div className="header-actions">{actions}</div>}
      </div>
    </header>
  );
}
