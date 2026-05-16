import { useState, type FormEvent } from 'react';
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom';
import { LockKeyhole, Shield } from 'lucide-react';
import { Button, Card } from '../components/ui';
import { LoadingSpinner } from '../components/LoadingSpinner';
import { useAuth } from '../context/auth';
import { usePageTitle } from '../hooks/usePageTitle';
import { normalizeNextTarget } from '../lib/authRouting';

export default function Login() {
  usePageTitle('Login');
  const auth = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const nextTarget = normalizeNextTarget(searchParams.get('next'));

  if (auth.loading) {
    return <LoadingSpinner />;
  }

  if (!auth.enabled) {
    return <Navigate to="/" replace />;
  }

  if (auth.authenticated) {
    return <Navigate to={nextTarget} replace />;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);

    try {
      await auth.login({ username, password });
      navigate(nextTarget, { replace: true });
    } catch (submitError) {
      const message = submitError instanceof Error ? submitError.message : 'Login failed';
      setError(message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface-0 px-6 py-12">
      <Card
        as="section"
        padding="none"
        className="w-full max-w-md overflow-hidden border-border shadow-[0_24px_80px_-40px_rgba(15,23,42,0.35)]"
      >
        <div className="border-b border-border bg-sidebar-surface px-8 py-6 text-white">
          <div className="mb-4 inline-flex rounded-full bg-white/10 p-3">
            <Shield size={24} />
          </div>
          <h1 className="text-2xl font-semibold">GridFleet</h1>
          <p className="mt-2 text-sm text-sidebar-text">
            Sign in with the shared operator credential set to access the production control plane.
          </p>
        </div>

        <form className="space-y-5 px-8 py-8" onSubmit={handleSubmit}>
          <div className="space-y-2">
            <label htmlFor="username" className="text-sm font-medium text-text-2">
              Username
            </label>
            <input
              id="username"
              type="text"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              className="w-full rounded-md border border-border-strong px-3 py-2 text-sm text-text-1 shadow-sm outline-none transition focus:border-accent focus:ring-2 focus:ring-accent"
              placeholder="operator"
              required
            />
          </div>

          <div className="space-y-2">
            <label htmlFor="password" className="text-sm font-medium text-text-2">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="w-full rounded-md border border-border-strong px-3 py-2 text-sm text-text-1 shadow-sm outline-none transition focus:border-accent focus:ring-2 focus:ring-accent"
              placeholder="Enter password"
              required
            />
          </div>

          {error ? (
            <div
              role="alert"
              className="rounded-md border border-danger-strong/30 bg-danger-soft px-3 py-2 text-sm text-danger-foreground"
            >
              {error}
            </div>
          ) : null}

          <Button
            type="submit"
            fullWidth
            loading={submitting}
            leadingIcon={<LockKeyhole size={16} />}
          >
            Sign In
          </Button>
        </form>
      </Card>
    </div>
  );
}
