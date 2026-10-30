// Declarative action schema for the universal popup standard.
// Every media surface (tile, hero, dialog header, Manage entry row) resolves
// its controls through actionsFor() so labels, order and visibility are
// identical everywhere. Rendering a menu must stay side-effect free —
// no enrolment, searches or engine construction on mount.

export type ActionId =
 'favourite' | 'watchlist' | 'options' | 'manage' |
 'settings' | 'correctTitle' | 'repairMetadata' | 'findBetterQuality' |
 'compareReleases' | 'searchNow' | 'download' | 'cancelAcquisition' |
 'resumeAcquisition' | 'history' | 'removeEntry' | 'deleteFile' |
 'restoreEntry' | 'fixMatch' | 'downloadServer' | 'downloadStrm' |
 'downloadBrowser' | 'subscribe';

// What the popup's subject looks like. Built from the item being shown —
// episodes, saved files, active transfers and removed entries change which
// actions exist; they never change WHERE the controls render.
export type PopupContext = {
 kind: 'movie' | 'show' | 'episode' | 'video' | 'channel' | 'vod' | 'group' | 'unknown';
 file?: boolean;          // a saved library file exists
 active?: boolean;        // an acquisition/transfer is running
 hold?: boolean;          // acquisition is held (explicit Resume only)
 removed?: boolean;       // entry sits in the removed filter
 automatic?: boolean;     // automatic/future rules allow acquisition
 metadataReady?: boolean; // saved file already has nfo + poster + fanart
};

export type ActionSpec = {
 id: ActionId;
 // Static label, or a function for kind-specific names (Movie settings etc).
 label: string | ((ctx: PopupContext) => string);
 // collection controls lead; settings & library ops follow;
 // danger is always last.
 group: 'collection' | 'acquisition' | 'settings' | 'library' | 'information' | 'danger';
 // Button variant — entries default to secondary; primary marks the
 // recommended action (Search now / Download); danger for destructive commits.
 variant?: 'default' | 'secondary' | 'danger';
 placement: 'header' | 'entry';
 visible: (ctx: PopupContext) => boolean;
 enabled?: (ctx: PopupContext) => boolean;
 disabledReason?: (ctx: PopupContext) => string | undefined;
};

// Canonical order for the header action row and for Manage entry rows.
// Manage is deliberately last: "…Options, Manage" reads right-to-left as the
// deepest control, matching the approved top-right placement.
const ORDER: ActionId[] = [
 'favourite', 'watchlist', 'options', 'subscribe',
 'settings', 'correctTitle', 'repairMetadata',
 'compareReleases', 'findBetterQuality', 'searchNow', 'download',
 'downloadServer', 'downloadStrm', 'downloadBrowser',
 'cancelAcquisition', 'resumeAcquisition',
 'history', 'fixMatch', 'restoreEntry', 'removeEntry', 'deleteFile', 'manage',
];

const owned = (ctx: PopupContext) => !!ctx.file;
const managed = (ctx: PopupContext) => ctx.kind !== 'video';

const settingsLabel = (ctx: PopupContext) =>
 ctx.kind === 'movie' ? 'Movie settings' : ctx.kind === 'episode' ? 'Episode settings' : ctx.kind === 'show' ? 'Series settings' : 'Video settings';

export const ACTIONS: ActionSpec[] = [
 // Header row — the per-tile menu (visibility decides, never placement).
 { id: 'favourite', label: 'Favourite', group: 'collection', placement: 'header',
   visible: ctx => ctx.kind !== 'group' },
 { id: 'watchlist', label: 'Watchlist', group: 'collection', placement: 'header',
   // Episodes never carry a Watchlist control — only their parent show.
   visible: ctx => ctx.kind !== 'group' && ctx.kind !== 'episode' },
 { id: 'options', label: 'Options', group: 'settings', placement: 'header',
   // Episodes route through Manage settings — no episode-level Options.
   visible: ctx => ['movie', 'show', 'channel', 'vod'].includes(ctx.kind) },
 { id: 'subscribe', label: 'Subscribe', group: 'collection', placement: 'header',
   visible: ctx => ctx.kind === 'channel' },
 { id: 'fixMatch', label: 'Fix match', group: 'settings', placement: 'header',
   visible: ctx => ctx.kind === 'vod' },
 { id: 'downloadServer', label: 'Download to server', group: 'acquisition', placement: 'header',
   visible: ctx => ctx.kind === 'vod' && !ctx.file },
 { id: 'downloadStrm', label: 'Download .strm', group: 'acquisition', placement: 'header',
   visible: ctx => ctx.kind === 'vod' },
 { id: 'downloadBrowser', label: 'Download video', group: 'acquisition', placement: 'header',
   visible: ctx => ctx.kind === 'vod' || ctx.kind === 'video' },
 { id: 'manage', label: 'Manage', group: 'library', placement: 'header',
   visible: ctx => ctx.kind !== 'group' && ctx.kind !== 'channel' },

 // Manage entry rows — per-item commands inside the management view.
 // File/acquisition ops hide entirely while a transfer is active; the
 // destructive pair stay visible but disabled with a reason.
 { id: 'settings', label: settingsLabel, group: 'settings', placement: 'entry',
   visible: ctx => !ctx.removed },
 { id: 'correctTitle', label: 'Correct title', group: 'settings', placement: 'entry',
   visible: ctx => owned(ctx) && managed(ctx) && !ctx.active && !ctx.removed },
 { id: 'repairMetadata', label: 'Repair metadata', group: 'settings', placement: 'entry',
   visible: ctx => owned(ctx) && !ctx.metadataReady && !ctx.active && !ctx.removed },
 { id: 'findBetterQuality', label: 'Find better quality', group: 'acquisition', placement: 'entry',
   visible: ctx => owned(ctx) && managed(ctx) && !ctx.active && !ctx.removed },
 { id: 'compareReleases', label: 'Compare releases', group: 'acquisition', placement: 'entry',
   visible: ctx => !ctx.file && managed(ctx) && !ctx.active && !ctx.removed },
 { id: 'searchNow', label: 'Search now', group: 'acquisition', variant: 'default', placement: 'entry',
   visible: ctx => !ctx.file && managed(ctx) && !!ctx.automatic && !ctx.active && !ctx.removed },
 { id: 'download', label: 'Download', group: 'acquisition', variant: 'default', placement: 'entry',
   visible: ctx => !ctx.file && ctx.kind === 'video' && !!ctx.automatic && !ctx.active && !ctx.removed },
 { id: 'cancelAcquisition', label: 'Cancel acquisition', group: 'acquisition', placement: 'entry',
   visible: ctx => !!ctx.active },
 { id: 'resumeAcquisition', label: 'Resume acquisition', group: 'acquisition', placement: 'entry',
   visible: ctx => !!ctx.hold && !ctx.removed },
 { id: 'history', label: 'History', group: 'information', placement: 'entry',
   visible: ctx => !ctx.removed },
 { id: 'restoreEntry', label: 'Restore entry', group: 'library', placement: 'entry',
   visible: ctx => !!ctx.removed },
 { id: 'removeEntry', label: 'Remove entry', group: 'danger', placement: 'entry',
   visible: ctx => !ctx.removed,
   enabled: ctx => !ctx.active,
   disabledReason: ctx => ctx.active ? 'Stop the active download first.' : undefined },
 { id: 'deleteFile', label: 'Delete file', group: 'danger', placement: 'entry',
   visible: ctx => owned(ctx) && !ctx.removed,
   enabled: ctx => !ctx.active,
   disabledReason: ctx => ctx.active ? 'Stop the active download first.' : undefined },
];

const rank = (id: ActionId) => { const i = ORDER.indexOf(id); return i < 0 ? ORDER.length : i; };

/** Actions for a surface, already visibility-filtered and canonically ordered. */
export function actionsFor(ctx: PopupContext, placement: 'header' | 'entry'): ActionSpec[] {
 return ACTIONS
  .filter(spec => spec.placement === placement && spec.visible(ctx))
  .sort((a, b) => rank(a.id) - rank(b.id));
}

export function actionLabel(spec: ActionSpec, ctx: PopupContext): string {
 return typeof spec.label === 'function' ? spec.label(ctx) : spec.label;
}

export function actionEnabled(spec: ActionSpec, ctx: PopupContext): boolean {
 return spec.enabled ? spec.enabled(ctx) : true;
}

export function actionDisabledReason(spec: ActionSpec, ctx: PopupContext): string | undefined {
 return spec.disabledReason ? spec.disabledReason(ctx) : undefined;
}
