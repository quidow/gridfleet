# Reinstalling GridFleet without re-adding every device

This runbook covers using the device portability bundle to recover a registered fleet after a fresh GridFleet install or DB wipe.

## 1. Export from the old instance

While the old instance is still running:

1. Open the Devices page (`/devices`).
2. Click **Export Config**. The browser downloads `gridfleet-devices-<timestamp>.json`.
3. Keep this file alongside any other ops artifacts you preserve across the install.

The bundle is human-readable. You can hand-edit it before importing: strip rows, fix tags, redirect a row's `original_host.hostname`. The bundle carries identity + operator config + testkit data only — runtime state (operational_state, holds, telemetry, verification stamps) is **not** preserved; the verification pipeline rediscovers it after import.

## 2. Reinstall GridFleet

Run the standard install playbook. Bring up the backend, frontend, and at least one device host so the agent registers.

## 3. Re-register hosts

Device imports require their target hosts to exist. Each agent host re-registers itself on first boot. Verify on the Hosts page that all expected hosts have re-appeared **before** continuing.

## 4. Import the bundle

1. Open **Devices → Import Devices** (`/devices/import`).
2. Upload the JSON bundle.
3. The wizard validates the bundle and shows a per-row preview:
   - **valid** rows are pre-mapped to the best-matching registered host (case-insensitive hostname match). Override the target host where needed.
   - **conflict (skip)** rows already exist in this DB. They are excluded from the commit.
   - **duplicate in bundle** rows share an identity with another row in the same bundle. Edit the bundle to remove duplicates and re-upload.
   - **invalid** rows are excluded; their issues column explains why (e.g. pack not installed).
4. Confirm every row you want imported has a target host set.
5. Click **Commit import**. The wizard reports `created / skipped / failed` counts.

## 5. Watch verification

Each created device is queued for verification on commit. Devices transition `offline → verifying → available` as the standard pipeline runs. Watch the Devices page or a per-device detail view.

## Troubleshooting

- **409 on commit:** the bundle hash didn't match. Re-run the validate step (re-upload the bundle); something changed between validate and commit, or the browser tab was reloaded after validate.
- **A row "failed: identity conflict":** another import (or hand-add) inserted the same identity between validate and commit. Re-validate the leftover rows and try again.
- **A row "failed: verification enqueue failed":** the device insert was rolled back. The row needs no cleanup; investigate the underlying job queue, then re-upload that subset.

## Inventory snapshot (separate feature)

The Devices page also has an **Export Inventory** button. That export is a *read-only snapshot* with runtime fields included (operational_state, hardware telemetry, verification status). It is **not** round-trippable through the import wizard. Use it for spreadsheets, audits, or external tooling.

Choose between CSV or JSON via the modal's format toggle. The column picker remembers your last selection in `localStorage`. Filters from the Devices page are passed through to the export (future enhancement: currently passes no filters by default — wire pending).

## Out of scope

- Re-importing hosts. Hosts come back through the standard agent registration flow.
- Re-importing reservations, sessions, groups, holds, or lifecycle policy state.
- Bundle signing or supply-chain integrity verification beyond the canonical hash that defends against in-flight tampering.
