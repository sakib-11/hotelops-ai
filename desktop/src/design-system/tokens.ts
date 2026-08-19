export const colors = {
  background: {
    primary: "#fafafa",
    secondary: "#f4f4f5",
    tertiary: "#e4e4e7",
    inverse: "#0e0e10",
  },
  surface: {
    primary: "#ffffff",
    secondary: "#f4f4f5",
    tertiary: "#e4e4e7",
    hover: "#e4e4e7",
    active: "#d4d4d8",
    elevated: "#ffffff",
    muted: "#f4f4f5",
  },
  border: {
    primary: "#d4d4d8",
    secondary: "#a1a1aa",
    focus: "#8b5cf6",
    subtle: "#e4e4e7",
  },
  text: {
    primary: "#18181b",
    secondary: "#52525b",
    tertiary: "#71717a",
    disabled: "#a1a1aa",
    inverse: "#fafafa",
    link: "#8b5cf6",
    linkHover: "#7c3aed",
  },
  action: {
    primary: "#8b5cf6",
    primaryHover: "#7c3aed",
    primaryActive: "#6d28d9",
    primaryDisabled: "#c4b5fd",
    secondary: "#f4f4f5",
    secondaryHover: "#e4e4e7",
    secondaryActive: "#d4d4d8",
    secondaryDisabled: "#f4f4f5",
    destructive: "#ef4444",
    destructiveHover: "#dc2626",
    destructiveActive: "#b91c1c",
    destructiveDisabled: "#fecaca",
    ghost: "transparent",
    ghostHover: "#f4f4f5",
    ghostActive: "#e4e4e7",
  },
  status: {
    success: "#10b981",
    successHover: "#059669",
    successLight: "#d1fae5",
    successText: "#065f46",
    warning: "#f59e0b",
    warningHover: "#d97706",
    warningLight: "#fef3c7",
    warningText: "#92400e",
    error: "#ef4444",
    errorHover: "#dc2626",
    errorLight: "#fee2e2",
    errorText: "#991b1b",
    info: "#3b82f6",
    infoHover: "#2563eb",
    infoLight: "#dbeafe",
    infoText: "#1e40af",
  },
  brand: {
    primary: "#8b5cf6",
    primaryHover: "#7c3aed",
    primaryActive: "#6d28d9",
    primaryLight: "#ede9fe",
    primaryText: "#4c1d95",
    secondary: "#06b6d4",
    secondaryHover: "#0891b2",
    secondaryLight: "#cffafe",
    secondaryText: "#164e63",
  },
  pastel: {
    lavender: "#e9d5ff",
    lavenderLight: "#f5f0ff",
    lavenderText: "#6b21a8",
    mint: "#a7f3d0",
    mintLight: "#ecfdf5",
    mintText: "#065f46",
    peach: "#fcd6a5",
    peachLight: "#fffbeb",
    peachText: "#92400e",
    rose: "#fda4af",
    roseLight: "#fff1f2",
    roseText: "#9f1239",
    sky: "#bae6fd",
    skyLight: "#f0f9ff",
    skyText: "#1e40af",
    lilac: "#ddd6fe",
    lilacLight: "#faf5ff",
    lilacText: "#5b21b6",
  },
} as const;

export const spacing = {
  xs: "4px",
  sm: "8px",
  md: "12px",
  lg: "16px",
  xl: "24px",
  "2xl": "32px",
  "3xl": "48px",
  "4xl": "64px",
} as const;

export const borderRadius = {
  small: "4px",
  medium: "8px",
  large: "12px",
  pill: "9999px",
} as const;

export const typography = {
  fontFamily: {
    sans: '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    mono: '"JetBrains Mono", "Fira Code", monospace',
  },
  fontSize: {
    caption: "11px",
    label: "12px",
    body: "14px",
    bodyLarge: "16px",
    title: "18px",
    heading: "22px",
    display: "28px",
    displayLarge: "36px",
  },
  fontWeight: {
    normal: "400",
    medium: "500",
    semibold: "600",
    bold: "700",
  },
  lineHeight: {
    tight: "1.25",
    normal: "1.5",
    relaxed: "1.625",
  },
  letterSpacing: {
    tight: "-0.02em",
    normal: "0",
    wide: "0.02em",
  },
} as const;

export const shadows = {
  subtle: "0 1px 2px 0 rgb(0 0 0 / 0.03), 0 1px 3px 1px rgb(0 0 0 / 0.02)",
  card: "0 1px 3px 0 rgb(0 0 0 / 0.05), 0 4px 6px -2px rgb(0 0 0 / 0.03)",
  elevated: "0 4px 12px 0 rgb(0 0 0 / 0.05), 0 8px 16px -4px rgb(0 0 0 / 0.04)",
  dropdown: "0 8px 24px 0 rgb(0 0 0 / 0.08), 0 12px 32px -4px rgb(0 0 0 / 0.06)",
  focus: "0 0 0 3px rgb(139 92 246 / 0.3)",
} as const;

export const transitions = {
  fast: "120ms ease",
  normal: "200ms ease",
  slow: "300ms ease",
} as const;

export const zIndex = {
  base: 0,
  dropdown: 100,
  sticky: 200,
  modal: 300,
  popover: 400,
  tooltip: 500,
  toast: 600,
} as const;

export const breakpoints = {
  sm: "640px",
  md: "768px",
  lg: "1024px",
  xl: "1280px",
  "2xl": "1536px",
} as const;

export const sidebar = {
  width: {
    collapsed: "72px",
    expanded: "256px",
  },
  transition: "width 200ms ease",
} as const;

export const header = {
  height: "56px",
} as const;

export const layout = {
  maxContentWidth: "1280px",
  pagePadding: "24px",
  cardPadding: "20px",
} as const;

export type Colors = typeof colors;
export type Spacing = typeof spacing;
export type BorderRadius = typeof borderRadius;
export type Typography = typeof typography;
export type Shadows = typeof shadows;
export type Transitions = typeof transitions;
export type ZIndex = typeof zIndex;
export type Breakpoints = typeof breakpoints;
export type Sidebar = typeof sidebar;
export type Header = typeof header;
export type Layout = typeof layout;
