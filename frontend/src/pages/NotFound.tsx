import { Link } from 'react-router-dom';
import { usePageTitle } from '../hooks/usePageTitle';

export default function NotFound() {
  usePageTitle('Not Found');
  return (
    <div className="flex flex-col items-center justify-center py-24 text-center">
      <h1 className="text-4xl font-bold text-text-1 mb-2">404</h1>
      <p className="text-text-3 mb-6">Page not found</p>
      <Link
        to="/"
        className="px-4 py-2 text-sm font-medium text-accent-on bg-accent rounded-md hover:bg-accent-hover"
      >
        Go to Dashboard
      </Link>
    </div>
  );
}
