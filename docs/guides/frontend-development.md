# Frontend Development Guide

GridFleet's frontend acts as a high-density operator workspace. It runs on **React + TypeScript + Vite + TailwindCSS**.

## App Architecture

```text
frontend/src/
├── api/          # Strongly typed Axios clients matching backend routes
├── components/   # Shared UI primitives (DataTable, Badge, Button, FilterBar)
├── context/      # Global React providers (auth, theme)
├── hooks/        # react-query data hooks with defined polling intervals
├── pages/        # Routable top-level views (Dashboard, Devices, Hosts)
└── types/        # TypeScript interfaces matching backend Pydantic schemas
```

- **`src/api/`**: Strongly typed Axios clients. All external communication routes through here.
- **`src/components/`**: Houses all shared UI primitives. Ensure you reuse existing primitives (like `Badge`, `DataTable`, `FilterBar`) before building new ones.
- **`src/hooks/`**: Data fetching and mutation hooks using `react-query`. Most lists and item details use predefined polling intervals (e.g., 5-15s) to guarantee real-time reflection of device capability, state, or heartbeat.
- **`src/pages/`**: Routable views built by combining layout frames with atomic UI components.
- **`src/types/`**: The entire frontend uses TypeScript. Types heavily mirror the Pydantic schemas in the backend and are structurally chunked (e.g., `devices.ts`, `hosts.ts`).

## Design Patterns & Standards

During Phase 109 and subsequent updates, the frontend styling was unified under a standard design token set. If developing new components, strictly adhere to these practices:

### 1. Spacing and Density
- We utilize standard Tailwind spacing scales (`p-4`, `p-6`). Never hardcode pixel values when a Tailwind class is available.
- Keep the density appropriate for an "Operator Console": compact tables, clear hierarchical alerts, and structured data layouts.

### 2. Form Controls
- Use native HTML elements standardly wrapped. E.g., for Dates use `type="date"` for native browser compatibility.
- Prefer unified payloads for complex search filters and entity creation.

### 3. Error states & Fallbacks
- **`FetchError`**: Always wrap queries that may fail with the standard `<FetchError error={error} retry={refetch} />` component.
- Loading states should ideally prevent jarring layout shifts (e.g., use block skeleton loaders if fetching large data segments).

## Development Workflow

To build and run locally:

```bash
cd frontend
npm ci
npm run dev
```

### Static Analysis
Ensure code passes existing linting rules before committing:
```bash
npm run lint         # Standard ESLint
npx tsc --noEmit     # TypeScript type-check
```

### E2E Tests
Browser-scoped tests run via Playwright. Note that **backend and frontend dev servers must be active** natively or via docker.
```bash
npm run test:e2e
```
