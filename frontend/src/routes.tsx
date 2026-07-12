import { Suspense, lazy, type ReactNode } from 'react';
import { Navigate, type RouteObject } from 'react-router-dom';
import Shell from './components/Shell';
import { RequireAdmin, RequireAuth } from './components/guards';
import Login from './pages/Login';
import ForgotPassword from './pages/ForgotPassword';
import UpdatePassword from './pages/UpdatePassword';
import Browse from './pages/Browse';
import ListingDetail from './pages/ListingDetail';
import BuildingDetail from './pages/BuildingDetail';
import EstimationDetail from './pages/EstimationDetail';
import EstimationList from './pages/EstimationList';
import Brokers from './pages/Brokers';
import BrokerDetail from './pages/BrokerDetail';
import Collections from './pages/Collections';
import CollectionDetail from './pages/CollectionDetail';
import Pipeline from './pages/Pipeline';
import Watchdog from './pages/Watchdog';
import WatchdogManage from './pages/WatchdogManage';
import WatchdogEdit from './pages/WatchdogEdit';
import Notifications from './pages/Notifications';

// Admin-only pages are code-split out of the default bundle — a non-admin
// session never downloads them.
const Health = lazy(() => import('./pages/Health'));
const Costs = lazy(() => import('./pages/Costs'));
const Dedup = lazy(() => import('./pages/Dedup'));
const Scrapers = lazy(() => import('./pages/Scrapers'));
const Settings = lazy(() => import('./pages/Settings'));
const Datasets = lazy(() => import('./pages/Datasets'));
const Outreach = lazy(() => import('./pages/Outreach'));
const OutreachDetail = lazy(() => import('./pages/OutreachDetail'));
const BrokerReview = lazy(() => import('./pages/BrokerReview'));
// TODO(estimation-5 Part C1): remove DevConfidencePreview + its route
// once design is approved and the indicator is in real use.
const DevConfidencePreview = lazy(() => import('./pages/DevConfidencePreview'));

function AdminPage({ children }: { children: ReactNode }) {
  return (
    <RequireAdmin>
      <Suspense fallback={null}>{children}</Suspense>
    </RequireAdmin>
  );
}

export const routes: RouteObject[] = [
  // Full-page auth screens (outside the app Shell, so they stay reachable
  // while logged out — everything under the Shell requires a session).
  { path: '/login', element: <Login />, handle: { title: 'Sign in' } },
  { path: '/forgot-password', element: <ForgotPassword />, handle: { title: 'Reset password' } },
  { path: '/reset-password', element: <UpdatePassword />, handle: { title: 'New password' } },
  {
    path: '/',
    element: (
      <RequireAuth>
        <Shell />
      </RequireAuth>
    ),
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
      { path: 'health', element: <AdminPage><Health /></AdminPage>, handle: { title: 'Health' } },
      { path: 'costs', element: <AdminPage><Costs /></AdminPage>, handle: { title: 'LLM costs' } },
      { path: 'estimate', element: <Navigate to="/estimations" replace /> },
      { path: 'estimations', element: <EstimationList />, handle: { title: 'Estimations' } },
      { path: 'estimation/:id', element: <EstimationDetail />, handle: { title: 'Estimation' } },
      { path: 'brokers', element: <Brokers />, handle: { title: 'Brokers' } },
      { path: 'brokers/review', element: <AdminPage><BrokerReview /></AdminPage>, handle: { title: 'Brokers · Review' } },
      { path: 'brokers/:id', element: <BrokerDetail />, handle: { title: 'Broker' } },
      { path: 'outreach', element: <AdminPage><Outreach /></AdminPage>, handle: { title: 'Outreach' } },
      { path: 'outreach/:id', element: <AdminPage><OutreachDetail /></AdminPage>, handle: { title: 'Campaign' } },
      { path: 'building/:id', element: <BuildingDetail />, handle: { title: 'Building' } },
      { path: 'collections', element: <Collections />, handle: { title: 'Collections' } },
      { path: 'collection/:id', element: <CollectionDetail />, handle: { title: 'Collection' } },
      { path: 'pipeline', element: <Pipeline />, handle: { title: 'Pipeline' } },
      { path: 'datasets', element: <AdminPage><Datasets /></AdminPage>, handle: { title: 'Datasets' } },
      { path: 'watchdog', element: <Watchdog />, handle: { title: 'Watchdogs' } },
      { path: 'watchdog/manage', element: <WatchdogManage />, handle: { title: 'Watchdogs · Manage' } },
      { path: 'watchdog/:id/edit', element: <WatchdogEdit />, handle: { title: 'Edit watchdog' } },
      { path: 'notifications', element: <Notifications />, handle: { title: 'Notifications' } },
      { path: 'dedup', element: <AdminPage><Dedup /></AdminPage>, handle: { title: 'Dedup' } },
      { path: 'settings', element: <AdminPage><Settings /></AdminPage>, handle: { title: 'Settings' } },
      { path: 'scrapers', element: <AdminPage><Scrapers /></AdminPage>, handle: { title: 'Scrapers' } },
      { path: 'dev/confidence-indicator', element: <AdminPage><DevConfidencePreview /></AdminPage>, handle: { title: 'Confidence indicator (dev)' } },
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
