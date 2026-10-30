import * as Mantine from '@mantine/core';
import { Button as ReelDockButton } from './components/ui';
import { NotificationMessage, notify } from './notifications';
import DownloadPanel from './downloadUI/DownloadPanel';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, CircleAlert, Film, Network } from 'lucide-react';
import { api } from './api';
import { addDownloadEvent } from './appNavigation';
import { TextArea, TextInput } from './TextField';
import { mediaDate } from './mediaDate';
import { Button, Dialog, DialogContent, DialogDescription, DialogTitle } from './components/ui';
import ListRow, { CompactActivityRows } from './components/ListRow';
import { ActivityStatus as DownloadState, activityStatusColour } from './components/ActivityStatus';
import DropZone from './components/DropZone';
import { Field, Group, Notice, Row, Toggle } from './SettingsUI';
import FixMatch from './FixMatch';
import ActivityMediaTitle, { ActivityMediaMenuItem, type ActivityMediaCard } from './ActivityMedia';
import type { DiscoverItem } from './Discover';
export type TorrentFile = {
    index: number;
    path: string;
    size: number;
    kind: string;
    selected: boolean;
    selectable: boolean;
    reason: string;
};
export type TorrentPreview = {
    id: string;
    status: string;
    accepted: boolean;
    name: string;
    reason: string;
    files: TorrentFile[];
    suggested_name?: string;
    suggested_folder?: string;
};
export type TorrentJob = {
    id: string;
    name: string;
    state: string;
    progress: number;
    error: string;
    stop_reason: string;
    published: string[];
    down_rate: number;
    up_rate: number;
    peers: number;
    trackers: string[];
    files: TorrentFile[];
    placements?: {
        index: number;
        name: string;
        category: string;
    }[];
    metadata_note?: string;
    mediaItem?: DiscoverItem;
    added?: number;
    limits: {
        download_limit_bps: number;
        upload_limit_bps: number;
        share_ratio_limit: number | null;
    };
};
function message(error: unknown) {
    return error instanceof Error ? error.message : 'Please try again.';
}
function size(bytes: number) {
    if (!bytes)
        return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const power = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return (bytes / 1024 ** power).toFixed(power ? 1 : 0) + ' ' + units[power];
}
function rate(bytes: number) {
    return size(Math.max(0, Math.round(bytes))) + '/s';
}
function duration(seconds: number) {
    const value = Math.max(0, Math.round(seconds));
    if (value < 60)
        return value + ' sec';
    const minutes = Math.round(value / 60);
    if (minutes < 60)
        return minutes + ' min';
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    if (hours < 48)
        return rest ? hours + ' h ' + rest + ' min' : hours + ' h';
    return Math.floor(hours / 24) + ' d';
}
function eta(job: TorrentJob) {
    const total = (job.files || []).filter(file => file.selected).reduce((sum, file) => sum + file.size, 0);
    const remaining = total * Math.max(0, 1 - job.progress);
    if (!job.down_rate || remaining <= 0)
        return 'ETA —';
    return 'ETA ' + duration(remaining / job.down_rate);
}
const companionFile = /\.(srt|vtt|ass|ssa|sub|idx|jpg|jpeg|png|webp|nfo)$/i;
function kindLabel(job: {
    files?: TorrentFile[];
    published?: string[];
}) {
    const files = job.files || [];
    if (files.some(file => file.selected && file.kind === 'video'))
        return 'Torrent · Video';
    if (files.some(file => file.selected && file.kind === 'audio'))
        return 'Torrent · Audio';
    const saved = (job.published || []).find(item => !companionFile.test(item)) || '';
    if (saved.includes('/Audio/'))
        return 'Torrent · Audio';
    if (saved)
        return 'Torrent · Video';
    return 'Torrent · Media';
}
function parentDirectory(path: string) {
    const slash = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
    return slash > 0 ? path.slice(0, slash) : path;
}
function savedLocation(job: TorrentJob) {
    const published = job.published || [];
    const media = published.filter(item => !companionFile.test(item));
    if ((job.placements || []).length)
        return 'Path: choose Movies or TV Shows';
    if (job.progress >= 1) {
        const dirs = [...new Set((media.length ? media : published).map(parentDirectory))];
        return dirs.length ? 'Path: ' + dirs.join(', ') : 'Path: waiting to save';
    }
    if ((job.files || []).some(file => file.selected && file.kind === 'video'))
        return 'Path: video still downloading';
    if ((job.files || []).some(file => file.selected && file.kind === 'audio'))
        return 'Path: audio still downloading';
    return 'Path: waiting to save';
}
export function TorrentPlacement({ csrf }: {
    csrf: string;
}) {
    const { jobs, refresh } = useTorrentJobs();
    const [error, setError] = useState('');
    const [saving, setSaving] = useState('');
    const pending = jobs.flatMap(job => (job.placements || []).map(item => ({ ...item, jobId: job.id })));
    async function choose(jobId: string, index: number, category: 'movie' | 'series') {
        setSaving(jobId + ':' + index);
        setError('');
        try {
            await api('/api/torrents/' + encodeURIComponent(jobId), undefined, { action: 'placement', index, category }, csrf);
            refresh();
        }
        catch (e) {
            setError(message(e));
        }
        finally {
            setSaving('');
        }
    }
    if (!pending.length)
        return null;
    const rows = pending.map(item => {
        const name = item.name.split(/[/\\]/).pop() || item.name;
        return <Mantine.Paper key={item.jobId + ':' + item.index} withBorder p="md" radius="md"><Mantine.Stack gap="sm"><Mantine.Text fw={600} size="sm">{name}</Mantine.Text><Mantine.Group gap="sm" wrap="wrap"><Button type="button" disabled={!!saving} onClick={() => void choose(item.jobId, item.index, 'movie')}>Movie</Button><Button type="button" variant="secondary" disabled={!!saving} onClick={() => void choose(item.jobId, item.index, 'series')}>TV show</Button></Mantine.Group></Mantine.Stack></Mantine.Paper>;
    });
    return <Mantine.Paper aria-label="Choose where to save" component="section" withBorder p="md" radius="lg"><Mantine.Stack gap="md"><Mantine.Group gap="xs"><CircleAlert size={16} aria-hidden="true"/><Mantine.Text fw={600}>Choose where to save</Mantine.Text></Mantine.Group><Mantine.Text size="sm" c="dimmed">ReelDock could not tell whether this is a movie or a TV show.</Mantine.Text>{rows}{error && <NotificationMessage kind="error" message={error}/>}</Mantine.Stack></Mantine.Paper>;
}
export function useTorrentJobs() {
    const [jobs, setJobs] = useState<TorrentJob[]>([]);
    const [error, setError] = useState('');
    const [revision, setRevision] = useState(0);
    const refresh = useCallback(() => setRevision(value => value + 1), []);
    useEffect(() => {
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout>;
        async function load() {
            try {
                const result = await api<{
                    torrents: TorrentJob[];
                }>('/api/torrents', controller.signal);
                if (!controller.signal.aborted) {
                    setJobs(result.torrents);
                    setError('');
                }
            }
            catch (e) {
                if (!controller.signal.aborted)
                    setError(message(e));
            }
            finally {
                if (!controller.signal.aborted)
                    timer = setTimeout(load, 5000);
            }
        }
        void load();
        return () => { controller.abort(); clearTimeout(timer); };
    }, [revision]);
    return { jobs, error, refresh };
}
type MatchCard = ActivityMediaCard;
export function nativeMetadataSummary(note?:string){return !note?'':/^Basic local metadata/.test(note)?'Basic local metadata':/TMDB.*(?:credential|Settings)|(?:credential|Connect).*TMDB/i.test(note)?'Basic metadata is available without a TMDB login':'Metadata needs attention';}
export function NativeMetadataReadiness({lookup,edited=false}:{lookup:string;edited?:boolean}){
    const [card,setCard]=useState<Pick<MatchCard,'matched'|'title'|'message'>|null>(null),[failed,setFailed]=useState(false);
    useEffect(()=>{const controller=new AbortController();setCard(null);setFailed(false);api<MatchCard>(lookup,controller.signal,undefined,undefined,{feedback:'inline'}).then(result=>{if(!controller.signal.aborted)setCard(result);}).catch(()=>{if(!controller.signal.aborted)setFailed(true);});return()=>controller.abort();},[lookup]);
    return <Mantine.Text size="sm" c="dimmed" role="status" data-native-metadata-readiness>{failed?'Metadata lookup is unavailable. The download can continue; use Change metadata after it finishes.':!card?'Checking source metadata…':!card.matched?(card.message||'No unique metadata match for this source.')+' The download can continue; use Change metadata after it finishes.':`Source metadata matches “${card.title}”. ${edited?'Your edited Save as changes naming only; choose Change metadata after download to select another title.':'Save as changes naming only; it does not select a catalogue match.'}`}</Mantine.Text>;
}
export function TorrentName({ id, name, onClick, lookup, mediaItem, onOpenMedia }: {
    id?: string;
    name: string;
    onClick?: () => void;
    lookup?: string;
    mediaItem?: DiscoverItem;
    onOpenMedia?: (item:DiscoverItem)=>void;
}) {
    return <ActivityMediaTitle name={name} item={mediaItem} lookup={lookup||(id?'/api/torrents/'+encodeURIComponent(id)+'/lookup':undefined)} onOpen={onOpenMedia} onClick={onClick}/>;
}
export function TorrentProgress({ csrf }: {
    csrf: string;
}) {
    const { jobs, refresh } = useTorrentJobs();
    const [saving, setSaving] = useState('');
    const [error, setError] = useState('');
    const active = jobs.filter(job => ['downloading', 'queued', 'paused', 'choose'].includes(job.state));
    async function act(job: TorrentJob, action: string) {
        setSaving(job.id + action);
        setError('');
        try {
            await api('/api/torrents/' + encodeURIComponent(job.id), undefined, { action }, csrf);
            refresh();
        }
        catch (e) {
            setError(message(e));
        }
        finally {
            setSaving('');
        }
    }
    if (!active.length)
        return null;
    return <Mantine.Paper component="section" withBorder p="md" radius="lg" aria-label="Torrent downloads"><Mantine.Stack gap="sm"><Mantine.Title order={3} size="h5">Torrent downloads</Mantine.Title><CompactActivityRows>{active.map(job => <ListRow key={job.id} title={<TorrentName id={job.id} name={job.name}/>} media={<img src="/preview/assets/torrent-icon.svg" alt=""/>} badge={<DownloadState state={job.state} phaseLabel={job.state === 'choose' ? 'Choose type' : undefined}/>} statusColour={activityStatusColour(job.state, job.state === 'choose' ? 'Choose type' : undefined)} progress={<Mantine.Progress value={Math.round(job.progress * 100)} aria-label={'Download progress for ' + job.name}/>} meta={<Mantine.Text size="sm" c="dimmed">Torrent</Mantine.Text>} details={<><Mantine.Text size="sm">{Math.round(job.progress * 100)}% · {rate(job.down_rate)}{job.state === 'downloading' ? ' · ' + eta(job) : ''}</Mantine.Text>{job.files.some(file => file.selected) && <Mantine.Text size="sm">Estimated download size: {size(job.files.filter(file => file.selected).reduce((total, file) => total + file.size, 0))}</Mantine.Text>}</>} actions={<>{job.state === 'paused' ? <Button variant="secondary" disabled={!!saving} onClick={() => void act(job, 'resume')}>Resume</Button> : <Button variant="secondary" disabled={!!saving || job.state === 'choose'} onClick={() => void act(job, 'pause')}>Pause</Button>}<Button variant="secondary" disabled={!!saving} onClick={() => void act(job, 'remove')}>Remove</Button></>}/>)}</CompactActivityRows>{error && <NotificationMessage kind="error" message={error}/>}</Mantine.Stack></Mantine.Paper>;
}
export function TorrentAddDialog({ open, onOpenChange, csrf, onStarted, bare }: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    csrf: string;
    onStarted?: () => void;
    bare?: boolean;
}) {
    const [url, setUrl] = useState('');
    const [file, setFile] = useState<File | null>(null);
    const [preview, setPreview] = useState<TorrentPreview | null>(null);
    const [chosen, setChosen] = useState<number[]>([]);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [saveAs, setSaveAs] = useState('');
    useEffect(() => {
        if (!open) {
            setPreview(null);
            setChosen([]);
            setSaveAs('');
            setFile(null);
            setError('');
            setNotice('');
            setBusy('');
        }
    }, [open]);
    async function scan() {
        setBusy('scan');
        setError('');
        setNotice('');
        setPreview(null);
        try {
            let result: TorrentPreview;
            if (file) {
                if (!file.name.toLowerCase().endsWith('.torrent'))
                    throw new Error('Choose a .torrent file.');
                if (file.size > 2000000)
                    throw new Error('Choose a .torrent file smaller than 2 MB.');
                const body = new FormData();
                body.append('torrent', file);
                const response = await fetch('/api/torrents/preview', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrf }, body });
                const payload = await response.json().catch(() => ({ error: 'ReelDock did not return a valid response.' }));
                if (!response.ok)
                    throw new Error(payload.error || 'Could not read that torrent file.');
                result = payload;
            }
            else {
                result = await api<TorrentPreview>('/api/torrents/preview', undefined, { url }, csrf);
            }
            const started = Date.now();
            while (result.status === 'reading-metadata' && Date.now() - started < 20000) {
                await new Promise(resolve => setTimeout(resolve, 1000));
                result = await api<TorrentPreview>('/api/torrents/preview/' + encodeURIComponent(result.id));
            }
            setPreview(result);
            setSaveAs(result.suggested_name || '');
            setChosen(result.files.filter(file => file.selected).map(file => file.index));
            if (!result.accepted)
                setError(result.reason || 'This torrent is not media. Nothing was downloaded.');
        }
        catch (e) {
            setError(message(e));
        }
        finally {
            setBusy('');
        }
    }
    async function start() {
        if (!preview)
            return;
        setBusy('start');
        setError('');
        setNotice('');
        try {
            await api('/api/torrents', undefined, { action: 'start', preview_id: preview.id, files: chosen, title: saveAs.trim() }, csrf);
            notify({ kind: 'success', message: 'Download started. Finished files are renamed into Movies or TV Shows.' });
            onStarted?.();
            onOpenChange(false);
        }
        catch (e) {
            setError(message(e));
        }
        finally {
            setBusy('');
        }
    }
    const ready=!!preview?.accepted;
    const body = <Mantine.Stack onSubmit={event => { event.preventDefault(); if(busy||(ready&&!chosen.length))return;void(ready?start():scan()); }} gap="md" component="form" style={{flex:'1 0 auto',minWidth:0,width:'100%'}}><DownloadPanel fill status={notice} error={error} actions={ready?<><Button type="button" variant="secondary" disabled={!!busy} onClick={()=>{setPreview(null);setChosen([]);setSaveAs('');setError('');setNotice('');}}>Change source</Button><Button type="submit" style={{minWidth:160,height:44}} disabled={!!busy||!chosen.length}>{busy==='start'?'Starting…':'Start download'}</Button></>:<Button type="submit" style={{minWidth:160,height:44}} disabled={!!busy||(!url.trim()&&!file)}>{busy==='scan'?'Scanning…':'Scan'}</Button>}><Mantine.Stack gap="md">{!ready&&<Mantine.Stack gap="md"><Field label="Torrent address"><TextInput aria-label="Torrent address" type="text" placeholder="https://…/file.torrent or magnet:" value={url} onChange={event => { setUrl(event.target.value); setFile(null); setPreview(null); }}/></Field><Field label="Torrent file"><DropZone label="Torrent file" description="Choose a .torrent file smaller than 2 MB, or drag it here." accept=".torrent,application/x-bittorrent" file={file} disabled={!!busy} onFile={chosenFile => {
            setFile(chosenFile);
            if (chosenFile)
                setUrl('');
            setPreview(null);
        }}/></Field></Mantine.Stack>}{preview && <Group title={preview.name ? <TorrentName key={preview.id} name={preview.name} lookup={'/api/torrents/preview/' + encodeURIComponent(preview.id) + '/lookup'}/> : 'File list'}>{preview.files.map(file => <Toggle key={file.index} title={file.path} detail={file.selectable ? size(file.size) + ' · ' + file.kind : file.reason || 'Not media'} value={chosen.includes(file.index)} disabled={!file.selectable || !!busy} onChange={on => setChosen(current => on ? [...current, file.index] : current.filter(index => index !== file.index))}/>)}{!preview.files.length && <Mantine.Text size="sm" c="dimmed">{preview.status === 'reading-metadata' ? 'Reading torrent metadata…' : 'No files in this torrent.'}</Mantine.Text>}</Group>}{preview?.accepted && <><Field label="Save as" help="The video, subtitles and .nfo use this name before metadata is looked up. A TV episode keeps its episode number. A year on a TV show is the year it started." note={preview.suggested_folder ? `Existing folder: ${preview.suggested_folder}` : ''}><TextInput aria-label="Save as" value={saveAs} onChange={event => setSaveAs(event.target.value)}/></Field><NativeMetadataReadiness lookup={'/api/torrents/preview/'+encodeURIComponent(preview.id)+'/lookup'} edited={saveAs.trim()!==(preview.suggested_name||'').trim()}/></>}</Mantine.Stack></DownloadPanel></Mantine.Stack>;
    return bare ? body : <Dialog open={open} onOpenChange={onOpenChange}><DialogContent><Mantine.Stack component="header" gap="xs" mb="md"><DialogTitle>Torrent Download</DialogTitle><DialogDescription>ReelDock reads the file list before saving anything.</DialogDescription></Mantine.Stack>{body}</DialogContent></Dialog>;
}
function activityNote(job: TorrentJob) {
    if (job.metadata_note && job.state !== 'downloading')
        return job.metadata_note;
    if (job.state === 'downloading' || job.progress >= 1)
        return '';
    const error = job.error || '';
    if (/connection refused|errno 111|urlopen error/i.test(error)) {
        return 'The tracker refused the connection. Nothing is listening on its announce address, so it cannot send a peer list.';
    }
    if (/timed out/i.test(error)) {
        return 'A peer or tracker did not answer in time.';
    }
    return error || job.stop_reason || '';
}
export function useTorrentActivity(csrf: string, onOpenMedia?:(item:DiscoverItem)=>void) {
    const { jobs, error, refresh } = useTorrentJobs();
    const [selected, setSelected] = useState<TorrentJob | null>(null);
    const [saving, setSaving] = useState('');
    const [actionError, setActionError] = useState('');
    const [trackers, setTrackers] = useState('');
    const [matching, setMatching] = useState<TorrentJob | null>(null);
    function openMedia(item:DiscoverItem){setSelected(null);setMatching(null);onOpenMedia?.(item);}
    async function act(job: TorrentJob, action: string, extra: object = {}) {
        setSaving(job.id + action);
        setActionError('');
        try {
            await api('/api/torrents/' + encodeURIComponent(job.id), undefined, { action, ...extra }, csrf);
            refresh();
            if (action === 'remove')
                setSelected(null);
        }
        catch (e) {
            setActionError(message(e));
        }
        finally {
            setSaving('');
        }
    }
    // Normalised state for the Activity filter tabs; matches the badge below,
    // except 'choose' files under Queued rather than Downloading.
    function stateOf(job: TorrentJob) {
        if (job.progress >= 1)
            return 'complete';
        if (job.state === 'stopped')
            return 'cancelled';
        if (job.state === 'failed' || job.state === 'error')
            return 'failed';
        if (job.state === 'paused' || job.state === 'queued' || job.state === 'choose')
            return 'queued';
        return 'downloading';
    }
    function card(job: TorrentJob) {
        const finished = job.progress >= 1;
        const state = job.state === 'paused' ? 'queued' : finished ? 'complete' : job.state === 'stopped' ? 'cancelled' : ['failed', 'error'].includes(job.state) ? 'failed' : job.state === 'queued' ? 'queued' : 'downloading';
        const label = job.state === 'paused' ? 'Paused' : job.state === 'choose' ? 'Choose type' : finished ? 'Completed' : state === 'failed' ? 'Failed' : job.state === 'stopped' ? 'Stopped' : undefined;
        const note = activityNote(job);
        const dirs = finished ? [...new Set(((job.published || []).filter(item => !companionFile.test(item)).length ? (job.published || []).filter(item => !companionFile.test(item)) : job.published || []).map(parentDirectory))] : [];
        const started = job.added ? mediaDate(new Date(job.added * 1000).toISOString()) : '';
        const live = job.state === 'downloading' || job.state === 'seeding';
        return <ListRow key={job.id} media={<img src="/preview/assets/torrent-icon.svg" alt=""/>} title={<TorrentName id={job.id} name={job.name} mediaItem={job.mediaItem} onOpenMedia={onOpenMedia?openMedia:undefined}/>} badge={<DownloadState state={state} phaseLabel={label}/>} statusColour={activityStatusColour(state, label)} meta={<Mantine.Text size="sm" c="dimmed">{kindLabel(job)}{finished&&job.metadata_note?' · '+nativeMetadataSummary(job.metadata_note):''}</Mantine.Text>} progress={job.state === 'downloading' ? <Mantine.Progress value={Math.round(job.progress * 100)} aria-label={'Download progress for ' + job.name}/> : undefined} actions={<>
        {job.state === 'paused' ? <Button variant="secondary" disabled={!!saving} onClick={() => void act(job, 'resume')}>Resume</Button> : (job.state === 'downloading' || job.state === 'queued') && <Button variant="secondary" disabled={!!saving} onClick={() => void act(job, 'pause')}>Pause</Button>}
        {finished && <Button variant="secondary" disabled={!!saving} onClick={() => { setSelected(null); setMatching(job); }}>Change metadata</Button>}
        <Button variant="secondary" onClick={() => {setSelected(job);setTrackers(job.trackers.join('\n'));}}>Torrent options</Button>
        {onOpenMedia&&<ActivityMediaMenuItem item={job.mediaItem} lookup={'/api/torrents/'+encodeURIComponent(job.id)+'/lookup'} onOpen={openMedia}/>}
        <Button variant="secondary" disabled={!!saving} onClick={() => void act(job, 'remove')}>Remove</Button>
      </>} details={<>
        {note && <span>{note}</span>}
        {started && <span>Started: {started}</span>}
        <span>{rate(live ? job.down_rate : 0)} down · {rate(live ? job.up_rate : 0)} up · {job.peers} peers{job.state === 'downloading' ? ' · ' + eta(job) : ''}</span>
        {(job.placements || []).length ? <span>Choose a Movies or TV Shows folder for this download.</span> : dirs.length ? <span>Path: {dirs.join(', ')}</span> : null}
      </>}/>;
    }
    const dialog = <><Dialog open={!!selected} onOpenChange={open => {
            if (!open)
                setSelected(null);
        }}><DialogContent>{selected && <><Mantine.Stack component="header" gap="xs" mb="md"><DialogTitle>{selected.name}</DialogTitle><DialogDescription>{kindLabel(selected)}</DialogDescription></Mantine.Stack><Mantine.Stack gap="md"><Group title="Trackers"><Field label="Tracker addresses"><TextArea aria-label="Tracker addresses" clearable={false} fit value={trackers} onChange={event => setTrackers(event.target.value)} onBlur={() => void act(selected, 'trackers', { trackers: trackers.split('\n').map(item => item.trim()).filter(Boolean) })}/></Field></Group><Notice text={actionError} error/><Mantine.Group gap="sm" wrap="wrap"><Button variant="secondary" disabled={!!saving} onClick={() => void act(selected, selected.state === 'paused' ? 'resume' : 'pause')}>{selected.state === 'paused' ? 'Resume' : 'Pause'}</Button><Button variant="secondary" disabled={!!saving} onClick={() => void act(selected, 'remove')}>Remove</Button></Mantine.Group></Mantine.Stack></>}</DialogContent></Dialog><Dialog open={!!matching} onOpenChange={open => {
            if (!open)
                setMatching(null);
        }}><DialogContent>{matching && <FixMatch url={'/api/torrents/' + encodeURIComponent(matching.id) + '/match'} path="" csrf={csrf} onChanged={refresh}/>}</DialogContent></Dialog></>;
    const notice = (error || actionError) ? <Notice text={error || actionError} error/> : null;
    return { jobs, card, dialog, notice, stateOf, refresh };
}
