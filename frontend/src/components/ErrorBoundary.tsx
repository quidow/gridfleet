import { Component, type ErrorInfo, type ReactNode } from 'react';

type ErrorBoundaryProps = {
  children: ReactNode;
  level?: 'page' | 'section';
  scope?: string;
  resetKey?: string;
};

type ErrorBoundaryState = {
  error: Error | null;
};

class ErrorBoundaryImpl extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = {
    error: null,
  };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  override componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    const scope = this.props.scope ?? this.props.level ?? 'surface';
    console.error(`[ErrorBoundary:${scope}]`, error, errorInfo);
  }

  override componentDidUpdate(prevProps: ErrorBoundaryProps) {
    if (this.props.resetKey !== prevProps.resetKey && this.state.error) {
      this.setState({ error: null });
    }
  }

  private readonly handleReload = () => {
    if (this.props.level === 'page') {
      window.location.reload();
      return;
    }
    this.setState({ error: null });
  };

  override render() {
    const { error } = this.state;
    const level = this.props.level ?? 'section';

    if (!error) {
      return this.props.children;
    }

    if (level === 'page') {
      return (
        <div className="rounded-lg border border-danger-strong/30 bg-danger-soft px-6 py-10 text-center">
          <h2 className="text-lg font-semibold text-danger-foreground">Something went wrong</h2>
          <p className="mt-2 text-sm text-danger-foreground">
            This page failed to render. Reload to try again.
          </p>
          <button
            type="button"
            onClick={this.handleReload}
            className="mt-4 inline-flex items-center rounded-md bg-danger-strong px-4 py-2 text-sm font-medium text-danger-on hover:bg-danger-foreground"
          >
            Reload
          </button>
        </div>
      );
    }

    return (
      <div className="rounded-lg border border-warning-strong/30 bg-warning-soft px-5 py-6">
        <h2 className="text-sm font-semibold text-warning-foreground">Something went wrong</h2>
        <p className="mt-1 text-sm text-warning-foreground">
          This section failed to render. Reload to try again.
        </p>
        <button
          type="button"
          onClick={this.handleReload}
          className="mt-4 inline-flex items-center rounded-md border border-warning-strong/40 bg-surface-1 px-3 py-1.5 text-sm font-medium text-warning-foreground hover:bg-warning-soft"
        >
          Reload
        </button>
      </div>
    );
  }
}

type BoundaryWrapperProps = Omit<ErrorBoundaryProps, 'level'>;

export function PageErrorBoundary(props: BoundaryWrapperProps) {
  return <ErrorBoundaryImpl {...props} level="page" />;
}

export function SectionErrorBoundary(props: BoundaryWrapperProps) {
  return <ErrorBoundaryImpl {...props} level="section" />;
}
