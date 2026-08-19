import { PlaceholderPage } from "./PlaceholderPage";
import { RecordingsIcon } from "./icons";

export function RecordingsPage() {
  return (
    <PlaceholderPage
      title="Recordings"
      description="Browse, search, and manage recorded video footage with advanced filtering and playback controls."
      icon={<RecordingsIcon />}
    />
  );
}
