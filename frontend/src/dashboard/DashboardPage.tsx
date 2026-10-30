import type { AppView } from '../appNavigation';
import { lazy, Suspense } from 'react';
import LoadingStatus from '../components/LoadingStatus';
import HomeShortcuts from './HomeShortcuts';
import type { ReadyDashboard } from './types';

const imports = {
  favourites: () => import('../collections/Favourites'),
  watchlist: () => import('../collections/Watchlist'),
  media: () => import('../viewMedia/ViewMedia'),
  library: () => import('../LibraryPages'),
  guide: () => import('../LiveGuide'),
  vod: () => import('../M3uLibrary'),
};
const Favourites = lazy(imports.favourites);
const Watchlist = lazy(imports.watchlist);
const ViewMedia = lazy(imports.media);
const Channels = lazy(() => imports.library().then(module => ({ default: module.Channels })));
const DownloadList = lazy(() => imports.library().then(module => ({ default: module.DownloadList })));
const LiveGuide = lazy(imports.guide);
const M3uLibrary = lazy(imports.vod);

/** Warm only the chosen page's code on navigation intent, never its data or engines. */
export function preloadPage(view: AppView) {
  const load = view === 'Favourites' ? imports.favourites : view === 'Watchlist' ? imports.watchlist :
    view === 'Discover' || view === 'Local Library' ? imports.media :
    ['Subscriptions', 'YouTube', 'Downloads', 'Activity'].includes(view) ? imports.library :
    view === 'TV Guide' ? imports.guide : view === 'M3U Subscriptions' ? imports.vod : null;
  if (load) void load().catch(() => { /* The actual page load owns failure feedback. */ });
}

type Props = {
  view: AppView;
  ready: ReadyDashboard;
  revision: number;
  navigate: (view: AppView) => void;
};

/** Select the active page, preserving each page's existing refresh/remount behaviour. */
export default function DashboardPage({ view, ready, revision, navigate }: Props) {
  return <Suspense fallback={<LoadingStatus/>}><Page view={view} ready={ready} revision={revision} navigate={navigate} /></Suspense>;
}

function Page({ view, ready, revision, navigate }: Props) {
  switch (view) {
    case 'Watchlist':
      return <Watchlist csrf={ready.csrf}/>;
    case 'Favourites':
      return <Favourites csrf={ready.csrf}/>;
    case 'Discover':
      return <ViewMedia csrf={ready.csrf} initial={ready.discover_initial?.viewMedia || undefined} />;
    case 'M3U Subscriptions':
      return <M3uLibrary key={revision} />;
    case 'TV Guide':
      return <LiveGuide key={revision} csrf={ready.csrf} />;
    case 'YouTube':
    case 'Subscriptions':
      return <Channels key={revision} csrf={ready.csrf} />;
    case 'Downloads':
    case 'Activity':
      return <DownloadList key={revision} csrf={ready.csrf} />;
    case 'Local Library':
      return <ViewMedia key={revision} csrf={ready.csrf} initialSource="local" initialMedia="shows" sourceLocked />;
    case 'Settings':
      return null;
    case 'Home':
      return <HomeShortcuts ready={ready} navigate={navigate} />;
  }
}
