import { useEffect, useMemo, useState } from 'react';

const HOUR_MS = 3_600_000;

export function useRolling7DayParams() {
  const [hourKey, setHourKey] = useState(() => Math.floor(Date.now() / HOUR_MS));
  useEffect(() => {
    const id = window.setInterval(() => setHourKey(Math.floor(Date.now() / HOUR_MS)), 60_000);
    return () => window.clearInterval(id);
  }, []);
  return useMemo(() => {
    const to = new Date((hourKey + 1) * HOUR_MS);
    const from = new Date(to);
    from.setDate(from.getDate() - 7);
    return { date_from: from.toISOString(), date_to: to.toISOString() };
  }, [hourKey]);
}
