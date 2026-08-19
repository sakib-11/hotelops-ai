import type { MainAreaProps } from "./types";
import "./MainArea.css";

export function MainArea({ children, header }: MainAreaProps) {
  return (
    <main className="main-area" role="main">
      <div className="main-area-header">{header}</div>
      <div className="main-area-content">{children}</div>
    </main>
  );
}
