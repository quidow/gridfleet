import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '../../context/auth';
import { buildLocationTarget, normalizeNextTarget } from '../../lib/authRouting';
import LoadingSpinner from '../LoadingSpinner';

export default function ProtectedRoute() {
  const auth = useAuth();
  const location = useLocation();

  if (auth.loading) {
    return <LoadingSpinner />;
  }

  if (!auth.enabled || auth.authenticated) {
    return <Outlet />;
  }

  const nextTarget = normalizeNextTarget(buildLocationTarget(location));
  const params = new URLSearchParams();
  if (nextTarget !== '/') {
    params.set('next', nextTarget);
  }
  const suffix = params.toString();

  return <Navigate to={`/login${suffix ? `?${suffix}` : ''}`} replace />;
}
