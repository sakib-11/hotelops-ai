import { PlaceholderPage } from "./PlaceholderPage";
import { HomeIcon } from "./icons";
import { OccupancyStatusCard } from "@/features/operational/OccupancyStatusCard";
import { VERTICAL_SLICE_DEMO } from "@/features/operational/verticalSliceDemo";

export function OverviewPage() {
  return (
    <PlaceholderPage
      title="Overview"
      description="Real-time operational dashboard showing key metrics, venue status, and system health at a glance."
      icon={<HomeIcon />}
    >
      {/* Task 18.13 — the minimal Tauri card proving the complete vertical
          slice. It reads ONLY the authorized FastAPI retrieval surface;
          the ids are the documented deterministic fixture references. */}
      <OccupancyStatusCard
        eventId={VERTICAL_SLICE_DEMO.eventId}
        factId={VERTICAL_SLICE_DEMO.factId}
      />
    </PlaceholderPage>
  );
}
