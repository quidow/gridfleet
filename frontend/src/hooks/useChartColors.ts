import { useTheme } from '../context/theme';

// Recharts needs concrete hex at render time, so chart series colors live
// outside the CSS token layer. Light mode keeps the existing palette; dark mode
// remaps each series to the calmer GitHub-aligned palette so lines and bars do
// not glow against the navy surfaces. Keys are the light-mode hex already used
// in the chart components, so callers just wrap their existing color.
const DARK_REMAP: Record<string, string> = {
  '#22c55e': '#3fb950', // green  — passed / active / memory
  '#16a34a': '#3fb950',
  '#ef4444': '#f85149', // red    — failed / overloaded / unfulfilled
  '#dc2626': '#f85149',
  '#f59e0b': '#d29922', // amber  — error / disk / queued / underutilized
  '#d97706': '#d29922',
  '#3b82f6': '#58a6ff', // blue   — supply / CPU / utilization
  '#2563eb': '#58a6ff',
  '#7c3aed': '#a371f7', // purple — inferred demand
  '#d1d5db': '#6e7681', // neutral — idle
};

export function useChartColor(): (lightHex: string) => string {
  const { mode } = useTheme();
  return (lightHex) => (mode === 'dark' ? DARK_REMAP[lightHex] ?? lightHex : lightHex);
}
