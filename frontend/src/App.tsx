import { useLocation, useRoutes } from 'react-router-dom';
import { routes } from './routes';
import ErrorBoundary from './components/ErrorBoundary';
import { TitleController } from './lib/pageTitle';

export default function App() {
  const element = useRoutes(routes);
  const location = useLocation();
  // Last-resort net. Shell wraps the ROUTE BODY in its own boundary, so a page
  // crash keeps the nav, footer and toasts; this one catches what renders
  // outside that body — TopBar, Footer, ToastViewport, and the Explore-area
  // modal (its provider sits outside <main>). Still keyed on pathname: that is
  // what lets a crash here auto-recover on the next navigation instead of
  // stranding the user on the fallback until a manual reload.
  return (
    <TitleController routes={routes}>
      <ErrorBoundary key={location.pathname} label="app">
        {element}
      </ErrorBoundary>
    </TitleController>
  );
}
