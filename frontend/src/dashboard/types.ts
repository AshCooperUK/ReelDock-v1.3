import type { DiscoverPreview } from '../Discover';
import type { GlanceSummary } from './AtAGlance';
import type { GoogleApiState } from '../GoogleApiStatus';

// Data returned by /api/dashboard; shared by the shell and its page components.
export type M3uStats = {
  playlists: number;
  movies: number;
  shows: number | null;
  episodes: number;
  dated_tv: number;
  live_playlists: number;
  unsorted: number;
};

export type Overview = {
  google_api?: GoogleApiState;
  m3u?: M3uStats;
  version: string;
  yt_dlp_version: string;
  worker_online: boolean;
  channels: number;
  videos: number;
  favourites: number;
  downloads: Record<string, number>;
  media_disk: { total: number; used: number; free: number } | null;
  data_disk?: { total: number; used: number; free: number } | null;
  storage_disks?: {
    id: string;
    label: string;
    paths: string[];
    total: number | null;
    used: number | null;
    free: number | null;
  }[];
  storage_disks_incomplete?: boolean;
  storage_configuration_warnings?: string[];
  downloads_paused: boolean;
};

export type Dashboard =
  | { state: 'setup' | 'sign_in'; continue_url: string }
  | {
      state: 'ready';
      overview: Overview;
      glance?: GlanceSummary;
      discover_initial?: DiscoverPreview;
      m3u_sources: number;
      csrf: string;
      sync_all?: { busy: boolean; message: string };
    };

export type ReadyDashboard = Extract<Dashboard, { state: 'ready' }>;
