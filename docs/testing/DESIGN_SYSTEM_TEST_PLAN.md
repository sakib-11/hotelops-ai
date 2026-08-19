# Comprehensive Test Plan: Design System Integration & Routing Validation

## Project Context
**Project:** HotelOps AI  
**Component:** Pastel Design System (Task 40.3)  
**Target:** Tauri + React 19 + TypeScript Desktop Application  
**Date:** 2026-08-09  

---

## 1. Scope & Objectives

### 1.1 In-Scope
- Design system component library (`src/components/ui/`)
- Design tokens (`src/design-system/`)
- Application shell routing (`src/app/shell/`)
- Integration with existing Tauri/React/TypeScript stack
- Cross-component data flow and prop validation

### 1.2 Out-of-Scope
- Backend API integration (Task 5, 20)
- Authentication flows (Task 41+)
- WebSocket real-time features (Task 20)
- CCTV/video processing features (Task 4+)

---

## 2. Test Architecture

### 2.1 Test Pyramid Distribution
```
                    ┌─────────────┐
                    │   E2E (5%)  │  Tauri app launch, full routing cycle
                    ├─────────────┤
              ┌─────│Integration │  Component composition, context providers
              │     │  (25%)     │
              │     ├─────────────┤
              │     │   Unit     │  Individual components, hooks, utilities
              │     │  (70%)     │
              │     └─────────────┘
              ▼
```

### 2.2 Test Tools & Frameworks
| Layer | Tool | Configuration |
|-------|------|---------------|
| Unit | Vitest + React Testing Library | `vitest.config.ts` |
| Component | Storybook + Vitest | `.storybook/` |
| Integration | Vitest + MSW | `tests/integration/` |
| E2E | Playwright (Tauri mode) | `tests/e2e/` |
| Visual | Chromatic / Percy | CI pipeline |
| Accessibility | axe-core + Vitest | Integrated in unit tests |

---

## 3. Unit Test Plan (70%)

### 3.1 Design Token Tests
**Location:** `tests/unit/design-system/`

| Test Case | Description | Expected |
|-----------|-------------|----------|
| `tokens.color-palette.light-mode` | Verify all light mode CSS custom properties resolve | All `--color-*` vars defined |
| `tokens.color-palette.dark-mode` | Verify all dark mode CSS custom properties resolve | All `--color-*` vars defined |
| `tokens.spacing.scale` | Validate spacing scale consistency (4px base) | `xs=4, sm=8, md=12, lg=16, xl=24...` |
| `tokens.border-radius.values` | Verify radius tokens map to CSS | `small=4, medium=8, large=12, pill=9999` |
| `tokens.typography.scale` | Validate font sizes, weights, line heights | All tokens resolvable |
| `tokens.shadows.elevation` | Verify shadow tokens for elevation | `subtle < card < elevated < dropdown` |
| `tokens.semantic-colors.mapping` | Verify status→color mapping | success→green, error→red, etc. |
| `tokens.pastel-palette.accessibility` | Verify WCAG AA contrast for pastel tokens | All text/background combos pass |

### 3.2 Component Unit Tests
**Location:** `tests/unit/components/ui/`

#### Button Component
| Test Case | Description |
|-----------|-------------|
| `Button.renders-primary-variant` | Primary variant applies correct classes |
| `Button.renders-secondary-variant` | Secondary variant applies correct classes |
| `Button.renders-ghost-variant` | Ghost variant applies correct classes |
| `Button.renders-destructive-variant` | Destructive variant applies correct classes |
| `Button.all-sizes` | sm/md/lg render with correct height/padding |
| `Button.loading-state` | Shows spinner, disables interaction, sets aria-busy |
| `Button.disabled-state` | Prevents click, applies disabled styles, aria-disabled |
| `Button.icon-position-start` | Icon renders before text |
| `Button.icon-position-end` | Icon renders after text |
| `Button.full-width` | Applies 100% width |
| `Button.keyboard-focus` | Visible focus ring on Tab |
| `Button.click-handler` | Fires onClick when not disabled/loading |
| `IconButton.aria-label-required` | Warns if aria-label missing |
| `IconButton.all-sizes` | sm/md/lg render correct dimensions |

#### Card Component
| Test Case | Description |
|-----------|-------------|
| `Card.default-variant` | Border + subtle shadow |
| `Card.elevated-variant` | No border + elevated shadow |
| `Card.outlined-variant` | Border only |
| `Card.padding-variants` | none/sm/md/lg apply correct padding |
| `Card.hoverable` | Shadow elevation on hover |
| `Card.Header` | Renders title, description, action slot |
| `Card.Content` | Renders children |
| `Card.Footer` | Renders children, right-aligned |
| `Card.compound-components` | Header/Content/Footer compose correctly |

#### Badge & StatusBadge
| Test Case | Description |
|-----------|-------------|
| `Badge.all-variants` | default/primary/secondary/success/warning/error/info |
| `Badge.sizes` | sm/md render correct height |
| `Badge.dot-indicator` | Shows colored dot when `dot=true` |
| `Badge.removable` | Shows remove button, fires onRemove |
| `StatusBadge.all-statuses` | Maps 10 statuses to correct variants |
| `StatusBadge.label-fallback` | Uses status name when label not provided |
| `StatusBadge.showDot` | Dot visible by default, hidden when false |

#### Form Components (Input, Select, Textarea, Checkbox, Toggle)
| Test Case | Description |
|-----------|-------------|
| `Input.label-association` | label `htmlFor` matches input `id` |
| `Input.helper-text` | Described by helper via `aria-describedby` |
| `Input.error-state` | Border color, `aria-invalid=true`, error announced |
| `Input.leading-icon` | Icon renders in correct position |
| `Input.trailing-icon` | Icon renders in correct position |
| `Input.disabled` | Disabled styles, `aria-disabled` |
| `Select.options` | Renders all options, placeholder handling |
| `Select.error-state` | Error styling + announcement |
| `Textarea.rows` | Respects `rows` prop |
| `Checkbox.label-click` | Clicking label toggles checkbox |
| `Checkbox.indeterminate` | Shows horizontal line |
| `Toggle.sizes` | sm/md/lg track/thumb dimensions |
| `Toggle.keyboard` | Space/Enter toggles, focus visible |
| `Toggle.error-state` | Track border error color |

#### Feedback Components (Tooltip, LoadingState, Skeleton, EmptyState, ErrorState)
| Test Case | Description |
|-----------|-------------|
| `Tooltip.placement` | top/bottom/left/right position correctly |
| `Tooltip.delay` | Respects delay prop |
| `Tooltip.keyboard-dismiss` | Escape hides tooltip |
| `Tooltip.focus-trigger` | Shows on focus |
| `LoadingState.variants` | spinner/dots/pulse render |
| `LoadingState.fullscreen` | Overlay + backdrop + centered |
| `Skeleton.variants` | text/circular/rectangular |
| `Skeleton.multi-line` | Renders N lines, last line 60% width |
| `Skeleton.animation` | Shimmer animation class applied |
| `EmptyState.default-icon` | Shows default icon when none provided |
| `EmptyState.action-slot` | Renders action children |
| `ErrorState.retry-button` | Fires onRetry with primary button |
| `ErrorState.dismissible` | Shows dismiss button, fires onDismiss |
| `ErrorState.error-code` | Displays code in monospace |

#### Layout Components (PageHeader, NavigationItem)
| Test Case | Description |
|-----------|-------------|
| `PageHeader.title` | Renders title with correct typography |
| `PageHeader.description` | Renders description muted |
| `PageHeader.icon` | Icon in colored circle |
| `PageHeader.breadcrumbs` | Renders links + separators + current |
| `PageHeader.action-slot` | Renders action children right-aligned |
| `PageHeader.compact` | Reduced padding, smaller title |
| `NavigationItem.active` | Brand background + indicator bar |
| `NavigationItem.badge` | Renders badge with count |
| `NavigationItem.disabled` | Opacity 50%, no hover, not clickable |
| `NavigationItem.compact` | Smaller padding, label size |

### 3.3 Hook & Utility Tests
**Location:** `tests/unit/hooks/`, `tests/unit/utils/`

| Test Case | Description |
|-----------|-------------|
| `useDesignTokens` | Returns token values, updates on theme change |
| `useMediaQuery` | Matches breakpoints correctly |
| `classNames utility` | Joins classes, filters falsy values |
| `formatError` | Formats error messages consistently |

---

## 4. Integration Test Plan (25%)

### 4.1 Component Composition Tests
**Location:** `tests/integration/components/`

| Test Case | Description |
|-----------|-------------|
| `Card.with-Button-in-Footer` | Button in Card.Footer renders + handles click |
| `Card.with-Badge-in-Header` | StatusBadge in Card.Header |
| `PageHeader.with-NavigationItem-breadcrumbs` | NavigationItems as breadcrumb links |
| `Tooltip.on-Button` | Tooltip wraps Button, shows on hover |
| `LoadingState.over-Card` | Fullscreen loading over Card content |
| `Skeleton.in-Card-grid` | Multiple Skeletons in Card grid layout |
| `Form-with-Input-Select-Checkbox` | Complete form composition |
| `ErrorState.with-retry-Loading` | Retry triggers LoadingState then resolves |

### 4.2 Design Token Integration
| Test Case | Description |
|-----------|-------------|
| `Tokens.apply-to-all-components` | All components consume CSS variables |
| `Theme-switch.light-to-dark` | All components update on `prefers-color-scheme` change |
| `Theme-switch.manual-toggle` | Manual theme toggle updates all components |
| `Token.override-per-component` | Component can override specific tokens |

### 4.3 Routing Integration
**Location:** `tests/integration/routing/`

| Test Case | Description |
|-----------|-------------|
| `AppShell.renders-all-routes` | All 7 routes mount without error |
| `Route.navigation-updates-sidebar` | Sidebar active state matches route |
| `Route.navigation-updates-header` | PageHeader title/description matches route |
| `Route.breadcrumbs` | Breadcrumbs reflect route hierarchy |
| `Route.404-redirect` | Unknown routes redirect to `/overview` |
| `Route.root-redirect` | `/` redirects to `/overview` |
| `Sidebar.collapse-preserves-route` | Collapsed sidebar still navigates |
| `Sidebar.keyboard-navigation` | Arrow keys navigate, Enter activates |

### 4.4 Context Provider Integration
| Test Case | Description |
|-----------|-------------|
| `DesignSystemProvider.wraps-app` | Tokens available globally |
| `RouterProvider.nested-routes` | Outlet renders child routes |
| `React.StrictMode.no-double-effects` | No double mounts in dev |

---

## 5. E2E Test Plan (5%)

### 5.1 Tauri Application Tests
**Location:** `tests/e2e/tauri/`

| Test Case | Description |
|-----------|-------------|
| `App.launches-successfully` | Tauri window opens, React mounts |
| `App.window-dimensions` | 1280x800 default, min 1024x600 |
| `App.responsive-resize` | Layout adapts on resize |
| `App.full-navigation-cycle` | Visit all 7 routes via sidebar |
| `App.sidebar-toggle` | Collapse/expand via button + trigger |
| `App.keyboard-only-navigation` | Tab through all interactive elements |
| `App.persist-state-on-reload` | Route persists on refresh (if implemented) |
| `App.dev-tools-accessible` | F12 opens devtools in dev mode |
| `App.production-build` | Packaged app launches, all routes work |

### 5.2 Visual Regression Tests
**Location:** `tests/e2e/visual/`

| Test Case | Description |
|-----------|-------------|
| `Light-theme.all-pages` | Screenshot comparison all routes |
| `Dark-theme.all-pages` | Screenshot comparison all routes |
| `Component-states` | Hover, focus, active, disabled states |
| `Responsive-breakpoints` | 768px, 1024px, 1280px viewports |

---

## 6. Accessibility Test Plan

### 6.1 Automated (Unit/Integration)
| Tool | Coverage |
|------|----------|
| `axe-core` | All components, all routes |
| `eslint-plugin-jsx-a11y` | Lint-time checks |
| `storybook-addon-a11y` | Storybook stories |

### 6.2 Manual Checklist
- [ ] Tab order logical across all pages
- [ ] Focus visible on all interactive elements
- [ ] ARIA labels/descriptions present
- [ ] Color contrast ≥ 4.5:1 (text), ≥ 3:1 (UI)
- [ ] No color-only information
- [ ] Screen reader announces route changes
- [ ] Keyboard shortcuts documented
- [ ] Reduced motion respected

---

## 7. Performance Test Plan

### 7.1 Bundle Analysis
| Metric | Target |
|--------|--------|
| Initial JS bundle | < 300 kB gzipped |
| CSS bundle | < 50 kB gzipped |
| Time to Interactive | < 2s (dev), < 1s (prod) |
| Component mount time | < 50ms per component |

### 7.2 Runtime Performance
| Test | Target |
|------|--------|
| Route transition | < 100ms |
| Sidebar toggle animation | 200ms, 60fps |
| Tooltip show/hide | < 50ms |
| Skeleton shimmer | 60fps, no jank |

---

## 8. Data Flow Validation

### 8.1 Prop Drilling Verification
```
AppShell
  ├── Sidebar (navItems, activePath, onToggle, user)
  │   └── NavLink (to, isActive, onClick)
  └── MainArea
      └── PageHeader (title, description, breadcrumbs, action)
          └── Outlet (child routes)
              └── PlaceholderPage (title, description, icon)
```

**Validation:** Each prop type matches interface, no `any` types, required props enforced.

### 8.2 State Management
| State | Location | Persistence |
|-------|----------|-------------|
| `isSidebarCollapsed` | AppShell (useState) | Session only |
| `activePath` | AppShell (derived from location) | URL |
| `user` | AppShell (static for now) | — |
| `theme` | CSS `prefers-color-scheme` | OS preference |

---

## 9. Regression Prevention

### 9.1 Pre-commit Hooks
```yaml
# .pre-commit-config.yaml additions
- repo: local
  hooks:
    - id: design-system-unit-tests
      name: Design System Unit Tests
      entry: npm run test:unit -- --run
      language: system
      types: [typescript, tsx]
      files: ^src/(components|design-system)/
```

### 9.2 CI Pipeline Gates
| Stage | Required | Timeout |
|-------|----------|---------|
| TypeScript | ✅ | 3 min |
| Lint | ✅ | 2 min |
| Format | ✅ | 1 min |
| Unit Tests | ✅ | 5 min |
| Integration Tests | ✅ | 5 min |
| Build | ✅ | 3 min |
| E2E (smoke) | ✅ | 5 min |

### 9.3 Visual Regression Baseline
- Baseline images committed to `tests/visual-baselines/`
- PRs failing visual diff require explicit approval
- Update baseline via `npm run test:visual:update`

---

## 10. Test Data & Fixtures

### 10.1 Component Fixtures
**Location:** `tests/fixtures/components/`

```typescript
// Navigation fixtures
export const mockNavItems = [
  { id: 'overview', label: 'Overview', icon: HomeIcon, path: '/overview' },
  // ... all 7 items
];

export const mockUser = { name: 'Test User', email: 'test@hotelops.ai' };

// Route fixtures
export const routeTitles = {
  '/overview': 'Overview',
  '/live': 'Live Monitoring',
  // ...
};
```

### 10.2 Mock Providers
```tsx
// test-utils/providers.tsx
export function renderWithProviders(ui: ReactElement) {
  return render(ui, {
    wrapper: ({ children }) => (
      <BrowserRouter>
        <DesignSystemProvider>{children}</DesignSystemProvider>
      </BrowserRouter>
    ),
  });
}
```

---

## 11. Execution Strategy

### 11.1 Local Development
```bash
# Run all unit tests in watch mode
npm run test:unit -- --watch

# Run integration tests
npm run test:integration

# Run visual regression
npm run test:visual

# Full test suite
npm run test:all
```

### 11.2 CI Execution Order
1. Parallel: TypeScript + Lint + Format
2. Parallel: Unit Tests + Integration Tests
3. Sequential: Build → E2E Smoke → Visual Regression
4. Notify on failure with artifact links

---

## 12. Success Criteria

| Criterion | Threshold |
|-----------|-----------|
| Unit test coverage (components) | ≥ 90% |
| Unit test coverage (hooks/utils) | ≥ 80% |
| Integration test pass rate | 100% |
| E2E smoke test pass rate | 100% |
| Visual regression diff | 0 (baseline match) |
| Accessibility violations | 0 (automated) |
| Bundle size increase | ≤ 10% vs Task 40.2 |
| Route navigation time | < 100ms |

---

## 13. Risk Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Tauri E2E flakiness | Medium | High | Run 3x, require 2/3 pass |
| Visual diff false positives | Medium | Medium | Threshold tuning, manual review |
| Theme switching not tested | Low | Medium | Add manual theme toggle in dev |
| Mobile breakpoint not applicable | N/A | N/A | Desktop-only, skip mobile tests |

---

## 14. Deliverables

1. **Test Implementation** - All test files in `tests/`
2. **CI Configuration** - Updated `.github/workflows/ci.yml`
3. **Test Utilities** - `tests/utils/`, `tests/fixtures/`
4. **Documentation** - `docs/testing/DESIGN_SYSTEM_TESTING.md`
5. **Baseline Images** - `tests/visual-baselines/`

---

## 15. Timeline Estimate

| Phase | Effort | Dependencies |
|-------|--------|--------------|
| Unit test infrastructure | 2 days | Vitest config |
| Component unit tests | 5 days | Component completion |
| Integration tests | 3 days | Unit tests passing |
| E2E Tauri setup | 2 days | Tauri build working |
| Visual regression | 1 day | Baseline approval |
| CI integration | 1 day | All tests passing |
| **Total** | **14 days** | — |

---

**Approval Required:** This test plan must be reviewed and approved before implementation begins.

**Next Steps:** 
1. Set up Vitest + React Testing Library in desktop app
2. Create test utilities and fixtures
3. Implement unit tests for highest-priority components (Button, Card, Badge)
4. Integrate with CI pipeline