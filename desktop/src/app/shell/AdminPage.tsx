import { PlaceholderPage } from "./PlaceholderPage";
import { AdminIcon } from "./icons";

export function AdminPage() {
  return (
    <PlaceholderPage
      title="Administration"
      description="System configuration, user management, tenant settings, and integration management."
      icon={<AdminIcon />}
    />
  );
}
