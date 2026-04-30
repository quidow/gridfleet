import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';

interface Props {
  icon: LucideIcon;
  title: string;
  description?: string;
  action?: ReactNode;
  className?: string;
}

export default function EmptyState({ icon: Icon, title, description, action, className = '' }: Props) {
  return (
    <div className={['flex flex-col items-center justify-center py-12 text-center', className].filter(Boolean).join(' ')}>
      <Icon className="mb-4 text-text-3" size={48} />
      <p className="text-sm font-medium text-text-1">{title}</p>
      {description ? <p className="mt-1 text-sm text-text-2">{description}</p> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}
