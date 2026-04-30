import { Link } from 'react-router-dom';
import { LockKeyhole } from 'lucide-react';

interface ReservationPillProps {
  reservation: {
    run_id: string;
    run_name: string;
    excluded: boolean;
  };
}

export default function ReservationPill({ reservation }: ReservationPillProps) {
  const suffix = reservation.excluded ? ' (excluded)' : '';
  return (
    <Link
      to={`/runs/${reservation.run_id}`}
      className="inline-flex items-center gap-1.5 rounded-full bg-accent-soft px-3 py-1 text-xs font-medium text-accent hover:bg-accent-soft/80"
    >
      <LockKeyhole size={12} />
      Reserved by {reservation.run_name}
      {suffix}
    </Link>
  );
}
