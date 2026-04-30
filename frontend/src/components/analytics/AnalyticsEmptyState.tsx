import { BarChart3 } from 'lucide-react';

type Props = {
  title: string;
  description: string;
};

export default function AnalyticsEmptyState({ title, description }: Props) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border bg-surface-2 px-6 py-10 text-center">
      <div className="mb-3 rounded-full bg-surface-1 p-3 text-text-3 shadow-sm">
        <BarChart3 size={20} />
      </div>
      <h4 className="text-sm font-medium text-text-1">{title}</h4>
      <p className="mt-1 max-w-md text-sm text-text-3">{description}</p>
    </div>
  );
}
