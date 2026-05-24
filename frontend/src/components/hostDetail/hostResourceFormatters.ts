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
  return `${cores} cores`;
}
