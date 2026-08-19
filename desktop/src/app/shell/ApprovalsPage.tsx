import { PlaceholderPage } from "./PlaceholderPage";
import { ApprovalsIcon } from "./icons";

export function ApprovalsPage() {
  return (
    <PlaceholderPage
      title="Approvals"
      description="Review and approve AI-generated recommendations, evidence packages, and operational decisions."
      icon={<ApprovalsIcon />}
    />
  );
}
