export interface TabDefinition {
  id: string;
  label: string;
  /** Optional section header. Tabs with matching section values are grouped under that header. */
  section?: string;
}

interface TabsProps {
  tabs: TabDefinition[];
  activeId: string;
  onChange: (id: string) => void;
  className?: string;
}

/**
 * Tab strip component.
 *
 * When any tab carries a `section` field, tabs are grouped under muted section
 * headers. Otherwise a flat strip is rendered (matches Analytics.tsx visual style).
 */
export function Tabs({ tabs, activeId, onChange, className }: TabsProps) {
  const isGrouped = tabs.some((t) => t.section !== undefined);

  if (isGrouped) {
    // Collect unique sections in order of first appearance
    const sections: string[] = [];
    for (const t of tabs) {
      const s = t.section ?? 'Other';
      if (!sections.includes(s)) sections.push(s);
    }

    return (
      <div className={`border-b border-border ${className ?? ''}`}>
        {/* Wrap (not horizontal-scroll): with many grouped tabs a scroll strip
            clips the trailing tabs with no affordance. Wrapping keeps every tab
            visible at any width. */}
        <nav aria-label="Tabs" className="-mb-px flex flex-wrap items-end gap-y-1">
          {sections.map((section, sIdx) => {
            const sectionTabs = tabs.filter((t) => (t.section ?? 'Other') === section);
            return (
              <div key={section} className="flex flex-wrap items-end">
                {/* Subtle separator between section groups */}
                {sIdx > 0 && (
                  <div className="mx-3 mb-2 h-6 w-px self-end bg-border" />
                )}
                {sectionTabs.map((tab) => (
                  <button
                    key={tab.id}
                    onClick={() => onChange(tab.id)}
                    className={`whitespace-nowrap border-b-2 px-3 pb-3 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 ${
                      activeId === tab.id
                        ? 'border-accent text-accent'
                        : 'border-transparent text-text-2 hover:border-border-strong hover:text-text-1'
                    }`}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>
            );
          })}
        </nav>
      </div>
    );
  }

  // Flat strip — mirrors Analytics.tsx:74-90
  return (
    <div className={`border-b border-border ${className ?? ''}`}>
      <nav aria-label="Tabs" className="-mb-px flex space-x-8 overflow-x-auto">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => onChange(tab.id)}
            className={`whitespace-nowrap border-b-2 px-1 py-3 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 ${
              activeId === tab.id
                ? 'border-accent text-accent'
                : 'border-transparent text-text-2 hover:border-border-strong hover:text-text-1'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </nav>
    </div>
  );
}

