import { useLocation, useRoutes } from 'react-router-dom';
import { routes } from './routes';
import ErrorBoundary from './components/ErrorBoundary';
import { TitleController } from './lib/pageTitle';

export default function App() {
  const element = useRoutes(routes);
  const location = useLocation();
  // Key on pathname so a crashed route auto-recovers on the next navigation
  // instead of stranding the user on the fallback until a manual reload.
  return (
    <TitleController routes={routes}>
      <ErrorBoundary key={location.pathname} label="route">
        {element}
      </ErrorBoundary>
    </TitleController>
  );
}
