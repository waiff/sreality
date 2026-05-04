import { Navigate, type RouteObject } from 'react-router-dom';
import Shell from './components/Shell';
import Browse from './pages/Browse';
import ListingDetail from './pages/ListingDetail';
import Region from './pages/Region';
import Health from './pages/Health';
import Estimate from './pages/Estimate';
import EstimationDetail from './pages/EstimationDetail';
import EstimationList from './pages/EstimationList';

export const routes: RouteObject[] = [
  {
    path: '/',
    element: <Shell />,
    children: [
      { index: true, element: <Navigate to="/browse" replace /> },
      { path: 'browse', element: <Browse /> },
      { path: 'listing', element: <ListingDetail /> },
      { path: 'listing/:sreality_id', element: <ListingDetail /> },
      { path: 'region', element: <Region /> },
      { path: 'health', element: <Health /> },
      { path: 'estimate', element: <Estimate /> },
      { path: 'estimations', element: <EstimationList /> },
      { path: 'estimation/:id', element: <EstimationDetail /> },
      { path: '*', element: <NotFound /> },
    ],
  },
];

function NotFound() {
  return (
    <div className="px-6 py-16 max-w-md mx-auto text-center">
      <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        404
      </p>
      <h1 className="mt-2 text-2xl">Not here.</h1>
      <p className="mt-2 text-sm text-[var(--color-ink-3)]">
        That page doesn't exist in the browser.
      </p>
    </div>
  );
}
