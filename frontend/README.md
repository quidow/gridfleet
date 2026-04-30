# GridFleet Frontend

React 19 + TypeScript + Vite frontend for the GridFleet operator UI.

## Getting Started

Install dependencies:

```bash
npm install
```

Start the local dev server:

```bash
npm run dev
```

The frontend expects the backend API to be available separately. In local development, Vite proxies API requests to the configured backend target.

## Quality Checks

Run ESLint:

```bash
npm run lint
```

Create a production build:

```bash
npm run build
```

Run Playwright end-to-end tests:

```bash
npm run test:e2e
```

## Main Areas

- `src/pages/`: route-level screens
- `src/components/`: shared UI and workflow components
- `src/hooks/`: React Query hooks and page controllers
- `src/api/`: backend API clients
- `e2e/`: Playwright coverage
