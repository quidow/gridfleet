# Reference

This directory is for lookup-style documentation that describes the shipped contract without retelling workflows.

Use `docs/reference/` for material such as:

- API route and payload reference
- settings keys, defaults, and operational meaning
- events and webhooks
- glossary and core data-model terms
- testing-surface support expectations

Reference pages should optimize for exactness and fast lookup. Put task narrative in `docs/guides/` instead.

## Current Reference Set

- `environment.md`
  - Backend core env vars, settings-registry env fallbacks, agent process env vars, and installer-only helper variables.
- `api.md`
  - Supported `/api` routes grouped by domain, with the main inputs and response shapes.
- `settings.md`
  - Registry-backed settings keys, defaults, validation, env fallbacks, and operational meaning.
- `events-and-webhooks.md`
  - SSE, notifications, webhook routes, emitted event names, and event-envelope shape.
- `glossary.md`
  - Core fleet terms used across the product contract.
- `testkit.md`
  - Supported Python testkit package, imports, plugin path, and examples.
- `capabilities.md`
  - Generated Appium capability contract by platform and device type.
