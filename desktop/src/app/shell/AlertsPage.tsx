import { PlaceholderPage } from "./PlaceholderPage";
import { AlertsIcon } from "./icons";

export function AlertsPage() {
  return (
    <PlaceholderPage
      title="Alerts"
      description="Real-time alert management with acknowledgment workflows, escalation policies, and audit trails."
      icon={<AlertsIcon />}
    />
  );
}
