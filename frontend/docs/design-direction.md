# Frontend Design Direction

**Direction:** Precision-enterprise — tight, monochromatic surfaces with one teal accent, tabular-numerics, and 160–240ms motion that confirms state changes without theatrics.

## Why this direction

The fleet operator opens this app dozens of times a day. The visual identity has to read as instrument-grade: dense, legible, predictable. We get distinctiveness from a committed accent (teal, not default-indigo), a small motion vocabulary, a numeric treatment, and an inline app mark — not from layout reinvention or display-font heroics. Tokens stay small. Pages stay templated.

## Committed elements

- **Accent:** `--color-accent` is teal (`#0d9488` light / `#5eead4` dark). Used for brand surfaces, active nav row, focus rings, and call-to-action hover/border. Never used for status meaning — `info` blue, `success` green, `warning` amber, `danger` red carry status.
- **Motion vocabulary:** `--duration-{instant,fast,base,slow}` (80 / 160 / 240 / 360ms) paired with `--ease-standard` (and `--ease-emphasized` for rare overshoot moments). Cold-load entrance uses `.fade-in-stagger` (240ms per child, 80ms stagger). Hover affordances use `.hover-lift` (160ms).
- **Heading scale:** Page titles use `.heading-page`, card section titles use `.heading-section`, and nested section titles use `.heading-subsection`. Headings use IBM Plex Sans Variable, bundled through Fontsource under OFL-1.1, with Inter as fallback.
- **Numeric treatment:** Big metrics use `.metric-numeric` — tabular-nums + slashed-zero on top of the existing `font-mono`.
- **App mark:** Inline SVG (three stacked horizontal bars) tinted `--color-accent`, in the sidebar header next to the wordmark. Acts as the only logo asset; no external file.
- **Dashboard card hierarchy:** Dashboard sections use `DashboardCard`. Exactly one section should be `primary` for the main KPI surface; supporting sections use `secondary` with lighter chrome and the same header rhythm.
- **List page subheaders:** List pages use `ListPageSubheader` between filters and tables for label + count rhythm. Keep primary page actions in `PageHeader.actions`; subheaders should not become bordered separator rows.

## Do

- Use `.hover-lift` on clickable stat cards, summary cards, and any tile that navigates somewhere on click.
- Use teal for the active sidebar nav row, focus rings (`outline-accent-ring`), and primary-button text/border accents — but never for status badges.
- Apply `.metric-numeric` to every big number an operator scans (Available / Busy / Offline counts, pass rate, utilization, watchlist count, summary pill values).
- Use `.heading-page`, `.heading-section`, and `.heading-subsection` instead of raw heading size/weight strings on shared app surfaces.
- Use `ListPageSubheader` for list labels and result counts between filters and tables.
- Keep Dashboard section chrome centralized in `DashboardCard`; use `SectionSkeleton` for cold-load placeholders that match the final section shape.
- Use token utilities only in page and section code. Allowed color families are `surface`, `border`, `text`, `accent`, `success`, `warning`, `danger`, `info`, `neutral`, `sidebar`, `device-type`, `platform`, and `lifecycle`.

## Don't

- Don't animate longer than `--duration-slow` (360ms). If something needs more time, the affordance is wrong, not the duration.
- Don't reach for teal to mean "good" or "running" — that's status territory and belongs to the `success` / `info` palettes.
- Don't introduce spring/bounce easing in default flows. Use `--ease-standard`. `--ease-emphasized` is reserved for one-off moments where overshoot communicates a real state change.
- Don't use raw numeric Tailwind color utilities such as `text-blue-*`, `bg-amber-*`, `border-gray-*`, or `ring-red-*` outside `tokens.css`. Add or reuse a token first.

## Honoring `prefers-reduced-motion`

`tokens.css` collapses `.fade-in-stagger` to `opacity: 1; transform: none` and removes the `.hover-lift` translate when `prefers-reduced-motion: reduce` is set. Shadow-tick on hover stays (it's paint, not motion). Any new animation added later must follow the same pattern.

## DefinitionList orientation policy

- **Dense / right-aligned value (`layout="justified"`)** for diagnostic panels where the label + value sit on one row: Device Info (post-migration), Host Info, Run Info, Timestamps, Hardware Telemetry, Session Viability.
- **Stacked (`layout="stacked"`)** for large read-only form-like blocks where labels introduce paragraphs of value: Appium Capabilities, long device descriptions.
- Never mix orientations inside a single card.
