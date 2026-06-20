import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Check, Copy } from 'lucide-react';

import type { GridRouterNodeRead } from '../../types/gridRouter';

type OpState = GridRouterNodeRead['operational_state'];

const COLOR: Record<OpState, string> = {
  available: 'bg-emerald-500',
  busy: 'bg-amber-500',
  verifying: 'bg-indigo-500',
  offline: 'bg-red-500',
  maintenance: 'bg-slate-400',
};

export function NodeCard({ node }: { node: GridRouterNodeRead }) {
  const [copied, setCopied] = useState(false);

  const copyKeys = async () => {
    await navigator.clipboard.writeText(JSON.stringify(node.stereotype, null, 2));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="relative flex flex-col overflow-hidden rounded-xl border border-border bg-surface-1 py-3 pl-4 pr-3 shadow-sm">
      <span className={`absolute inset-y-0 left-0 w-1.5 ${COLOR[node.operational_state]}`} />
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 shrink-0 rounded-full ${COLOR[node.operational_state]}`} />
        <span className="text-xs font-bold uppercase tracking-wide">{node.operational_state}</span>
        {node.node_effective_state ? (
          <span className="text-xs font-semibold text-text-3">{node.node_effective_state}</span>
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
