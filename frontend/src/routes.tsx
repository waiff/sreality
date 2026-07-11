import { Navigate, type RouteObject } from 'react-router-dom';
import Shell from './components/Shell';
import Login from './pages/Login';
import ForgotPassword from './pages/ForgotPassword';
import UpdatePassword from './pages/UpdatePassword';
import Browse from './pages/Browse';
import ListingDetail from './pages/ListingDetail';
import Health from './pages/Health';
import BuildingDetail from './pages/BuildingDetail';
import EstimationDetail from './pages/EstimationDetail';
import EstimationList from './pages/EstimationList';
import Brokers from './pages/Brokers';
import BrokerDetail from './pages/BrokerDetail';
import BrokerReview from './pages/BrokerReview';
import Outreach from './pages/Outreach';
import OutreachDetail from './pages/OutreachDetail';
import Collections from './pages/Collections';
import CollectionDetail from './pages/CollectionDetail';
import Pipeline from './pages/Pipeline';
import Datasets from './pages/Datasets';
import Settings from './pages/Settings';
import Scrapers from './pages/Scrapers';
import Watchdog from './pages/Watchdog';
import WatchdogManage from './pages/WatchdogManage';
import WatchdogEdit from './pages/WatchdogEdit';
import Notifications from './pages/Notifications';
import Dedup from './pages/Dedup';
import Costs from './pages/Costs';
// TODO(estimation-5 Part C1): remove DevConfidencePreview + its route
// once design is approved and the indicator is in real use.
import DevConfidencePreview from './pages/DevConfidencePreview';

export const routes: RouteObject[] = [
  // Full-page auth screens (outside the app Shell). Additive — these do not
  // gate the rest of the app yet; the login gate is flipped in a later Phase 1
  // increment once shared-market views are re-granted to `authenticated`.
  { path: '/login', element: <Login />, handle: { title: 'Sign in' } },
  { path: '/forgot-password', element: <ForgotPassword />, handle: { title: 'Reset password' } },
  { path: '/reset-password', element: <UpdatePassword />, handle: { title: 'New password' } },
  {
    path: '/',
    element: <Shell />,
    children: [
      // `handle.title` is the static browser-tab title for each route (the
      // single source of truth, read by TitleController via matchRoutes).
      // Dynamic pages (a listing, a broker, …) carry a generic fallback here
      // and refine it at runtime with usePageTitle — see lib/pageTitle.tsx.
      { index: true, element: <Navigate to="/browse" replace /> },
      { path: 'browse', element: <Browse />, handle: { title: 'Browse' } },
      // Bare /listing handles the ?property=ID query form (the dedup merge feed
      // + Browse merge links use it); ListingDetail resolves it to the
      // property's representative listing and redirects to /listing/:id.
      { path: 'listing', element: <ListingDetail />, handle: { title: 'Listing' } },
      { path: 'listing/:sreality_id', element: <ListingDetail />, handle: { title: 'Listing' } },
      { path: 'health', element: <Health />, handle: { title: 'Health' } },
      { path: 'costs', element: <Costs />, handle: { title: 'LLM costs' } },
      { path: 'estimate', element: <Navigate to="/estimations" replace /> },
      { path: 'estimations', element: <EstimationList />, handle: { title: 'Estimations' } },
      { path: 'estimation/:id', element: <EstimationDetail />, handle: { title: 'Estimation' } },
      { path: 'brokers', element: <Brokers />, handle: { title: 'Brokers' } },
      { path: 'brokers/review', element: <BrokerReview />, handle: { title: 'Brokers · Review' } },
      { path: 'brokers/:id', element: <BrokerDetail />, handle: { title: 'Broker' } },
      { path: 'outreach', element: <Outreach />, handle: { title: 'Outreach' } },
      { path: 'outreach/:id', element: <OutreachDetail />, handle: { title: 'Campaign' } },
      { path: 'building/:id', element: <BuildingDetail />, handle: { title: 'Building' } },
      { path: 'collections', element: <Collections />, handle: { title: 'Collections' } },
      { path: 'collection/:id', element: <CollectionDetail />, handle: { title: 'Collection' } },
      { path: 'pipeline', element: <Pipeline />, handle: { title: 'Pipeline' } },
      { path: 'datasets', element: <Datasets />, handle: { title: 'Datasets' } },
      { path: 'watchdog', element: <Watchdog />, handle: { title: 'Watchdogs' } },
      { path: 'watchdog/manage', element: <WatchdogManage />, handle: { title: 'Watchdogs · Manage' } },
      { path: 'watchdog/:id/edit', element: <WatchdogEdit />, handle: { title: 'Edit watchdog' } },
      { path: 'notifications', element: <Notifications />, handle: { title: 'Notifications' } },
      { path: 'dedup', element: <Dedup />, handle: { title: 'Dedup' } },
      { path: 'settings', element: <Settings />, handle: { title: 'Settings' } },
      { path: 'scrapers', element: <Scrapers />, handle: { title: 'Scrapers' } },
      { path: 'dev/confidence-indicator', element: <DevConfidencePreview />, handle: { title: 'Confidence indicator (dev)' } },
      { path: '*', element: <NotFound />, handle: { title: 'Not found' } },
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
