// Address-bar state for the single-page app: which page is open, the Guide week and an open channel.
// Pages are pushed onto browser history so Back and Forward move between them; the Guide week and
// channel are kept in the current entry (replaceState) so links can be shared without flooding history.
export const appViews = ['Home', 'Favourites', 'Watchlist', 'Discover', 'TV Guide', 'M3U Subscriptions', 'Subscriptions', 'Activity', 'Local Library', 'Settings', 'YouTube', 'Downloads', 'View Media'] as const;
export type AppView = typeof appViews[number];

// Canonical routes match their nav labels; the historical names stay accepted so old links keep working.
const viewAliases: Record<string, AppView> = { YouTube: 'Subscriptions', Downloads: 'Activity', 'View Media': 'Discover' };
export function canonicalView(view: AppView): AppView { return viewAliases[view] || view; }

const CHANNEL = /^UC[A-Za-z0-9_-]{22}$/;

function relative(url: URL) { return url.pathname + url.search + url.hash; }

export function viewFromSearch(search: string): AppView {
  const value = new URLSearchParams(search).get('view');
  const canonical = value ? canonicalView(value as AppView) : null;
  return canonical && (appViews as readonly string[]).includes(canonical) ? canonical : 'Home';
}

/** The `?section=` value used to deep-link a Settings category. */
export function settingsSection(search: string) {
  return new URLSearchParams(search).get('section');
}

export const youtubeCookiesUrl='/app/?view=Settings&section=YouTube#cookies';

export function withSettingsSection(href: string, section: string | null, anchor?:string) {
  const url = new URL(href);
  if (section) url.searchParams.set('section', section); else url.searchParams.delete('section');
  if(anchor!==undefined)url.hash=anchor;
  else if(section!=='YouTube'&&url.hash==='#cookies')url.hash='';
  return relative(url);
}

/** The `?q=` value used to deep-link a Discover search. */
export function mediaSearchQuery(search: string) {
  return new URLSearchParams(search).get('q');
}

/** URL after submitting the global search box; null when an empty search has nothing to clear. */
export function mediaSearchUrl(href: string, text: string) {
  const url = new URL(href);
  const query = text.trim();
  if (!query && !url.searchParams.has('q')) return null;
  url.searchParams.set('view', 'Discover');
  url.searchParams.delete('filter');
  if (query) url.searchParams.set('q', query); else url.searchParams.delete('q');
  return url.pathname + url.search + url.hash;
}

/** URL for a page. The Guide week belongs to the Guide page, so it is dropped when the page changes. */
export function viewUrl(href: string, view: AppView) {
  const url = new URL(href);
  const canonical = canonicalView(view);
  if (canonical === 'Home') url.searchParams.delete('view'); else url.searchParams.set('view', canonical);
  url.searchParams.delete('date');
  if (canonical !== 'Settings') {url.searchParams.delete('section');if(url.hash==='#cookies')url.hash='';}
  if (canonical !== 'Discover') url.searchParams.delete('q');
  url.searchParams.delete('filter');
  return relative(url);
}

type HistoryLike = { state: unknown; pushState(data: unknown, unused: string, url: string): void; replaceState(data: unknown, unused: string, url: string): void };

/** Record a page change: a new history entry for a different page, an in-place update for the same one. */
export function recordView(view: AppView, place: { href: string; search: string } = location, log: HistoryLike = history) {
  const url = viewUrl(place.href, view);
  if (viewFromSearch(place.search) === view) log.replaceState(log.state, '', url); else log.pushState(null, '', url);
}

/** A real calendar date in YYYY-MM-DD form, or null. */
export function guideDate(search: string) {
  const value = new URLSearchParams(search).get('date');
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const parsed = new Date(value + 'T12:00:00Z');
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? value : null;
}

export function withGuideDate(href: string, date: string | null) {
  const url = new URL(href);
  if (date) url.searchParams.set('date', date); else url.searchParams.delete('date');
  return relative(url);
}

/** Opens the shell's merged Add-download dialog; detail selects the tab. */
export type AddDownloadKind = 'torrent' | 'nzb' | 'youtube' | 'strm';
export const addDownloadEvent = 'reeldock-add-download';

/** A YouTube channel id (UC + 22 characters), or null. */
export function channelParam(search: string) {
  const value = new URLSearchParams(search).get('channel');
  return value && CHANNEL.test(value) ? value : null;
}

export function withChannel(href: string, id: string | null) {
  const url = new URL(href);
  if (id && CHANNEL.test(id)) url.searchParams.set('channel', id); else url.searchParams.delete('channel');
  return relative(url);
}
