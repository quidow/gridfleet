import { useState, useEffect, type ReactNode } from 'react';
import { SidebarContext } from '../context/SidebarContext';

function getInitialCollapsed(): boolean {
  try {
    const stored = localStorage.getItem('sidebar-collapsed');
    if (stored !== null) return stored === 'true';
  } catch {
    // localStorage unavailable
  }
  return typeof window !== 'undefined' && window.innerWidth < 768;
}

export default function SidebarProvider({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(getInitialCollapsed);

  useEffect(() => {
    try {
      localStorage.setItem('sidebar-collapsed', String(collapsed));
    } catch {
      // localStorage unavailable
    }
  }, [collapsed]);

  function toggle() {
    setCollapsed((prev) => !prev);
  }

  return (
    <SidebarContext.Provider value={{ collapsed, toggle }}>
      {children}
    </SidebarContext.Provider>
  );
}
