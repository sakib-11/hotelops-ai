import type { FC } from "react";

interface PageProps {
  title: string;
  description: string;
  icon: React.ReactNode;
  children?: React.ReactNode;
}

export const PlaceholderPage: FC<PageProps> = ({ title, description, icon, children }) => (
  <div className="placeholder-page">
    <div className="placeholder-page-header">
      <div className="placeholder-page-icon" aria-hidden="true">
        {icon}
      </div>
      <div className="placeholder-page-text">
        <h2 className="placeholder-page-title">{title}</h2>
        <p className="placeholder-page-description">{description}</p>
      </div>
    </div>
    {children && <div className="placeholder-page-content">{children}</div>}
    <div className="placeholder-page-notice">
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="10" />
        <path d="M12 16v-4" />
        <path d="M12 8h.01" />
      </svg>
      <span>This page is a placeholder for Task 40.2. Full implementation coming in Task 41+.</span>
    </div>
  </div>
);
