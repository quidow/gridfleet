import type { DriverPack } from '../../types/driverPacks';

interface DivergedFromUpstreamBadgeProps {
  pack: DriverPack;
  catalog: DriverPack[];
}

/**
 * Renders a small warning badge when a pack's `derived_from` points to an
 * upstream pack whose current release in the catalog has advanced beyond the
 * release the fork was cut from.
 *
 * Renders nothing when:
 * - `pack.derived_from` is null / undefined
 * - the upstream pack is not present in `catalog`
 * - the upstream's `current_release` matches `pack.derived_from.release`
 */
export default function DivergedFromUpstreamBadge({
  pack,
  catalog,
}: DivergedFromUpstreamBadgeProps) {
  const derivedFrom = pack.derived_from;
  if (!derivedFrom) {
    return null;
  }

  const upstream = catalog.find((p) => p.id === derivedFrom.pack_id);
  if (!upstream) {
    return null;
  }

  if (!upstream.current_release || upstream.current_release === derivedFrom.release) {
    return null;
  }

  return (
    <span className="inline-flex items-center rounded-full bg-warning-soft px-2.5 py-0.5 text-xs font-medium text-warning-foreground">
      Diverged from {derivedFrom.pack_id} {upstream.current_release}
    </span>
  );
}
