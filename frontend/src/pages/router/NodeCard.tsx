import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Check, Copy } from 'lucide-react';

import { Badge, type BadgeTone } from '../../components/ui/Badge';
import { OPERATIONAL_STATE_TONE } from '../../lib/deviceState';
import type { GridRouterNodeRead } from '../../types/gridRouter';

// The Badge (status pill + dot) and the card spine both derive from the shared
// operational-state tone map, so the colour for a state stays identical to the
// Devices table and dashboard.
const SPINE: Record<BadgeTone, string> = {
  success: 'bg-success-strong',
  warning: 'bg-warning-strong',
  info: 'bg-info-strong',
  critical: 'bg-danger-strong',
  neutral: 'bg-neutral-strong',
};

export function NodeCard({ node }: { node: GridRouterNodeRead }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timerRef.current), []);

  const copyKeys = async () => {
    // navigator.clipboard is undefined on non-secure (plain-HTTP) origins; guard so
    // the button degrades quietly instead of throwing an unhandled rejection.
    if (!navigator.clipboard?.writeText) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(node.stereotype, null, 2));
      setCopied(true);
      timerRef.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      // write rejected (permissions / secure-context) — keep prior state
    }
  };

  const tone = OPERATIONAL_STATE_TONE[node.operational_state];

  return (
    <div className="relative flex flex-col overflow-hidden rounded-xl border border-border bg-surface-1 py-3 pl-4 pr-3 shadow-sm">
      <span className={`absolute inset-y-0 left-0 w-1.5 ${SPINE[tone]}`} />
      <div className="flex items-center gap-2">
        <Badge tone={tone} dot>
          {node.operational_state}
        </Badge>
        {node.node_effective_state ? (
          <span className="text-xs font-semibold text-text-3">{node.node_effective_state}</span>
        ) : null}
        {node.operational_state === 'available' && node.unavailable_reason ? (
          // An `available` device the allocator would still refuse (warm-park / restart /
          // reserved). For the other states the reason just echoes the badge, so it is
          // only surfaced here where it adds information.
          <span className="rounded-md bg-warning-soft px-2 py-0.5 text-xs font-semibold text-warning-strong">
            {node.unavailable_reason}
          </span>
        ) : null}
        <span className="ml-auto rounded-md bg-surface-2 px-2 py-0.5 text-xs font-semibold text-text-2">
          {node.platform_id}
        </span>
      </div>

      <Link to={`/devices/${node.device_id}`} className="mt-2 text-base font-bold hover:underline">
        {node.device_name}
      </Link>

      <div className="mt-2 rounded-lg border border-border bg-surface-2 px-3 py-2 font-mono text-xs leading-relaxed">
        {Object.entries(node.stereotype).map(([key, value]) => (
          <div key={key} className="overflow-hidden text-ellipsis whitespace-nowrap">
            <span className="text-text-3">{key}:</span> <span className="font-semibold text-text-1">{String(value)}</span>
          </div>
        ))}
      </div>

      <div className="mt-auto flex items-center gap-2 pt-2 text-xs text-text-3">
        {node.host_id ? (
          <Link to={`/hosts/${node.host_id}`} className="hover:underline">
            {node.host_name ?? 'host'}
          </Link>
        ) : (
          <span>no host</span>
        )}
        {node.session_id ? (
          <Link to="/sessions" className="font-semibold text-text-1 hover:underline">
            {node.session_id}
            {node.session_target ? ` → ${node.session_target}` : ''}
          </Link>
        ) : (
          <span>no session</span>
        )}
        <button
          type="button"
          onClick={copyKeys}
          className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border bg-surface-2 px-2.5 py-1 text-xs font-medium text-text-2 hover:bg-surface-1"
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? 'Copied' : 'Copy keys'}
        </button>
      </div>
    </div>
  );
}
