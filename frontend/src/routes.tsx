import { Suspense, type ReactNode } from 'react';
import { lazyChunk } from '@/lib/lazyChunk';
import { ROUTES } from '@/lib/routes';
import { Link, Navigate, type RouteObject } from 'react-router-dom';
import Shell from './components/Shell';
import Skeleton from './components/Skeleton';
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
const Health = lazyChunk(() => import('./pages/Health'));
const Costs = lazyChunk(() => import('./pages/Costs'));
const Scrapers = lazyChunk(() => import('./pages/Scrapers'));
const Settings = lazyChunk(() => import('./pages/Settings'));
const Datasets = lazyChunk(() => import('./pages/Datasets'));
const Outreach = lazyChunk(() => import('./pages/Outreach'));
const OutreachDetail = lazyChunk(() => import('./pages/OutreachDetail'));
const BrokerReview = lazyChunk(() => import('./pages/BrokerReview'));
const LocationQuality = lazyChunk(() => import('./pages/LocationQuality'));
const NewDedupDashboard = lazyChunk(() => import('./pages/NewDedupDashboard'));
const NewDedupSettings = lazyChunk(() => import('./pages/NewDedupSettings'));
const NewDedupLabeling = lazyChunk(() => import('./pages/NewDedupLabeling'));
const NewDedupTrainingSet = lazyChunk(() => import('./pages/NewDedupTrainingSet'));
const NewDedupExam = lazyChunk(() => import('./pages/NewDedupExam'));
const NewDedupExamReview = lazyChunk(() => import('./pages/NewDedupExamReview'));
const NewDedupTaxonomy = lazyChunk(() => import('./pages/NewDedupTaxonomy'));
// TODO(estimation-5 Part C1): remove DevConfidencePreview + its route
// once design is approved and the indicator is in real use.
const DevConfidencePreview = lazyChunk(() => import('./pages/DevConfidencePreview'));

function AdminPage({ children }: { children: ReactNode }) {
  return (
    <RequireAdmin>
      {/* An admin page IS the whole route body — `null` would render a blank
        * page while its chunk loads, and a stale chunk holds this fallback for
        * the length of the recovery reload. */}
      <Suspense fallback={<Skeleton height={320} className="mx-6 my-8" />}>
        {children}
      </Suspense>
    </RequireAdmin>
  );
}

export const routes: RouteObject[] = [
  // Full-page auth screens (outside the app Shell, so they stay reachable
  // while logged out — everything under the Shell requires a session).
  { path: ROUTES.login.pattern, element: <Login />, handle: { title: 'Sign in' } },
  { path: ROUTES.forgotPassword.pattern, element: <ForgotPassword />, handle: { title: 'Reset password' } },
  { path: ROUTES.resetPassword.pattern, element: <UpdatePassword />, handle: { title: 'New password' } },
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
      { index: true, element: <Navigate to={ROUTES.browse.build()} replace /> },
      { path: ROUTES.browse.childPath, element: <Browse />, handle: { title: 'Browse' } },
      // Bare /listing handles the ?property=ID query form (Browse merge links
      // use it); ListingDetail resolves it to the property's representative
      // listing and redirects to /listing/:id.
      { path: ROUTES.listing.childPath, element: <ListingDetail />, handle: { title: 'Listing' } },
      // Canonical natural-key form (migration 091). ListingDetail redirects the
      // legacy numeric route below to this one so no negative synthetic id
      // (migration 097) is ever shown in the URL bar.
      { path: ROUTES.listingCanonical.childPath, element: <ListingDetail />, handle: { title: 'Listing' } },
      // Legacy/resolver form, kept forever: positive → sreality's real id,
      // negative → frozen pre-cutover alias; also the target of every deep link
      // ever sent before the natural-key cutover.
      { path: ROUTES.listingLegacy.childPath, element: <ListingDetail />, handle: { title: 'Listing' } },
      { path: ROUTES.health.childPath, element: <AdminPage><Health /></AdminPage>, handle: { title: 'Health' } },
      { path: ROUTES.costs.childPath, element: <AdminPage><Costs /></AdminPage>, handle: { title: 'LLM costs' } },
      { path: 'estimate', element: <Navigate to={ROUTES.estimations.build()} replace /> },
      { path: ROUTES.estimations.childPath, element: <EstimationList />, handle: { title: 'Estimations' } },
      { path: ROUTES.estimationDetail.childPath, element: <EstimationDetail />, handle: { title: 'Estimation' } },
      { path: ROUTES.brokers.childPath, element: <Brokers />, handle: { title: 'Brokers' } },
      { path: ROUTES.brokersReview.childPath, element: <AdminPage><BrokerReview /></AdminPage>, handle: { title: 'Brokers · Review' } },
      { path: ROUTES.brokerDetail.childPath, element: <BrokerDetail />, handle: { title: 'Broker' } },
      { path: ROUTES.outreach.childPath, element: <AdminPage><Outreach /></AdminPage>, handle: { title: 'Outreach' } },
      { path: ROUTES.outreachDetail.childPath, element: <AdminPage><OutreachDetail /></AdminPage>, handle: { title: 'Campaign' } },
      { path: ROUTES.buildingDetail.childPath, element: <BuildingDetail />, handle: { title: 'Building' } },
      { path: ROUTES.collections.childPath, element: <Collections />, handle: { title: 'Collections' } },
      { path: ROUTES.collectionDetail.childPath, element: <CollectionDetail />, handle: { title: 'Collection' } },
      { path: ROUTES.pipeline.childPath, element: <Pipeline />, handle: { title: 'Pipeline' } },
      { path: ROUTES.datasets.childPath, element: <AdminPage><Datasets /></AdminPage>, handle: { title: 'Datasets' } },
      { path: ROUTES.watchdog.childPath, element: <Watchdog />, handle: { title: 'Watchdogs' } },
      { path: ROUTES.watchdogManage.childPath, element: <WatchdogManage />, handle: { title: 'Watchdogs · Manage' } },
      { path: ROUTES.watchdogEdit.childPath, element: <WatchdogEdit />, handle: { title: 'Edit watchdog' } },
      { path: ROUTES.notifications.childPath, element: <Notifications />, handle: { title: 'Notifications' } },
      { path: ROUTES.locationQuality.childPath, element: <AdminPage><LocationQuality /></AdminPage>, handle: { title: 'Location quality' } },
      { path: ROUTES.settings.childPath, element: <AdminPage><Settings /></AdminPage>, handle: { title: 'Settings' } },
      { path: ROUTES.newDedup.childPath, element: <AdminPage><NewDedupDashboard /></AdminPage>, handle: { title: 'NEW DEDUP' } },
      { path: ROUTES.newDedupSettings.childPath, element: <AdminPage><NewDedupSettings /></AdminPage>, handle: { title: 'NEW DEDUP · Settings' } },
      { path: ROUTES.newDedupLabeling.childPath, element: <AdminPage><NewDedupLabeling /></AdminPage>, handle: { title: 'NEW DEDUP · Labeling' } },
      { path: ROUTES.newDedupTaxonomy.childPath, element: <AdminPage><NewDedupTaxonomy /></AdminPage>, handle: { title: 'NEW DEDUP · Taxonomy' } },
      { path: ROUTES.newDedupTrainingSet.childPath, element: <AdminPage><NewDedupTrainingSet /></AdminPage>, handle: { title: 'NEW DEDUP · Training set' } },
      { path: ROUTES.newDedupExam.childPath, element: <AdminPage><NewDedupExam /></AdminPage>, handle: { title: 'NEW DEDUP · Exam' } },
      { path: ROUTES.newDedupExamReview.childPath, element: <AdminPage><NewDedupExamReview /></AdminPage>, handle: { title: 'NEW DEDUP · Exam review' } },
      { path: ROUTES.scrapers.childPath, element: <AdminPage><Scrapers /></AdminPage>, handle: { title: 'Scrapers' } },
      { path: ROUTES.devConfidenceIndicator.childPath, element: <AdminPage><DevConfidencePreview /></AdminPage>, handle: { title: 'Confidence indicator (dev)' } },
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
      {/* The host serves the SPA at every path depth, so a mistyped or drifted
        * URL lands here with an HTTP 200 rather than a server 404 — this was a
        * dead end with nothing to click. */}
      <Link
        to={ROUTES.browse.build()}
        className="inline-block mt-5 text-sm text-[var(--color-copper)] hover:text-[var(--color-copper-2)] underline underline-offset-4"
      >
        Back to Browse
      </Link>
    </div>
  );
}
