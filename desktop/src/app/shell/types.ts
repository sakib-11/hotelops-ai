export interface NavItem {
  id: string;
  label: string;
  icon: React.ReactNode;
  path: string;
}

export interface UserPlaceholder {
  name: string;
  email: string;
  avatar?: string;
}

export interface SidebarProps {
  isCollapsed: boolean;
  onToggle: () => void;
  navItems: NavItem[];
  activePath: string;
  onNavigate: (path: string) => void;
  user?: UserPlaceholder;
}

export interface HeaderProps {
  title: string;
  breadcrumbs?: { label: string; path?: string }[];
  actions?: React.ReactNode;
}

export interface MainAreaProps {
  children: React.ReactNode;
  header: React.ReactNode;
}

export interface AppShellProps {
  children: React.ReactNode;
}
