import type { ReactNode } from 'react';
import { SectionHeader } from '../ui';

interface Props {
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
}

/**
 * Shared chrome for all Settings tab panels: consistent title/actions header
 * above the panel body. The panel body supplies its own visual container
 * (SettingsSection uses its own card grid; registry panels use a shadow table).
 */
export default function SettingsPanelLayout({ title, description, actions, children }: Props) {
  return (
    <div>
      <SectionHeader title={title} description={description} actions={actions} className="mb-4" />
      {children}
    </div>
  );
}
