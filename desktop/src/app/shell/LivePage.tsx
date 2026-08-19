import { PlaceholderPage } from "./PlaceholderPage";
import { LiveIcon } from "./icons";

export function LivePage() {
  return (
    <PlaceholderPage
      title="Live Monitoring"
      description="Real-time video feeds and live analytics from all connected cameras across venues."
      icon={<LiveIcon />}
    />
  );
}
