# Reinstalling GridFleet without re-adding every device

This runbook covers using the device portability bundle to recover a registered fleet after a fresh GridFleet install or DB wipe.

## 1. Export from the old instance

While the old instance is still running:

1. Open **Settings → Backup & Restore** (`/settings?tab=backup`).
2. Click **Export Config** in the **Export configuration** card. The browser downloads `gridfleet-devices-<timestamp>.json`.
3. Keep this file alongside any other ops artifacts you preserve across the install.

The bundle is human-readable. You can hand-edit it before importing: strip rows, adjust a device's `static_groups`, redirect a row's `original_host.hostname`. The bundle carries identity + operator config + testkit data + device groups only — runtime state (operational_state, telemetry, verification stamps) is **not** preserved; the verification pipeline rediscovers it after import.

### Bundle schema version

Exports are `schema_version: 2`. A v2 bundle carries device groups:

- a top-level `groups` array of group definitions (`key`, `name`, `description`, `group_type`, and `filters` including `member_of` for dynamic groups)
- a per-device `static_groups` array of static group keys

Groups are referenced by key throughout; no group UUID is exported, so a bundle moves cleanly between installs.

> **`schema_version: 1` bundles are rejected.** Import fails with `unsupported portability schema version; expected 2`. There is no converter. A v1 bundle predates device groups and its device `tags` cannot be reconstructed into groups after the fact — the tag-to-group migration is one-way and ran against the source database, not against exported files. If you are holding a v1 bundle, restore the source instance, run `alembic upgrade head` so its tags migrate into static groups, then re-export.

> **Deploying the release that introduced v2.** The same release drops the `devices.tags` column in one migration step, with no compatibility window, and it strands `device_verification` jobs that were queued before the deploy. `backend` and `backend-scheduler` must be recreated together or the old scheduler crash-loops against the new schema. See [Releases that drop a database column](backend-deploy-restart-rollback.md#releases-that-drop-a-database-column) before upgrading a live install.

If you hand-edit the groups section, keep these rules or the import is rejected before anything is written:

- every device `static_groups` key and every dynamic `member_of` key must name a **static** group defined in the same bundle
- no bundle group key may collide with a group already on the target install — keys are immutable, so resolve a collision by editing the bundle or deleting the existing group, never by renaming

> **Sensitive content.** The exported bundle includes per-device `test_data` and `device_config`. These may contain credentials, secrets, or other sensitive material your devices need at test time. Treat the bundle as a sensitive artifact — store it the same way you store other operator secrets, do not commit it to git, and rotate any embedded credentials if the file leaves operator hands.

## 2. Reinstall GridFleet

Run the standard install playbook. Bring up the backend, frontend, and at least one device host so the agent registers.

## 3. Re-register hosts

Device imports require their target hosts to exist. Each agent host re-registers itself on first boot. Verify on the Hosts page that all expected hosts have re-appeared **before** continuing.

## 4. Import the bundle

1. Open **Settings → Backup & Restore** (`/settings?tab=backup`) and use the **Import devices** card (the legacy `/devices/import` URL redirects there).
2. Upload the JSON bundle.
3. The wizard validates the bundle and shows a per-row preview:
   - **valid** rows are pre-mapped to the best-matching registered host (case-insensitive hostname match). Override the target host where needed.
   - **conflict (skip)** rows already exist in this DB. They are excluded from the commit.
   - **duplicate in bundle** rows share an identity with another row in the same bundle. Edit the bundle to remove duplicates and re-upload.
   - **invalid** rows are flagged with the **invalid** status badge and excluded from the commit (the wizard does not show per-row issue text).
4. Confirm every row you want imported has a target host set.
5. Click **Commit import**. The wizard reports `created / skipped / failed` counts.

## 5. Watch verification

Each created device is queued for verification on commit. Devices transition `offline → verifying → available` as the standard pipeline runs. Watch the Devices page or a per-device detail view.

## Troubleshooting

- **409 on commit:** the bundle hash didn't match. Re-run the validate step (re-upload the bundle); something changed between validate and commit, or the browser tab was reloaded after validate.
- **A row "failed: identity conflict":** another import (or hand-add) inserted the same identity between validate and commit. Re-validate the leftover rows and try again.
- **"unsupported portability schema version; expected 2":** the bundle is a v1 export. See [Bundle schema version](#bundle-schema-version) — re-export from a migrated source instance.
- **Import rejected for an unknown group reference:** a device's `static_groups` entry, or a dynamic group's `member_of` entry, names a static group that is not defined in the bundle. Add the missing group definition or drop the reference.
- **Import rejected for a group key collision:** a group key in the bundle already exists on this install. Keys are immutable — edit the bundle to use a different key, or delete the existing group first.
- **A row "failed: verification enqueue failed":** the device insert was rolled back. The row needs no cleanup; investigate the underlying job queue, then re-upload that subset.

## Inventory snapshot (separate feature)

The Devices page also has an **Export Inventory** button. That export is a *read-only snapshot* with runtime fields included (operational_state, hardware telemetry, verification status). It is **not** round-trippable through the import wizard. Use it for spreadsheets, audits, or external tooling.

Choose between CSV or JSON via the modal's format toggle. The column picker remembers your last selection in `localStorage`. The active Devices-page filters (excluding sort and pagination) are applied to the export.

## Out of scope

- Re-importing hosts. Hosts come back through the standard agent registration flow.
- Re-importing reservations, sessions, or lifecycle policy state. (Device groups **are** in the bundle as of `schema_version: 2`.)
- Bundle signing or supply-chain integrity verification beyond the canonical hash that defends against in-flight tampering.
