/* The Browse page = the shared <BrowseExperience> driven by the URL adapter.
 * All the filter/map/data wiring lives in <BrowseExperience> so the same
 * surface also powers the "Explore area" modal (in-memory adapter). */
import BrowseExperience from '@/components/BrowseExperience';
import { useUrlBrowseState } from '@/lib/browseState';

export default function Browse() {
  const view = useUrlBrowseState();
  return <BrowseExperience view={view} layout="page" />;
}
