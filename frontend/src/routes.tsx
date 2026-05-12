import { Navigate, type RouteObject } from 'react-router-dom';
import Shell from './components/Shell';
import Browse from './pages/Browse';
import ListingDetail from './pages/ListingDetail';
import Region from './pages/Region';
import Health from './pages/Health';
import EstimationDetail from './pages/EstimationDetail';
import EstimationList from './pages/EstimationList';
import EstimationCompare from './pages/EstimationCompare';
import Collections from './pages/Collections';
import CollectionDetail from './pages/CollectionDetail';
import Settings from './pages/Settings';
// TODO(estimation-5 Part C1): remove DevConfidencePreview + its route
// once design is approved and the indicator is in real use.
import DevConfidencePreview from './pages/DevConfidencePreview';

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
      { path: 'estimate', element: <Navigate to="/estimations" replace /> },
      { path: 'estimations', element: <EstimationList /> },
      { path: 'estimations/compare', element: <EstimationCompare /> },
      { path: 'estimation/:id', element: <EstimationDetail /> },
      { path: 'collections', element: <Collections /> },
      { path: 'collection/:id', element: <CollectionDetail /> },
      { path: 'settings', element: <Settings /> },
      { path: 'dev/confidence-indicator', element: <DevConfidencePreview /> },
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
