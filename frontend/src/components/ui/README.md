# UI Primitives — `components/ui/`

Shared building blocks introduced in Phase 106. Every primitive lives here; import from `./ui` (or the index barrel) rather than reimplementing inline.

---

## Badge

Visual pill label with semantic tone.

```tsx
<Badge tone="success" size="md" dot>online</Badge>
```

| Prop | Type | Default | Notes |
|------|------|---------|-------|
| `tone` | `'neutral' \| 'info' \| 'success' \| 'warning' \| 'danger'` | `'neutral'` | Maps to bg/text colors |
| `size` | `'sm' \| 'md'` | `'md'` | |
| `icon` | `ReactNode` | — | Leading icon |
| `dot` | `boolean` | `false` | Renders a colored dot before text |
| `className` | `string` | — | Escape hatch |
| `children` | `ReactNode` | required | |

---

## Button

Standard action button with variant and size options.

```tsx
<Button variant="primary" size="md" onClick={save}>Save</Button>
<Button variant="danger" loading>Deleting…</Button>
```

| Prop | Type | Default | Notes |
|------|------|---------|-------|
| `variant` | `'primary' \| 'secondary' \| 'danger' \| 'ghost'` | `'primary'` | |
| `size` | `'sm' \| 'md'` | `'md'` | |
| `loading` | `boolean` | `false` | Shows inline spinner, forces `disabled` |
| `leadingIcon` | `ReactNode` | — | |
| `trailingIcon` | `ReactNode` | — | |
| `fullWidth` | `boolean` | `false` | |

All other `HTMLButtonElement` attributes are forwarded. Defaults to `type="button"`.

---

## Card

Consistent white card with optional padding.

```tsx
<Card padding="lg"><p>Content</p></Card>
```

| Prop | Type | Default |
|------|------|---------|
| `padding` | `'none' \| 'sm' \| 'md' \| 'lg'` | `'md'` |
| `as` | `'div' \| 'section' \| 'article'` | `'div'` |
| `className` | `string` | — |

---

## ConfirmDialog

Wraps `Modal` with a cancel/confirm footer. Uses `Button` primitives.

```tsx
<ConfirmDialog
  isOpen={open}
  onClose={() => setOpen(false)}
  onConfirm={handleDelete}
  title="Delete device?"
  message="This cannot be undone."
  confirmLabel="Delete"
  variant="danger"
/>
```

| Prop | Type | Default |
|------|------|---------|
| `isOpen` | `boolean` | required |
| `onClose` | `() => void` | required |
| `onConfirm` | `() => void` | required |
| `title` | `string` | required |
| `message` | `string` | required |
| `confirmLabel` | `string` | `'Confirm'` |
| `variant` | `'danger' \| 'default'` | `'default'` |

---

## PageHeader

Shared page title row with optional subtitle, relative updated-at stamp, and summary slot.

```tsx
<PageHeader
  title="Dashboard"
  subtitle="Fleet status"
  updatedAt={Date.now()}
  summary={<SummaryPill tone="ok" label="DB" />}
  actions={<Button>Add Device</Button>}
/>
```

| Prop | Type | Default |
|------|------|---------|
| `title` | `ReactNode` | required |
| `subtitle` | `string` | — |
| `updatedAt` | `Date \| string \| number \| null` | — |
| `summary` | `ReactNode` | — |
| `actions` | `ReactNode` | — |

---

## SummaryPill

Display-only status pill with tone dot, label, and optional value.

```tsx
<SummaryPill tone="warn" label="Queued" value={2} />
```

| Prop | Type | Default |
|------|------|---------|
| `tone` | `'ok' \| 'warn' \| 'error' \| 'neutral'` | required |
| `label` | `string` | required |
| `value` | `ReactNode` | — |

---

## DataTable

Generic typed table with sort, selection, row actions, loading, and empty-state support.

```tsx
<DataTable<Device, DeviceSortKey>
  columns={columns}
  rows={devices}
  rowKey={(d) => d.id}
  sort={{ key: 'name', direction: 'asc' }}
  onSortChange={setSort}
  loading={isLoading}
  emptyState={<EmptyState icon={Smartphone} title="No devices" />}
/>
```

### Column definition

```ts
interface DataTableColumn<Row, SortKey> {
  key: string;          // React key
  header: ReactNode;    // Column header content
  sortKey?: SortKey;    // If set, header becomes a SortableHeader
  align?: 'left' | 'center' | 'right';
  width?: string;
  className?: string;
  headerClassName?: string;
  render: (row: Row, index: number) => ReactNode;
}
```

### Key props

| Prop | Type | Default |
|------|------|---------|
| `columns` | `DataTableColumn[]` | required |
| `rows` | `Row[]` | required |
| `rowKey` | `(row: Row) => string \| number` | required |
| `loading` | `boolean` | `false` |
| `error` | `ReactNode` | — |
| `emptyState` | `ReactNode` | Generic "No data" EmptyState |
| `sort` | `{ key: SortKey; direction: 'asc'\|'desc' }` | — |
| `onSortChange` | `(next) => void` | — |
| `onRowClick` | `(row) => void` | — |
| `selection` | `{ selectedKeys, onToggle, onToggleAll? }` | — |
| `rowActions` | `(row) => RowActionItem[]` | — |
| `density` | `'comfortable' \| 'compact'` | `'comfortable'` |
| `stickyHeader` | `boolean` | `false` |

Sort state is **controlled** — the caller manages `sort` and responds to `onSortChange`. Row-action clicks call `stopPropagation`, so `onRowClick` never fires when an action is selected.

---

## FilterBar

Layout-only container for filter controls with optional Clear affordance.

```tsx
<FilterBar onClear={clearFilters} trailing={<SearchInput />}>
  <select>…</select>
  <select>…</select>
</FilterBar>
```

| Prop | Type | Default |
|------|------|---------|
| `children` | `ReactNode` | required |
| `onClear` | `() => void` | — | Shows "Clear" link when provided |
| `trailing` | `ReactNode` | — | Right-aligned slot |
| `className` | `string` | — |

---

## ProportionalBar

Segmented breakdown bar with optional legend links.

```tsx
<ProportionalBar
  segments={[
    { key: 'available', label: 'Available', count: 4, barClassName: 'bg-success-soft0', to: '/devices?status=available' },
  ]}
/>
```

| Prop | Type | Default |
|------|------|---------|
| `segments` | `{ key, label, count, barClassName, dotClassName?, to? }[]` | required |

---

## AttentionListCard

Tonal attention card with total, supporting description, and linked detail rows.

```tsx
<AttentionListCard
  title="Attention"
  description="devices needing review"
  total={3}
  tone="warn"
  rows={[{ label: 'Telemetry coverage', values: '2 stale', to: '/devices?hardware_telemetry_state=stale' }]}
/>
```

| Prop | Type | Default |
|------|------|---------|
| `title` | `string` | required |
| `description` | `string` | — |
| `total` | `number` | required |
| `tone` | `'neutral' \| 'warn' \| 'critical'` | required |
| `rows` | `{ label, values, to? }[]` | required |

---

## DividedHealthStrip

Bordered horizontal health strip with icon-labeled cells and tone-coded values.

```tsx
<DividedHealthStrip
  cells={[
    { icon: Database, label: 'Database', tone: 'ok', value: 'Connected' },
  ]}
/>
```

| Prop | Type | Default |
|------|------|---------|
| `cells` | `{ icon, label, tone, value, detail? }[]` | required |

---

## IconButton

Square button for icon-only actions.

```tsx
<IconButton aria-label="Delete" icon={<Trash2 size={16} />} variant="danger" />
```

| Prop | Type | Default |
|------|------|---------|
| `aria-label` | `string` | required |
| `icon` | `ReactNode` | required |
| `variant` | `'ghost' \| 'danger'` | `'ghost'` |
| `size` | `'sm' \| 'md'` | `'md'` |
| `tooltip` | `string` | — | Maps to `title` |

---

## Modal

Full-screen overlay dialog with focus trap, Escape handling, and body scroll lock.

```tsx
<Modal
  isOpen={open}
  onClose={() => setOpen(false)}
  title="Edit Device"
  size="lg"
  footer={<><Button variant="secondary" onClick={…}>Cancel</Button><Button onClick={save}>Save</Button></>}
>
  {/* body content */}
</Modal>
```

| Prop | Type | Default |
|------|------|---------|
| `isOpen` | `boolean` | required |
| `onClose` | `() => void` | required |
| `title` | `string` | required |
| `children` | `ReactNode` | required |
| `size` | `'sm' \| 'md' \| 'lg' \| 'xl'` | `'md'` |
| `footer` | `ReactNode` | — | Rendered in a border-t footer bar |
| `initialFocusRef` | `RefObject<HTMLElement>` | — | Element to focus on open |
| `closeOnBackdropClick` | `boolean` | `true` |
| `closeOnEscape` | `boolean` | `true` |

---

## Pagination

Server-pagination footer with page navigation, page-size selection, and range summary.

```tsx
<Pagination
  page={2}
  pageSize={50}
  total={123}
  onPageChange={setPage}
  onPageSizeChange={setPageSize}
/>
```

| Prop | Type | Default |
|------|------|---------|
| `page` | `number` | required |
| `pageSize` | `number` | required |
| `total` | `number \| null` | — |
| `pageSizeOptions` | `number[]` | `[25, 50, 100]` |
| `onPageChange` | `(page: number) => void` | required |
| `onPageSizeChange` | `(pageSize: number) => void` | required |
| `className` | `string` | — |

When `total` is omitted, the component still shows the current range and next/prev controls, but hides the `Last` button and `of N` copy.

---

## StatCard

Shared dashboard-style stat card with left-border tone accent.

```tsx
<StatCard label="Hosts" value={12} icon={Server} accent="bg-accent-soft text-accent" tone="neutral" />
```

| Prop | Type | Default |
|------|------|---------|
| `label` | `string` | required |
| `value` | `number \| string` | required |
| `icon` | `LucideIcon` | required |
| `accent` | `string` | required |
| `tone` | `'neutral' \| 'positive' \| 'warn' \| 'critical'` | `'neutral'` |
| `hint` | `string` | — |

---

## SectionHeader

Page or card section heading with optional description and right-aligned actions.

```tsx
<SectionHeader
  title="Devices"
  description="All managed devices in the fleet."
  actions={<Button>Add Device</Button>}
  level={1}
/>
```

| Prop | Type | Default |
|------|------|---------|
| `title` | `ReactNode` | required |
| `description` | `ReactNode` | — |
| `actions` | `ReactNode` | — |
| `level` | `1 \| 2 \| 3` | `2` |
| `className` | `string` | — |

---

## FetchError

Inline error banner shown in place of a content area when a query fails. Always includes a **Retry** affordance so the
user is never left with a blank or partial page.

```tsx
const { data, isError, refetch } = useItems();

{isError && (
  <FetchError
    message="Could not load items."
    onRetry={() => void refetch()}
    className="mb-4"
  />
)}
```

| Prop | Type | Default |
|------|------|---------|
| `onRetry` | `() => void` | required |
| `message` | `string` | `'Something went wrong while loading this data.'` |
| `className` | `string` | — |

**Domain rule**: every page that fetches data must render `<FetchError>` on error. A page must never display a blank
body when a query fails.

---

## Design Tokens (`src/tokens.css`)

A minimal `@layer components` layer that controls spacing and surface styles across the whole app.
Import is automatic via `index.css`. Use these class names instead of ad-hoc Tailwind strings on
top-level layout containers.

| Class | Meaning |
|-------|---------|
| `.card` | White card surface — `bg-surface-1 rounded-lg border border-border` |
| `.card-padding` | Card inner padding — `p-5` |
| `.section-gap` | Bottom margin between page sections — `mb-8` |
| `.heading-page` | Page title scale — display font, `text-2xl font-semibold text-text-1` |
| `.heading-section` | Card section title scale — display font, `text-base font-semibold text-text-1` |
| `.heading-subsection` | Nested section title scale — display font, `text-sm font-semibold text-text-1` |
| `.section-header-text` | Supporting label style — `text-sm font-medium text-text-2` |
| `.page-gutter` | Page shell padding — `px-6 pt-6 pb-10` (applied by Layout) |

**Keep the token layer small.** Only shared spacing, surface, heading, and numeric treatments live here.
Do not add one-off typography or color aliases for local component needs.
