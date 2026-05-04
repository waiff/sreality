import { useRoutes } from 'react-router-dom';
import { routes } from './routes';
import PasswordGate from './components/PasswordGate';

export default function App() {
  const element = useRoutes(routes);
  return <PasswordGate>{element}</PasswordGate>;
}
