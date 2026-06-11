import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';

interface Props {
  icon: LucideIcon;
  title: string;
  description: string;
  action?: ReactNode;
}

export function DashedEmptyPanel({ icon: Icon, title, description, action }: Props) {
  return (
    <section className="rounded-lg border border-dashed border-border-strong bg-surface-2 px-5 py-8">
      <div className="mx-auto flex max-w-xl flex-col items-center text-center sm:flex-row sm:text-left">
        <div className="mb-4 flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-1 text-text-2 sm:mb-0 sm:mr-4">
          <Icon size={20} />
        </div>
        <div className="min-w-0 flex-1">
          <h3 className="heading-subsection">{title}</h3>
          <p className="mt-1 text-sm text-text-2">{description}</p>
        </div>
        {action ? <div className="mt-4 sm:ml-5 sm:mt-0">{action}</div> : null}
      </div>
    </section>
  );
}
