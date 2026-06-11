import { Navigate, type RouteObject } from 'react-router-dom';
import Shell from './components/Shell';
import Browse from './pages/Browse';
import ListingDetail from './pages/ListingDetail';
import Health from './pages/Health';
import BuildingDetail from './pages/BuildingDetail';
import EstimationDetail from './pages/EstimationDetail';
import EstimationList from './pages/EstimationList';
import Collections from './pages/Collections';
import CollectionDetail from './pages/CollectionDetail';
import Datasets from './pages/Datasets';
import Settings from './pages/Settings';
import Scrapers from './pages/Scrapers';
import Watchdog from './pages/Watchdog';
import WatchdogManage from './pages/WatchdogManage';
import WatchdogEdit from './pages/WatchdogEdit';
import Dedup from './pages/Dedup';
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
      // Bare /listing handles the ?property=ID query form (the dedup merge feed
      // + Browse merge links use it); ListingDetail resolves it to the
      // property's representative listing and redirects to /listing/:id.
      { path: 'listing', element: <ListingDetail /> },
      { path: 'listing/:sreality_id', element: <ListingDetail /> },
      { path: 'health', element: <Health /> },
      { path: 'estimate', element: <Navigate to="/estimations" replace /> },
      { path: 'estimations', element: <EstimationList /> },
      { path: 'estimation/:id', element: <EstimationDetail /> },
      { path: 'building/:id', element: <BuildingDetail /> },
      { path: 'collections', element: <Collections /> },
      { path: 'collection/:id', element: <CollectionDetail /> },
      { path: 'datasets', element: <Datasets /> },
      { path: 'watchdog', element: <Watchdog /> },
      { path: 'watchdog/manage', element: <WatchdogManage /> },
      { path: 'watchdog/:id/edit', element: <WatchdogEdit /> },
      { path: 'dedup', element: <Dedup /> },
      { path: 'settings', element: <Settings /> },
      { path: 'scrapers', element: <Scrapers /> },
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
