# ADR-001: Desktop Application Stack

## Status

Accepted

## Context

HotelOps AI requires a desktop application for hotel operations staff. The desktop application must:

1. **Run as a native desktop application** — operations staff need a dedicated application, not a browser tab
2. **Display real-time dashboards** — live camera feeds, occupancy data, operational events via WebSocket
3. **Support rich visualizations** — floor plans, heatmaps, charts (including potential future 3D visualization)
4. **Access controlled native capabilities** — potential future USB device access, local file system for exports, hardware-accelerated video decoding
5. **Be maintainable by a single development team** — technology must align with team skills and not introduce excessive complexity
6. **Communicate with a remote backend** — desktop is a client; all operational data lives on the server

The team considered three approaches for the desktop application.

## Decision

Use **Tauri v2** as the desktop application shell, with **React** for UI and **TypeScript** for type safety.

The desktop stack will be:
- **Tauri v2** — native desktop shell (Rust backend)
- **React 19** — UI framework
- **TypeScript 5** — type-safe frontend development
- **Vite 6** — build tool and dev server

## Rationale

- **React ecosystem** provides mature libraries for dashboards, charts, state management, and real-time communication, which are core to HotelOps AI's UI requirements
- **TypeScript** catches UI bugs at compile time, critical for a production operations tool
- **Tauri** provides a small, controlled native boundary (Rust) while keeping most code in TypeScript/React
- **Desktop packaging** is built-in with Tauri (Windows, macOS, Linux installers)
- **Security model** follows least privilege — Tauri's capability system allows explicit opt-in to native features
- **Performance** — Tauri's WebView-based approach means near-native performance for UI rendering
- **Bundle size** — significantly smaller than Electron (no Chromium bundle)

## Alternatives Considered

### Alternative 1: Electron + React + TypeScript

- **Description**: Electron provides a full Chromium runtime bundled with the application
- **Pros**: Mature ecosystem, extensive documentation, well-understood by many developers
- **Cons**: Large bundle size (~150MB+), higher memory usage, full Chromium in every app
- **Why not chosen**: Bundle size and memory overhead are significant for an operations tool that runs continuously. Tauri provides equivalent React/TypeScript support with a fraction of the resource usage. The larger attack surface of bundling a full browser is not justified for this application's needs.

### Alternative 2: Flutter Desktop

- **Description**: Flutter provides a cross-platform desktop framework using Dart
- **Pros**: Single codebase for potential future mobile app, fast rendering, custom UI components
- **Cons**: Significant team learning curve (Dart, Flutter widget model), less mature desktop ecosystem, JSON/REST serialization requires manual work, weaker TypeScript/jQuery-type ecosystem for realtime dashboards
- **Why not chosen**: The team's existing skills are in TypeScript/React. Flutter would require learning Dart and a completely different UI paradigm. The React ecosystem's strengths in data visualization and realtime UI align better with HotelOps AI's requirements. A potential future mobile app does not justify the technology risk for the initial desktop release.

## Consequences

### Positive
- **Rich UI ecosystem**: React's library ecosystem provides excellent support for dashboards, charts, and real-time data visualization
- **Type safety**: TypeScript catches UI bugs at compile time
- **Small bundle**: Tauri produces significantly smaller desktop applications than Electron
- **Controlled native surface**: Rust native code is limited to what Tauri explicitly exposes via capabilities
- **Security by design**: Tauri's CSP and capability model enforce least privilege
- **Modern tooling**: Vite provides fast dev server and optimized builds

### Negative
- **WebView dependency**: Tauri uses the system WebView, which means platform-specific behavior must be tested on each target OS
- **Rust knowledge required**: While the Rust surface is small, understanding Tauri internals requires Rust knowledge
- **Platform-specific testing**: Desktop packaging requires validation on each target platform (Windows, macOS, Linux)
- **Tauri ecosystem maturity**: Tauri v2 is newer than Electron, so fewer third-party plugins and community resources are available
- **No built-in Chromium**: Unlike Electron, Tauri cannot guarantee consistent rendering across platforms

## Security Impact

- Tauri's capability system allows granular permission control — the desktop starts with `core:default` only
- CSP is configured to restrict connections to the backend API only
- No filesystem, shell, or process execution capabilities are granted by default
- Native capabilities will be added only through approved ADRs

## Operational Impact

- Desktop builds must be produced for each target platform
- CI pipeline needs platform-specific build runners for automated builds
- Distribution strategy (installer, auto-update) to be determined in a future ADR

## Rollback / Migration

### Rollback Plan
- If Tauri proves unsuitable, the React/TypeScript UI can be extracted and reused with Electron or a web-based approach
- The backend API remains unchanged regardless of desktop technology
- Transitioning from Tauri to Electron would require: replacing `src-tauri/` with Electron shell, adapting Tauri API calls to Electron IPC

### Migration Plan
- Not applicable — this is an initial decision, not a migration

## References

- [Product Charter](../product/product-charter.md) — Architecture Principle #5: Desktop-Backend Separation
- [Architecture README](../README.md) — Module boundaries
- ADR-000 — ADR template

---

## Metadata

| Field | Value |
|-------|-------|
| **Date** | 2026-07-28 |
| **Author(s)** | Engineering Team |
| **Last Modified** | 2026-07-28 |
