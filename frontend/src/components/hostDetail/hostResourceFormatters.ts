export function formatCpuUsage(cpuPercent: number | null, cores: number | null): string | null {
  if (
    cpuPercent === null ||
    cores === null ||
    cores <= 0 ||
    !Number.isFinite(cpuPercent) ||
    cpuPercent < 0
  ) {
    return null;
  }
  const busy = (cpuPercent / 100) * cores;
  return `${busy.toFixed(1)} / ${cores} cores`;
}
