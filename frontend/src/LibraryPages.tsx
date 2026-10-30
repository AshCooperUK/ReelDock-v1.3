import { Button as ReelDockButton } from './components/ui';
import { UnstyledButton as ControlTrigger } from '@mantine/core';
import { NotificationMessage } from './notifications';
import { mediaDate } from './mediaDate';
import ChannelCard, { type ChannelSort, type ChannelView, ChannelViewSelector, channelSorts, sortChannels, favouriteChannels } from './ChannelViews';
import { refreshDownloads, downloadLabel } from './downloadFeed';
import { type Job } from './M3uDownloads';
import LiveArtwork from './LiveArtwork';
import { useDownloadHistory, type ActivityHistoryRow, type HistoryRow, type SyncItem } from './DownloadHistory';
import ChannelIdentity from './ChannelIdentity';
import { cloneElement, Fragment, isValidElement, lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ActionIcon, Badge, Divider, Group, Loader, Menu, Paper, Progress, SimpleGrid, Stack, Text } from '@mantine/core';
import { SelectField as NativeSelect } from './components/SelectField';
import { useMediaQuery } from '@mantine/hooks';
import { ArrowDownToLine, ArrowRight, Ban, CircleCheck, CircleX, EllipsisVertical, FolderOpen, Film, Info, ListOrdered, Search, Tv } from 'lucide-react';
import { Button, Dialog, DialogContent, DialogDescription, DialogTitle, Switch } from './components/ui';
import ListRow, { ActivityQueueRows } from './components/ListRow';
import ActivityGroup, { type ActivityGroupId } from './components/ActivityGroup';
import { ConfirmInline } from './components/DetailSheet';
import { ActivityStatus as DownloadState, activityStatusColour } from './components/ActivityStatus';
import { FilterBar, MediaGrid } from './components/browse';
import { EmptyState } from './components/states';
import LoadingStatus from './components/LoadingStatus';
import { createWarmSnapshots } from './warmSnapshots';
import TileActions from './collections/TileActions';
import ActivityMediaTitle from './ActivityMedia';
import { MediaHover } from './DownloadActivityRow';
import type { DiscoverItem } from './Discover';
export type LibraryView = 'YouTube' | 'Downloads' | 'Local Library';
import { diskSize, type Subscription as Channel } from './ChannelOptions';
const channelSnapshots=createWarmSnapshots<{items:Channel[];more:boolean}>();
if(typeof window!=='undefined')channelSnapshots.watch(window,['reeldock-channel-changed','reeldock-channel-removed','reeldock-watchlist-changed','reeldock-library-changed','reeldock-display-snapshot-changed']);
type Video = {
    channel_info?: {
        banner?: string;
    };
    id: string;
    title: string;
    channel_name?: string;
    channel?: string;
    state?: string;
    progress?: number;
    message?: string;
    description?: string;
    bytes?: number;
    video?: {
        thumbnail?: string;
        description?: string;
        date?: string;
        time?: string;
        duration?: string;
    };
    avatar?: string;
};
import { api } from './api';
import { usePageSearch } from './pageSearch';
import { useTorrentActivity } from './TorrentDownloads';
import { useNzbActivity } from './NzbDownloads';
import { exportActivityLog, redactActivityText, saveActivityLog, type ActivityLogEntry } from './activityLog';
import { activityClearSignature, activityEntryCleared, captureActivityClear, finishedActivityAttempt, type CapturedActivityClear, type ActivityClearAcknowledgement, type ActivityClearItemResult, type ActivityClearResult, type ActivityClearSection, type PendingActivityClear } from './activityClear';
import type { SourceChoice } from './SourceAlternatives';
const BulkChannelOptions = lazy(() => import('./BulkChannelOptions'));
const M3uDetails = lazy(() => import('./M3uLibrary').then(module => ({ default: module.M3uDetails })));
const VideoSheet = lazy(() => import('./VideoSheet'));
const VideoDownloadControls = lazy(() => import('./VideoDownloadControls'));
const SourceAlternatives = lazy(() => import('./SourceAlternatives'));
const DiscoveryTitle = lazy(() => import('./discovery/DiscoveryTitle'));
export { ActivityStatus as DownloadState } from './components/ActivityStatus';
function message(error: unknown) { return error instanceof Error ? error.message : 'Please try again.'; }
function Empty({ title, children }: {
    title: string;
    children: string;
}) { return <EmptyState title={title} icon={<FolderOpen size={30}/>}>{children}</EmptyState>; }
function Warning({ text, retry }: {
    text: string;
    retry?: () => void;
}) { return <NotificationMessage kind="error" message={text} actions={<> {retry && <Button variant="secondary" onClick={retry}>Try again</Button>} </>}/>; }
function More({ busy, more, error, load }: {
    busy: boolean;
    more: boolean;
    error: string;
    load: () => void;
}) {
    const target = useRef<HTMLDivElement>(null);
    useEffect(() => {
        if (!more || busy || error || !target.current)
            return;
        const observer = new IntersectionObserver(entries => { if (entries.some(e => e.isIntersecting))
            load(); }, { rootMargin: '250px' });
        observer.observe(target.current);
        return () => observer.disconnect();
    }, [more, busy, error, load]);
    return <div ref={target} role="status">{busy ? 'Loading…' : more && !error ? 'Scroll for more' : ''}</div>;
}
export function Channels({ csrf }: {
    csrf: string;
}) {
    const [view, setView] = useState<ChannelView>('compact');
    const search=usePageSearch('Subscriptions');
    const [query, setQuery] = useState('');
    const [sort, setSort] = useState<ChannelSort>('favourites');
    const [favouritesOnly, setFavouritesOnly] = useState(false);
    useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 300); return () => clearTimeout(timer); }, [search]);
    return <Stack component="section" gap="md"><FilterBar sorts={channelSorts} sort={sort} onSort={value => setSort(value as ChannelSort)} sortLabel="Sort channels" extra={<><Switch showLabel label="Favourites only" checked={favouritesOnly} onCheckedChange={setFavouritesOnly}/><ChannelViewSelector csrf={csrf} view={view} onChange={setView}/></>}/><ChannelResults key={JSON.stringify([csrf,query,sort,favouritesOnly])} query={query} csrf={csrf} view={view} sort={sort} favouritesOnly={favouritesOnly}/></Stack>;
}
function ChannelResults({ query, csrf, view, sort, favouritesOnly }: {
    query: string;
    csrf: string;
    view: ChannelView;
    sort: ChannelSort;
    favouritesOnly: boolean;
}) {
    const scope=JSON.stringify([query,sort,favouritesOnly]);
    const snapshot=useMemo(()=>channelSnapshots.open(csrf,scope),[csrf,scope]);
    const warm=useMemo(()=>snapshot.read(),[snapshot]);
    const [selected, setSelected] = useState<Set<string>>(new Set());
    const toggleSelection = (id: string) => setSelected(old => { const next = new Set(old); if (next.has(id))
        next.delete(id);
    else
        next.add(id); return next; });
    const [items, setItems] = useState<Channel[]>(()=>warm?.items||[]);
    const [more, setMore] = useState(warm?.more??true);
    const [busy, setBusy] = useState(true);
    const [error, setError] = useState('');
    const [saving, setSaving] = useState('');
    const [actionError, setActionError] = useState('');
    const scrollPosition = useRef<{
        top: number;
        left: number;
    } | null>(null);
    useLayoutEffect(() => { const position = scrollPosition.current; if (position) {
        scrollPosition.current = null;
        window.scrollTo({ ...position, behavior: 'instant' });
    } }, [items]);
    const offset = useRef(warm?.items.length||0), loading = useRef(false), refreshPending = useRef(false), controller = useRef(new AbortController());
    const remember=(rows:Channel[],hasMore:boolean)=>snapshot.write({items:rows.slice(0,15),more:hasMore||rows.length>15});
    const load = useCallback(async (refresh = false) => {
        if (loading.current) {
            if (refresh)
                refreshPending.current = true;
            return;
        }
        loading.current = true;
        setBusy(true);
        setError('');
        const signal = controller.current.signal;
        if(signal.aborted){loading.current=false;return;}
        const requestSnapshot=channelSnapshots.open(csrf,scope);
        try {
            const target = refresh ? Math.max(offset.current, 15) : offset.current + 15;
            let cursor = refresh ? 0 : offset.current, hasMore = true;
            const incoming: Channel[] = [];
            do {
                const result = await api<{
                    items: Channel[];
                    more: boolean;
                }>('/api/subscriptions?q=' + encodeURIComponent(query) + '&offset=' + cursor + (sort==='favourites'?'':'&sort='+sort) + (favouritesOnly?'&favourites=1':''), signal);
                if (signal.aborted)
                    return;
                incoming.push(...result.items);
                cursor += result.items.length;
                hasMore = result.more;
                if (!result.items.length)
                    break;
            } while (refresh && hasMore && cursor < target);
            if(!requestSnapshot.valid()){refreshPending.current=requestSnapshot.currentAccount();return;}
            offset.current = cursor;
            // Preserve the current position for appended pages as well as reordered favourites.
            scrollPosition.current = { top: window.scrollY, left: window.scrollX };
            setItems(old => {if(!requestSnapshot.valid())return old;const next=refresh?incoming:[...old,...incoming.filter(item=>!old.some(saved=>saved.id===item.id))];requestSnapshot.write({items:next.slice(0,15),more:hasMore||next.length>15});return next;});
            setMore(hasMore);
        }
        catch (e) {
            if (!signal.aborted)
                setError(message(e));
        }
        finally {
            if (!signal.aborted) {
                loading.current = false;
                setBusy(false);
                if (refreshPending.current) {
                    refreshPending.current = false;
                    void load(true);
                }
            }
        }
    }, [query,csrf,scope,sort,favouritesOnly]);
    useEffect(() => { controller.current = new AbortController(); loading.current = false; void load(!!warm); return () => controller.current.abort(); }, [load]);
    useEffect(() => { const update = (event: Event) => { const item = (event as CustomEvent<Channel>).detail; setItems(old => {const next=old.map(saved=>item&&saved.id===item.id?{...saved,...item}:saved);remember(next,more);return next;}); if (item)
        void load(true); }; const remove = (event: Event) => { const id = (event as CustomEvent<string>).detail; setItems(old => {const next=old.filter(item=>item.id!==id);remember(next,more);return next;}); offset.current = Math.max(0, offset.current - 1);void load(true); }; window.addEventListener('reeldock-channel-changed', update); window.addEventListener('reeldock-channel-removed', remove); return () => { window.removeEventListener('reeldock-channel-changed', update); window.removeEventListener('reeldock-channel-removed', remove); }; }, [load,more]);
    useEffect(() => { const changed = (event: Event) => { const key = (event as CustomEvent<{
        key?: string;
    }>).detail?.key; if (!key || key.startsWith('UC'))
        void load(true); }; window.addEventListener('reeldock-watchlist-changed', changed); return () => window.removeEventListener('reeldock-watchlist-changed', changed); }, [load]);
    async function change(channel: Channel, key: 'favourite' | 'shown' | 'forecasts', value: boolean) {
        setSaving(channel.id);
        setActionError('');
        try {
            await api(key === 'favourite' ? '/api/channel-favourite/' + encodeURIComponent(channel.id) : '/api/subscriptions', controller.current.signal, key === 'favourite' ? { favourite: value } : { ids: [channel.id], [key]: value }, csrf);
            window.dispatchEvent(new Event('reeldock-display-snapshot-changed'));
            scrollPosition.current = { top: window.scrollY, left: window.scrollX };
            setItems(old => {const next=old.map(item=>item.id===channel.id?{...item,[key]:value}:item);remember(next,more);return next;});
            if (key === 'favourite')
                await load(true);
        }
        catch (e) {
            if (!controller.current.signal.aborted)
                setActionError(message(e));
        }
        finally {
            setSaving('');
        }
    }
    const shown = sortChannels(favouritesOnly ? items.filter(item => item.favourite) : items, sort);
    const firstLoad = busy && !items.length && !error;
    const ChannelGrid = view==='tile'?MediaGrid:Stack;
    return <>{actionError && <Warning text={actionError}/>}<ChannelGrid role="listbox" aria-multiselectable="true" aria-label="Channels">{shown.map(item => <ChannelCard key={item.id} item={item} view={view} saving={!!saving} selected={selected.has(item.id)} onSelect={() => toggleSelection(item.id)} onFavourite={() => void change(item, 'favourite', !item.favourite)}/>)}</ChannelGrid><LoadingStatus active={firstLoad} label="Loading channels…"/>{items.some(item => selected.has(item.id)) && <Suspense fallback={<Loader size="sm" aria-label="Loading channel options"/>}><BulkChannelOptions items={items.filter(item => selected.has(item.id))} csrf={csrf} onClear={() => { setSelected(new Set()); }} onChange={changed => { scrollPosition.current = { top: window.scrollY, left: window.scrollX }; setItems(old => {const next=old.map(item=>changed.find(updated=>updated.id===item.id)||item);remember(next,more);return next;}); void load(true); }}/></Suspense>}{!busy && !error && !items.length && <Empty title={favouritesOnly ? 'No favourite channels here' : query ? 'No matching channels' : 'No channels yet'}>{favouritesOnly ? 'Mark a channel as a favourite, or turn the filter off.' : query ? 'Try another channel name.' : 'Your channels will appear after your first YouTube import.'}</Empty>}{!busy && !error && !!items.length && !shown.length && <Empty title="No favourite channels here">Mark a channel as a favourite, or turn the filter off.</Empty>}{error && <Warning text={error} retry={() => void load()}/>}{!firstLoad && <More busy={busy} more={more} error={error} load={() => void load()}/>}</>;
}
function ActivityEventCard({ item, sync = false, clearAction, actionsDisabled }: {
    item: SyncItem;
    sync?: boolean;
    clearAction?: ReactNode;
    actionsDisabled?: boolean;
}) {
    const labels: Record<string, string> = { movies: 'Movies', series: 'TV episodes', tv: 'Dated TV', playlist: 'Live TV playlists', unsorted: 'Unsorted videos' };
    const changes = Object.entries(item.stats.changes || {});
    const identity = item.subscription + ' ' + item.title;
    const logo = /\bemby\b/i.test(identity) ? 'emby-icon.png' : sync || /\bm3u\b/i.test(identity) ? 'm3u-icon.svg' : /\byoutube\b/i.test(identity) ? 'youtube-activity-icon.svg' : 'reeldock-activity-icon.svg';
    const details = [
        item.message && <span key="message">{item.message}</span>,
        !!item.finished && <span key="date">Date: {mediaDate(new Date(item.finished * 1000).toISOString())}</span>,
        ...changes.map(([kind, count]) => <span key={kind}><strong>{labels[kind] || kind}</strong>{' — '}{count.added.toLocaleString('en-GB')} added · {count.removed.toLocaleString('en-GB')} removed{count.updated > 0 ? ` · ${count.updated.toLocaleString('en-GB')} updated` : ''}</span>),
        sync && item.state === 'complete' && !item.stats.changes && <span key="note">Media type breakdown was not recorded for this earlier sync.</span>,
    ].filter(Boolean);
    return <ListRow timestamp={activityDate(item)} media={<img src={"/preview/assets/" + logo} alt=""/>} title={<span>{item.title}</span>} badge={<DownloadState state={item.state === 'error' ? 'failed' : item.state}/>} statusColour={activityStatusColour(item.state)} meta={<span>ReelDock · {item.subscription}</span>} clearAction={clearAction} actionsDisabled={actionsDisabled} details={details.length ? details : undefined}/>;
}
function activityTime(item: object) {
    const value = item as {
        created?: number;
        finished?: number;
        updated?: number;
        added?: number;
    };
    return Number(value.created || value.finished || value.updated || value.added || 0);
}
function activityDate(item:{created?:number;updated?:number;finished?:number}){const stamp=Number(item.updated||item.finished||item.created||0);return stamp>0?mediaDate(new Date(stamp*1000).toISOString()):undefined;}

// Normalised display state for a history row (matches the DownloadState badge).
function rowState(row: ActivityHistoryRow) {
    const state = row.item.state || '';
    // Expected retention cleanup is a finished outcome; keep the stored expiry
    // and server-issued Clear observation intact for scheduling and history.
    if(row.kind==='youtube'&&state==='expired'&&/^Removed automatically: outside this channel[’']s download range\.$/.test(row.item.message||''))return 'removed';
    if(row.kind==='torrent')return (row.item.progress||0)>=1?'complete':state==='stopped'?'cancelled':['failed','error'].includes(state)?'failed':['paused','queued','choose'].includes(state)?'queued':'downloading';
    if(row.kind==='nzb')return state==='failed'?'failed':(row.item.progress||0)>=1&&state==='stopped'?'complete':['paused','queued','choose'].includes(state)?'queued':'downloading';
    return (row.kind === 'm3u_sync' || row.kind === 'event') ? (state === 'error' ? 'failed' : state) : state === 'running' ? 'downloading' : state;
}
// Activity filter tabs: state → tab mapping over the merged feed
// (history rows + torrent jobs + NZB jobs + active rows).
//   Downloading          : downloading, running, seeding, cancelling, searching, starting
//   Processing           : a real post-download phase, or awaiting placement
//   Queued               : queued, paused, choose (awaiting a Movies/TV pick), pending/unknown
//   Waiting for release  : waiting
//   Failed               : failed, error, cancelled, expired, stopped
//   Completed            : complete, skipped, removed (expected date-range cleanup)
//   All                  : everything
type ActivityFilter = 'downloading' | 'processing' | 'queued' | 'waiting' | 'failed' | 'completed' | 'all';
const ACTIVITY_FILTERS: {
    id: ActivityFilter;
    label: string;
}[] = [{ id: 'all', label: 'All' }, { id: 'downloading', label: 'Downloading' }, { id: 'processing', label: 'Processing' }, { id: 'queued', label: 'Queued' }, { id: 'waiting', label: 'Waiting for release' }, { id: 'failed', label: 'Failed' }, { id: 'completed', label: 'Completed' }];
function activityBucket(state: string, phase?: string): ActivityFilter {
    if (state === 'waiting')
        return 'waiting';
    if (['queued', 'paused', 'choose', 'pending'].includes(state))
        return 'queued';
    if (state === 'processing' || state === 'verifying' || ['downloading', 'running'].includes(state) && ['encoding', 'merging', 'sponsors', 'metadata', 'processing'].includes(phase || ''))
        return 'processing';
    if (['downloading', 'running', 'seeding', 'cancelling', 'searching', 'starting'].includes(state))
        return 'downloading';
    if (['failed', 'error', 'cancelled', 'expired', 'stopped'].includes(state))
        return 'failed';
    if (['complete', 'skipped', 'removed'].includes(state))
        return 'completed';
    return 'queued';
}
function orderActivity(a:{pin:number;at:number;id:string},b:{pin:number;at:number;id:string}){return (b.pin-a.pin)||b.at-a.at||(a.id<b.id?-1:1);}
export function activitySourceChoice(item:{id?:string|number;archived?:boolean;sourceChoice?:unknown}):SourceChoice|null{
    const value=item.sourceChoice as Partial<SourceChoice>|null;
    return !item.archived&&value?.eligible===true&&value.requestId===String(item.id)&&typeof value.attemptKey==='string'&&!!value.attemptKey?value as SourceChoice:null;
}
function nativeDialogOpen(node:ReactNode):boolean{
    if(Array.isArray(node))return node.some(nativeDialogOpen);
    return isValidElement<{open?:boolean;children?:ReactNode}>(node)&&Boolean(node.props.open===true||nativeDialogOpen(node.props.children));
}
/** The acquisition request and its actual transport are one displayed job.
 * Keep the request when its failed child has yielded to a new source search.
 * Archives and TXT export retain each source record independently. */
export function distinctActivityJobs<T extends {state?:string;data:{kind:string;item:{id?:string|number;state?:string;progress?:number|null;engine?:string;engine_id?:string;archived?:boolean;sourceChoice?:unknown}}}>(entries:readonly T[]):T[]{
    const transports=new Map<string,Map<string,T>>();
    for(const entry of entries){
        const {kind,item}=entry.data;
        if(!['torrent','nzb','youtube','m3u'].includes(kind)||item.archived||item.id==null)continue;
        if(!transports.has(kind))transports.set(kind,new Map());
        transports.get(kind)!.set(String(item.id),entry);
    }
    function childOf(entry:T){
        const {kind,item}=entry.data;
        if(kind!=='discover'||item.archived||typeof item.engine_id!=='string'||!item.engine_id)return;
        const transport=item.engine==='vod'?'m3u':['torrent','nzb','youtube'].includes(item.engine||'')?item.engine:'';
        return transport?transports.get(transport)?.get(item.engine_id):undefined;
    }
    const searchingParents=new Map<T,T>();
    for(const entry of entries){
        const child=childOf(entry);if(!child)continue;
        const state=child.state||child.data.item.state||'';
        const failed=['failed','error','cancelled','expired'].includes(state)||(state==='stopped'&&(child.data.item.progress||0)<1);
        if(failed&&(activitySourceChoice(entry.data.item)||['waiting','searching','starting'].includes(entry.state||entry.data.item.state||''))&&!searchingParents.has(child))searchingParents.set(child,entry);
    }
    return entries.filter(entry=>{
        if(searchingParents.has(entry))return false;
        const child=childOf(entry);
        return !child||searchingParents.get(child)===entry;
    });
}
export function DownloadList({ csrf }: {
    csrf: string;
    actionsTarget?: HTMLElement | null;
}) {
    const phone = useMediaQuery('(max-width: 47.99em)', false, { getInitialValueInEffect: false });
    const feed = useDownloadHistory(), torrents = useTorrentActivity(csrf,openProgramme), nzbs = useNzbActivity(csrf,openProgramme), [selection, setSelection] = useState<{
        kind: 'youtube';
        item: Video;
    } | {
        kind: 'm3u';
        item: Job;
    } | null>(null);
    const [programme,setProgramme]=useState<DiscoverItem|null>(null);
    function openProgramme(item:DiscoverItem){setSelection(null);setSourceChoice(null);setProgramme(item);}
    useEffect(()=>{setProgramme(null);},[csrf]);
    const [saving, setSaving] = useState(''), [actionError, setActionError] = useState(''), [confirmStop, setConfirmStop] = useState(false), [stopNote, setStopNote] = useState('');
    const [filter, setFilter] = useState<ActivityFilter>('all');
    const [exporting,setExporting]=useState(false),[exportNote,setExportNote]=useState(''),[exportError,setExportError]=useState('');
    const [clearing,setClearing]=useState<string|null>(null),[clearNote,setClearNote]=useState(''),[clearError,setClearError]=useState(''),[pendingClear,setPendingClear]=useState<PendingActivityClear|null>(null);
    const clearRequest=useRef<AbortController|null>(null);
    const [sourceChoice,setSourceChoice]=useState<SourceChoice|null>(null),[sourceNote,setSourceNote]=useState('');
    const exportRequest=useRef<AbortController|null>(null);
    useEffect(()=>()=>{exportRequest.current?.abort();exportRequest.current=null;clearRequest.current?.abort();clearRequest.current=null;},[csrf]);
    const [confirmClearLogs,setConfirmClearLogs]=useState(false);
    const rows = feed.items;
    async function exportLog(){
        if(exportRequest.current||clearRequest.current)return;
        const controller=new AbortController();exportRequest.current=controller;setExporting(true);setExportError('');setExportNote('Preparing activity log…');
        try{
            const result=await exportActivityLog({entries:sourceEntries,filter,label:ACTIVITY_FILTERS.find(item=>item.id===filter)?.label||'All',matches:entry=>filter==='all'||activityBucket(entry.state,entry.phase)===filter,historyEntry:row=>historyEntry(row,false,false),compare:orderActivity,signal:controller.signal,onProgress:(done,total)=>setExportNote('Preparing log: '+done.toLocaleString('en-GB')+' of '+total.toLocaleString('en-GB')+' history entries…')});
            if(controller.signal.aborted)return;
            saveActivityLog(result);setExportNote(result.count.toLocaleString('en-GB')+(result.count===1?' activity exported.':' activities exported.'));
        }catch(problem){if(controller.signal.aborted)setExportNote('Log export cancelled.');else{setExportNote('');setExportError(redactActivityText(message(problem)));}}
        finally{if(exportRequest.current===controller){exportRequest.current=null;setExporting(false);}}
    }
    function acceptClear(result:ActivityClearAcknowledgement,signatures:ReadonlySet<string>){
        setPendingClear(previous=>({...result,signatures:new Set([...(previous&&(feed.activityRevision||0)<previous.activityRevision?previous.signatures:[]),...signatures])}));
        if(feed.reset)feed.reset();else feed.refresh();
        torrents.refresh?.();nzbs.refresh?.();
    }
    async function clearItem(target:CapturedActivityClear|null){
        if(clearRequest.current||exportRequest.current||saving)return;
        if(!target){setClearError('Refresh Activity before clearing this entry; its attempt details are unavailable.');feed.refresh();torrents.refresh?.();nzbs.refresh?.();return;}
        const controller=new AbortController(),{kind,id,observed,signature}=target;
        clearRequest.current=controller;setClearing('item:'+kind+':'+id);setClearNote('');setClearError('');
        try{
            const result=await api<ActivityClearItemResult>('/api/activity/clear-item',controller.signal,{kind,id,observed},csrf,{feedback:'inline'});
            if(controller.signal.aborted||clearRequest.current!==controller)return;
            if(result.ok!==true||result.kind!==kind||String(result.id)!==String(id)||!Number.isSafeInteger(result.activityRevision)||result.activityRevision<0||!Number.isSafeInteger(result.cleared)||result.cleared<0||typeof result.held!=='boolean'||typeof result.stopping!=='boolean')throw new Error('Activity clear could not be confirmed. Refresh Activity to check its state.');
            acceptClear(result,new Set([signature]));
            void refreshDownloads();
            setClearNote(result.held?(result.stopping?'Cleared. Related work is stopping; automatic retries are blocked.':'Cleared; automatic retries blocked.'):'Activity cleared; kept in TXT exports.');
        }catch(problem){if(!controller.signal.aborted&&clearRequest.current===controller){setClearError(redactActivityText(message(problem)));feed.refresh();torrents.refresh?.();nzbs.refresh?.();void refreshDownloads();}}
        finally{if(clearRequest.current===controller){clearRequest.current=null;setClearing(null);}}
    }
    async function clearSection(section:ActivityClearSection){
        if(clearRequest.current||exportRequest.current||saving)return;
        const controller=new AbortController();clearRequest.current=controller;setClearing(section);setClearNote('');setClearError('');
        const bucket=section==='attention'?'failed':'completed';
        const signatures=new Set(sourceEntries.filter(entry=>section==='logs'?finishedActivityAttempt(entry.state):activityBucket(entry.state,entry.phase)===bucket).map(activityClearSignature));
        try{
            const result=await api<ActivityClearResult>('/api/activity/clear',controller.signal,{section},csrf,{feedback:'inline'});
            if(controller.signal.aborted||clearRequest.current!==controller)return;
            if(result.ok!==true||result.section!==section||!Number.isSafeInteger(result.activityRevision)||result.activityRevision<0||!Number.isSafeInteger(result.cleared)||result.cleared<0)throw new Error('Activity clear could not be confirmed. Refresh Activity to check its state.');
            acceptClear(result,signatures);
            if(section==='logs'){setConfirmClearLogs(false);setExportNote('');setExportError('');setClearNote(result.cleared.toLocaleString('en-GB')+' finished log '+(result.cleared===1?'entry':'entries')+' cleared from Activity and TXT exports.');}else setClearNote(result.cleared.toLocaleString('en-GB')+' finished '+(result.cleared===1?'entry':'entries')+' cleared from Activity. Entries remain in TXT exports.');
        }catch(problem){if(!controller.signal.aborted&&clearRequest.current===controller)setClearError(redactActivityText(message(problem)));}
        finally{if(clearRequest.current===controller){clearRequest.current=null;setClearing(null);}}
    }
    async function act(item: Video | Job, kind: string, action: string) {
        setSaving(kind + item.id);
        setActionError('');
        try {
            await api(kind === 'm3u' ? '/api/m3u-server-downloads' : '/api/downloads', undefined, kind === 'm3u' ? action === 'retry' ? { action: 'download', source: (item as Job).source, path: (item as Job).path } : { id: item.id, action } : { id: item.id, action }, csrf);
            void refreshDownloads();
            feed.refresh();
        }
        catch (e) {
            setActionError(message(e));
        }
        finally {
            setSaving('');
        }
    }
    async function stopAll() {
        setSaving('stop-all');
        setActionError('');
        setStopNote('Stopping downloads…');
        try {
            const result = await api<{
                acknowledged?: boolean;
                message?: string;
                sources?: Record<string, {
                    stopped: number;
                    pending: number;
                    errors: string[];
                }>;
            }>('/api/downloads', undefined, { action: 'stop_all' }, csrf);
            const counts = Object.entries(result.sources || {}).filter(([, item]) => item.stopped || item.pending).map(([name, item]) => name + ' ' + item.stopped + ' stopped' + (item.pending ? ' · ' + item.pending + ' still stopping' : '')).join(', ');
            setStopNote((result.message || 'Stopping downloads…') + (counts ? ' ' + counts : ''));
            void refreshDownloads();
            feed.refresh();
        }
        catch (e) {
            setActionError(message(e));
            setStopNote('');
        }
        finally {
            setSaving('');
            setConfirmStop(false);
        }
    }
    async function resumeDownloads() {
        setSaving('resume');
        setActionError('');
        try {
            await api('/api/downloads', undefined, { action: 'resume_downloads' }, csrf);
            setStopNote('Downloads can start again.');
            feed.refresh();
        }
        catch (e) {
            setActionError(message(e));
        }
        finally {
            setSaving('');
        }
    }
    async function cancelDiscover(id: string) {
        setSaving(id);
        setActionError('');
        try {
            await api('/api/discover/requests/cancel', undefined, { id }, csrf);
            void refreshDownloads();
            feed.refresh();
        }
        catch (e) {
            setActionError(message(e));
        }
        finally {
            setSaving('');
        }
    }
    function renderHistory(row: (typeof rows)[number]) {
        if (row.kind === 'm3u_sync' || row.kind === 'event')
            return <ActivityEventCard key={row.kind + row.item.id} item={row.item} sync={row.kind === 'm3u_sync'}/>;
        if (row.kind === 'discover') {
            const item = row.item;
            const facts = [item.year, item.rating ? item.rating + ' IMDb' : '', item.genre].filter(Boolean).join(' · ');
            const details = [
                item.message && <span key="message">{item.message}</span>,
                !!item.created && <span key="added">Added: {mediaDate(new Date(item.created * 1000).toISOString())}</span>,
                !!item.updated && <span key="updated">Updated: {mediaDate(new Date(item.updated * 1000).toISOString())}</span>,
                item.destination && <span key="path">Path: {item.destination}</span>,
                facts && <span key="facts">{facts}</span>,
                item.description && <span key="desc">{item.description}</span>,
            ].filter(Boolean);
            const alternative=activitySourceChoice(item);
            return <ListRow contextAction={alternative&&<Button variant="secondary" disabled={!!clearing||exporting||!!saving} onClick={()=>{setSourceNote('');setSourceChoice(alternative);}} style={{maxWidth:'100%'}}><Search size={18}/>Find alternatives</Button>} timestamp={activityDate(item)} key={'discover' + item.id} media={item.artwork ? <img src={item.artwork} alt="" loading="lazy" onError={event => { event.currentTarget.hidden = true; }}/> : (item.mediaKind === 'movie' ? <Film size={22} aria-hidden="true"/> : <Tv size={22} aria-hidden="true"/>)} title={item.mediaItem?<ActivityMediaTitle name={item.title} item={item.mediaItem} onOpen={openProgramme}/>:<span>{item.title}</span>} badge={<DownloadState state={item.state}/>} statusColour={activityStatusColour(item.state)} meta={<span>Discover · {item.mediaKind === 'movie' ? 'Movie' : 'TV show'}</span>} progress={item.state === 'downloading' && typeof item.progress === 'number' ? <Progress value={Math.round(item.progress * 100)} aria-label={'Download progress for ' + item.title}/> : undefined} actions={['waiting', 'searching', 'starting', 'queued', 'downloading', 'verifying', 'paused', 'cancelling'].includes(item.state) && <Button variant="secondary" disabled={saving === item.id || item.state === 'cancelling'} onClick={() => void cancelDiscover(item.id)}>{saving === item.id || item.state === 'cancelling' ? 'Cancelling…' : 'Cancel'}</Button>} details={details.length ? details : undefined}/>;
        }
        const item = row.item, state = rowState(row);
        const yt = row.kind === 'youtube' ? row.item : null, server = row.kind === 'm3u' ? row.item : null;
        const strm = server?.sourceType === 'strm';
        const open = () => {setProgramme(null);setSelection(row);};
        const art = server?.artwork || yt?.video?.thumbnail || '';
        const outcome = item.message || ({ complete: 'Download completed successfully.', failed: 'Download failed. No further details were recorded.', cancelled: 'Download cancelled.', queued: 'Waiting to download.' } as Record<string, string>)[state || ''] || 'Download in progress.';
        const details = [
            <span key="outcome">{outcome}</span>,
            strm && server?.sourceName && <span key="source">STRM file: {server.sourceName}</span>,
            strm && <span key="type">Type: {server?.kind === 'series' ? 'TV episode' : 'Movie'}</span>,
            !!item.created && <span key="added">Added: {mediaDate(new Date(item.created * 1000).toISOString())}</span>,
            !!item.updated && <span key="updated">{state === 'complete' ? 'Completed' : state === 'failed' ? 'Failed' : 'Updated'}: {mediaDate(new Date(item.updated * 1000).toISOString())}</span>,
            item.destination && <span key="path">Path: {item.destination}</span>,
        ].filter(Boolean);
        return <ListRow timestamp={activityDate(item)} key={row.kind + item.id} media={art ? <img src={art} alt="" loading="lazy" onError={event => { event.currentTarget.hidden = true; }}/> : <Film size={20} aria-hidden="true"/>} title={strm ? <span>{item.title}</span> : <MediaHover name={item.title} onClick={open} card={{title:item.title,channel:yt?.channel_name,overview:yt?.description||server?.description,image:art}} actions={yt?<TileActions item={{kind:'video',id:'video:'+yt.id,videoId:yt.id,title:yt.title,image:art,channel:yt.channel_name,channelId:yt.channel}} inline/>:server?<TileActions resolve={{source:server.source,path:server.path}} inline/>:undefined}><ControlTrigger type="button" mih={44} onClick={open}>{item.title}</ControlTrigger></MediaHover>} badge={<DownloadState state={state} phaseLabel={yt && state === 'downloading' ? downloadLabel(yt) : undefined}/>} statusColour={activityStatusColour(state, yt && state === 'downloading' ? downloadLabel(yt) : undefined)} meta={<>
    <span>{yt ? 'YouTube' : server?.kind === 'movies' ? 'Movie' : server?.kind === 'unsorted' ? 'Video' : 'TV episode'}</span>
    {yt ? <ChannelIdentity id={yt.channel} name={yt.channel_name || 'Unknown channel'} avatar={yt.avatar}/> : server && <span>{strm ? 'STRM' : server.subscription}</span>}
   </>} progress={state === 'downloading' && typeof item.progress === 'number' ? <Progress value={item.progress} aria-label={'Download progress for ' + item.title}/> : undefined} actions={<>
    {!strm && <ReelDockButton variant="secondary" aria-label={'Open ' + item.title} onClick={open}><Info size={18}/></ReelDockButton>}
    {['queued', 'downloading', 'cancelling'].includes(state || '') && <Button variant="secondary" disabled={!!saving || state === 'cancelling'} onClick={() => void act(item, row.kind, 'cancel')}>Cancel</Button>}
    {['failed', 'cancelled', 'expired'].includes(state || '') && <Button variant="secondary" disabled={!!saving} onClick={() => void act(item, row.kind, 'retry')}>Retry</Button>}
   </>} details={details.length ? details : undefined}/>;
    }
    const openQueue = (job: {
        state?: string;
    }) => ['queued', 'downloading', 'paused', 'choose'].includes(job.state || '');
    const visible = torrents.jobs;
    const visibleNzb = nzbs.jobs;
    const activeRows = feed.active || [];
    const activeIds = new Set(activeRows.map(row => row.kind + String(row.item.id)));
    const pinned = ['queued', 'downloading', 'cancelling', 'waiting', 'searching', 'starting', 'verifying', 'paused', 'running'];
    function historyEntry(row:ActivityHistoryRow,active=false,render=true){return {at:activityTime(row.item),pin:active||pinned.includes(row.item.state||'')?1:0,id:(active?'active':'')+row.kind+String(row.item.id),identity:row.kind+':'+String(row.item.id),state:rowState(row),phase:row.kind==='youtube'?row.item.phase:undefined,data:row,node:render&&row.kind!=='torrent'&&row.kind!=='nzb'?renderHistory(row):null};}
    // One merged feed: active rows pin to the top, then history rows, torrent and
    // NZB jobs by recency. The state filter applies to the whole set.
    const sourceEntries = [
        ...activeRows.map(row => historyEntry(row,true)),
        ...rows.filter(row => !activeIds.has(row.kind + String(row.item.id))).map(row => historyEntry(row)),
        ...visible.map(job => ({ at: job.added || 0, pin: openQueue(job) ? 1 : 0, id: 'torrent' + job.id, identity:'torrent:'+job.id, state: torrents.stateOf(job), phase: undefined, data:{kind:'torrent',item:job}, node: torrents.card(job) })),
        ...visibleNzb.map(job => ({ at: job.added || 0, pin: openQueue(job) ? 1 : 0, id: 'nzb' + job.id, identity:'nzb:'+job.id, state: nzbs.stateOf(job), phase: job.progress_stage, data:{kind:'nzb',item:job}, node: nzbs.card(job) })),
    ].filter(entry=>!activityEntryCleared(entry,activityBucket(entry.state,entry.phase),feed,pendingClear)).sort(orderActivity);
    const entries = distinctActivityJobs(sourceEntries);
    const alternatives=entries.flatMap(entry=>entry.data.kind==='discover'?[activitySourceChoice(entry.data.item)].filter((value):value is SourceChoice=>!!value):[]);
    const alternativeKeys=alternatives.map(value=>JSON.stringify([value.requestId,value.attemptKey])).join('|');
    useEffect(()=>{if(sourceChoice&&!alternatives.some(value=>value.requestId===sourceChoice.requestId&&value.attemptKey===sourceChoice.attemptKey))setSourceChoice(null);},[alternativeKeys,sourceChoice]);
    useEffect(()=>{setSourceChoice(null);setSourceNote('');},[csrf]);
    function withClearAction(entry:ActivityLogEntry&{node:ReactNode}){
        if(!isValidElement<{clearAction?:ReactNode;actionsDisabled?:boolean}>(entry.node))return entry.node;
        const media=entry.data.kind!=='event'&&entry.data.kind!=='m3u_sync',working=clearing==='item:'+entry.identity,target=captureActivityClear(entry);
        const finished=finishedActivityAttempt(target?.observed.state||(entry.data.item as {state?:string}).state||entry.state);
        const label=media?(finished?'Clear finished attempt':'Clear activity and stop automatic retries'):'Clear activity';
        const title=!target?'Refresh Activity before clearing this entry; its attempt details are unavailable.':!media?'Clear this activity record; kept in TXT exports.':finished?'Clear this finished attempt from Activity. A newer replacement keeps running; TXT exports and saved files are kept.':'Clear this item, stop related work and block automatic retries until explicit Retry or Resume. Saved files and TXT exports are kept.';
        return cloneElement(entry.node,{actionsDisabled:!!clearing,clearAction:<Button type="button" variant="secondary" style={{minHeight:44,width:phone?44:undefined,paddingInline:phone?0:undefined}} aria-label={label} title={title} disabled={!target||!!clearing||exporting||!!saving} onClick={()=>void clearItem(target)}>{working?(phone?'…':'Clearing…'):'Clear'}</Button>});
    }
    const counts: Record<ActivityFilter, number> = { downloading: 0, processing: 0, queued: 0, waiting: 0, failed: 0, completed: 0, all: entries.length };
    for (const entry of entries)
        counts[activityBucket(entry.state, entry.phase)] += 1;
    const shown = filter === 'all' ? entries : entries.filter(entry => activityBucket(entry.state, entry.phase) === filter);
    const queueGroups=[{id:'in-progress' as ActivityGroupId,title:'In progress',buckets:['downloading','processing'],clear:undefined},{id:'waiting' as ActivityGroupId,title:'Waiting',buckets:['queued','waiting'],clear:undefined},{id:'attention' as ActivityGroupId,title:'Needs attention',buckets:['failed'],clear:'attention' as ActivityClearSection},{id:'completed' as ActivityGroupId,title:'Completed',buckets:['completed'],clear:'completed' as ActivityClearSection}].map(group=>({...group,entries:shown.filter(entry=>group.buckets.includes(activityBucket(entry.state,entry.phase)))})).filter(group=>group.entries.length);
    const totals=[{label:'Active',value:counts.downloading+counts.processing,Icon:ArrowDownToLine},{label:'Queued',value:counts.queued+counts.waiting,Icon:ListOrdered},{label:'Attention',value:counts.failed,Icon:CircleX},{label:'Completed',value:counts.completed,Icon:CircleCheck}];
    return <Stack component="section" gap="md" aria-label="Activity"><Paper withBorder radius="md" p="sm" aria-label="Activity summary"><SimpleGrid cols={{base:2,sm:4}} spacing="sm">{totals.map(total=><Group key={total.label} gap={6} wrap="nowrap" style={{minWidth:0}}><total.Icon size={20} strokeWidth={1.8} aria-hidden="true"/><Text size="xs" c="dimmed" style={{whiteSpace:'nowrap',flex:1,minWidth:0}}>{total.label}</Text><Text size="sm" fw={700} style={{flexShrink:0}}>{total.value}</Text></Group>)}</SimpleGrid></Paper><Group justify="space-between" align="flex-start" wrap="nowrap" gap="sm"><NativeSelect aria-label="Activity status" disabled={exporting||!!clearing} value={filter} onChange={event => setFilter(event.currentTarget.value as ActivityFilter)} style={{width:220,flex:'0 1 220px',minWidth:0}}>{ACTIVITY_FILTERS.map(tab => <option key={tab.id} value={tab.id}>{tab.label + ' (' + counts[tab.id] + ')'}</option>)}</NativeSelect><Group gap="xs" wrap="nowrap">{!phone && feed.acquisitionHold && <Button variant="secondary" disabled={!!saving} onClick={() => void resumeDownloads()}>{saving === 'resume' ? 'Resuming…' : 'Resume downloads'}</Button>}<Menu position="bottom-end" withinPortal><Menu.Target><ActionIcon variant="default" aria-label="More download actions" size={44}><EllipsisVertical size={20} strokeWidth={1.8} aria-hidden="true"/></ActionIcon></Menu.Target><Menu.Dropdown><Menu.Item leftSection={<ArrowDownToLine size={20} strokeWidth={1.8}/>} disabled={exporting||!!clearing} onClick={()=>void exportLog()}>{exporting?'Exporting log…':'Export log (.txt)'}</Menu.Item><Menu.Item leftSection={<CircleX size={20} strokeWidth={1.8}/>} disabled={exporting||!!clearing||!!saving} onClick={()=>setConfirmClearLogs(true)}>Clear Logs</Menu.Item>{exporting&&<Menu.Item leftSection={<Ban size={20} strokeWidth={1.8}/>} onClick={()=>exportRequest.current?.abort()}>Cancel log export</Menu.Item>}{phone && feed.acquisitionHold && <Menu.Item leftSection={<ArrowRight size={20} strokeWidth={1.8}/>} disabled={!!saving} onClick={() => void resumeDownloads()}>{saving === 'resume' ? 'Resuming…' : 'Resume downloads'}</Menu.Item>}<Menu.Item leftSection={<Ban size={20} strokeWidth={1.8}/>} color="red" disabled={!!saving} onClick={() => setConfirmStop(true)}>Stop all downloads</Menu.Item></Menu.Dropdown></Menu></Group></Group>{confirmClearLogs&&<ConfirmInline title="Clear finished logs?" confirm="Clear Logs" keep="Keep logs" busy={!!clearing} onConfirm={()=>void clearSection('logs')} onCancel={()=>{if(!clearing)setConfirmClearLogs(false);}}><Text size="sm">Remove finished entries from Activity and TXT exports. Active, queued and waiting downloads keep running.</Text></ConfirmInline>}{exportNote&&<Text size="sm" c="dimmed" role="status">{exportNote}</Text>}{exportError&&<NotificationMessage kind="error" message={exportError}/>} {clearNote&&<Text size="sm" c="dimmed" role="status">{clearNote}</Text>}{clearError&&<NotificationMessage kind="error" message={clearError}/>} {saving && stopNote ? <Text size="sm" role="status">{stopNote}</Text> : <NotificationMessage kind="info" message={stopNote}/>}{torrents.notice}{nzbs.notice}{(feed.error || actionError) && <Warning text={feed.error || actionError}/>}<ActivityQueueRows><Stack gap="md">{queueGroups.map(group=><ActivityGroup key={group.id} id={group.id} title={group.title} count={group.entries.length} actions={group.clear&&<Button variant="secondary" style={{minHeight:44}} aria-label={'Clear '+group.title+' section'} title="Clear finished entries from Activity; active jobs stay; keep entries in TXT exports." disabled={!!clearing||exporting||!!saving} onClick={()=>void clearSection(group.clear!)}>{clearing===group.clear?'Clearing…':'Clear section'}</Button>}>{group.entries.map((entry,index)=><Fragment key={entry.id}>{index>0&&<Divider mx="sm"/>}{withClearAction(entry)}</Fragment>)}</ActivityGroup>)}</Stack></ActivityQueueRows>{torrents.dialog}{nzbs.dialog}{(sourceChoice||alternatives.length>0)&&<Suspense fallback={<LoadingStatus label="Opening alternatives…"/>}><SourceAlternatives choice={sourceChoice} autoChoices={alternatives} allowAuto={!selection&&!programme&&!confirmStop&&!confirmClearLogs&&!clearing&&!saving&&!nativeDialogOpen(torrents.dialog)&&!nativeDialogOpen(nzbs.dialog)} csrf={csrf} onClose={()=>setSourceChoice(null)} onSelected={()=>{setSourceNote('Selected source queued.');feed.refresh();torrents.refresh?.();nzbs.refresh?.();void refreshDownloads();}}/></Suspense>}{sourceNote&&<Text size="sm" role="status">{sourceNote}</Text>}<Text component="div" ref={feed.end} c="dimmed" size="sm" role="status">{feed.busy ? 'Loading activity...' : feed.more ? '' : entries.length ? `${entries.length} ${entries.length===1?'activity':'activities'}` : ''}</Text>{!feed.busy && !feed.error && !entries.length && <Empty title="Nothing in the queue">Downloads and M3U syncs will appear here.</Empty>}{!feed.busy && !!entries.length && !shown.length && <Empty title={'Nothing ' + (ACTIVITY_FILTERS.find(tab => tab.id === filter)?.label || '').toLowerCase() + ' right now'}>Switch to All to see every item.</Empty>}<Dialog open={confirmStop} onOpenChange={setConfirmStop}><DialogContent><Stack component="header" gap="xs" mb="md"><DialogTitle>Stop all downloads</DialogTitle><DialogDescription>This stops current and queued downloads, removes unfinished jobs and their temporary files, and keeps completed library media.</DialogDescription></Stack><Group gap="sm"><Button variant="danger" disabled={saving === 'stop-all'} onClick={() => void stopAll()}>{saving === 'stop-all' ? 'Stopping downloads…' : 'Stop all downloads'}</Button><Button variant="secondary" disabled={saving === 'stop-all'} onClick={() => setConfirmStop(false)}>Keep downloading</Button></Group></DialogContent></Dialog><Suspense fallback={<Loader size="sm" aria-label="Loading media details"/>}>{programme&&<DiscoveryTitle item={programme} csrf={csrf} onClose={()=>setProgramme(null)}/>} {selection?.kind === 'm3u' && <M3uDetails key={selection.item.id} source={selection.item.source} item={selection.item} onClose={() => setSelection(null)}/>}{selection?.kind === 'youtube' && <VideoSheet csrf={csrf} open onOpenChange={open => { if (!open)
        setSelection(null); }} video={{ id: selection.item.id, title: selection.item.title, image: selection.item.video?.thumbnail, channel: selection.item.channel_name || 'Unknown channel', channelId: selection.item.channel, avatar: selection.item.avatar, duration: selection.item.video?.duration }} actions={<TileActions item={{ kind: 'video', id: 'video:' + selection.item.id, videoId: selection.item.id, title: selection.item.title, image: selection.item.video?.thumbnail, channel: selection.item.channel_name, channelId: selection.item.channel }}/>} downloads={<VideoDownloadControls id={selection.item.id} title={selection.item.title} poster={selection.item.video?.thumbnail} showCollections={false}/>}/>}</Suspense></Stack>;
}
