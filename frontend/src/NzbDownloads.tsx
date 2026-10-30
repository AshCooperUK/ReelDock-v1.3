import * as Mantine from '@mantine/core';
import { Button as ReelDockButton } from './components/ui';
import { NotificationMessage, notify } from './notifications';
import DownloadPanel from './downloadUI/DownloadPanel';
import { useCallback, useEffect, useState } from 'react';
import { ArrowRight, CircleAlert, FileDown } from 'lucide-react';
import { api } from './api';
import { addDownloadEvent } from './appNavigation';
import { TextInput } from './TextField';
import { mediaDate } from './mediaDate';
import { Button, Dialog, DialogContent, DialogDescription, DialogTitle } from './components/ui';
import ListRow, { CompactActivityRows } from './components/ListRow';
import { ActivityStatus as DownloadState, activityStatusColour } from './components/ActivityStatus';
import DropZone from './components/DropZone';
import { Field, Group, Notice, Toggle } from './SettingsUI';
import FixMatch from './FixMatch';
import { TorrentName, NativeMetadataReadiness, nativeMetadataSummary } from './TorrentDownloads';
import { ActivityMediaMenuItem } from './ActivityMedia';
import type { DiscoverItem } from './Discover';
type NzbFile = {
    index: number;
    path: string;
    size: number;
    kind: string;
    selected: boolean;
    selectable: boolean;
    reason: string;
};
type NzbPreview = {
    id: string;
    status: string;
    accepted: boolean;
    name: string;
    reason: string;
    files: NzbFile[];
    suggested_name?: string;
    suggested_folder?: string;
    on_server?: string;
};
type NzbJob = {
    id: string;
    name: string;
    state: string;
    pause_reason?: string;
    progress_stage?: string;
    progress: number;
    error: string;
    stop_reason: string;
    total_bytes?: number;
    down_rate: number;
    connections: number;
    servers: string[];
    files: NzbFile[];
    placements?: {
        index: number;
        name: string;
        category: string;
    }[];
    metadata_note?: string;
    mediaItem?: DiscoverItem;
    added?: number;
    published?: string[];
};
/** Presentation only: keep the transport state and its Clear observation intact. */
export function nzbActivityStatus(job:Pick<NzbJob,'state'|'progress'|'progress_stage'|'pause_reason'>):{state:string;label?:string;description?:string}{
    if(job.state==='failed')return {state:'failed',label:'Failed'};
    if(job.progress>=1&&job.state==='stopped')return {state:'complete',label:'Completed'};
    if(job.state==='paused')return {state:'queued',label:({vpn:'Waiting for VPN',downloads:'Downloads paused',hold:'Downloads stopped'} as Record<string,string>)[job.pause_reason||'']||'Paused',description:job.pause_reason==='vpn'?'Downloads resume automatically when the VPN connection is ready.':undefined};
    if(job.state==='choose')return {state:'queued',label:'Choose type'};
    if(job.state==='queued')return {state:'queued',label:undefined};
    const label=({assembling:'Assembling',repairing:'Repairing',unpacking:'Extracting',extracting:'Extracting',processing:'Processing',publishing:'Publishing'} as Record<string,string>)[job.progress_stage||''];
    return job.state==='downloading'&&label?{state:'processing',label}:{state:'downloading',label:undefined};
}
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
export function useNzbJobs() {
    const [jobs, setJobs] = useState<NzbJob[]>([]);
    const [error, setError] = useState('');
    const [revision, setRevision] = useState(0);
    const refresh = useCallback(() => setRevision(value => value + 1), []);
    useEffect(() => {
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout>;
        async function load() {
            try {
                const result = await api<{
                    nzbs: NzbJob[];
                }>('/api/nzb', controller.signal);
                if (!controller.signal.aborted) {
                    setJobs(result.nzbs);
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
export function NzbPlacement({ csrf }: {
    csrf: string;
}) {
    const { jobs, refresh } = useNzbJobs();
    const [error, setError] = useState('');
    const [saving, setSaving] = useState('');
    const pending = jobs.flatMap(job => (job.placements || []).map(item => ({ ...item, jobId: job.id })));
    async function choose(jobId: string, index: number, category: 'movie' | 'series') {
        setSaving(jobId + ':' + index);
        setError('');
        try {
            await api('/api/nzb/' + encodeURIComponent(jobId), undefined, { action: 'placement', index, category }, csrf);
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
    return <Mantine.Paper aria-label="Choose where to save" component="section" withBorder p="md" radius="lg"><Mantine.Stack gap="md"><Mantine.Group gap="xs"><CircleAlert size={16} aria-hidden="true"/><Mantine.Text fw={600}>Choose where to save</Mantine.Text></Mantine.Group><Mantine.Text size="sm" c="dimmed">ReelDock could not tell whether this NZB is a movie or a TV show.</Mantine.Text>{rows}{error && <NotificationMessage kind="error" message={error}/>}</Mantine.Stack></Mantine.Paper>;
}
export function NzbProgress({ csrf }: {
    csrf: string;
}) {
    const { jobs, refresh } = useNzbJobs();
    const [saving, setSaving] = useState('');
    const [error, setError] = useState('');
    const active = jobs.filter(job => ['downloading', 'queued', 'paused', 'choose'].includes(job.state));
    async function act(job: NzbJob, action: string) {
        setSaving(job.id + action);
        setError('');
        try {
            await api('/api/nzb/' + encodeURIComponent(job.id), undefined, { action }, csrf);
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
    return <Mantine.Paper component="section" withBorder p="md" radius="lg" aria-label="NZB downloads"><Mantine.Stack gap="sm"><Mantine.Title order={3} size="h5">NZB downloads</Mantine.Title><CompactActivityRows>{active.map(job => {
        const {state,label,description}=nzbActivityStatus(job);
        return <ListRow key={job.id} title={<TorrentName id={job.id} name={job.name} lookup={'/api/nzb/' + encodeURIComponent(job.id) + '/lookup'}/>} media={<img src="/preview/assets/nzb-icon.svg" alt=""/>} badge={<DownloadState state={state} phaseLabel={label}/>} statusColour={activityStatusColour(state,label)} progress={<Mantine.Progress value={Math.round(job.progress * 100)} aria-label={'Download progress for ' + job.name}/>} meta={<Mantine.Text size="sm" c="dimmed">NZB</Mantine.Text>} details={<>{description&&<Mantine.Text size="sm">{description}</Mantine.Text>}<Mantine.Text size="sm">{Math.round(job.progress * 100)}% · {rate(job.state==='downloading'&&state!=='processing'?job.down_rate:0)}</Mantine.Text>{typeof job.total_bytes === 'number' && <Mantine.Text size="sm">Estimated download size: {size(job.total_bytes)}</Mantine.Text>}</>} actions={<>{job.state === 'paused' ? <Button variant="secondary" disabled={!!saving} onClick={() => void act(job, 'resume')}>Resume</Button> : <Button variant="secondary" disabled={!!saving || job.state === 'choose'} onClick={() => void act(job, 'pause')}>Pause</Button>}<Button variant="secondary" disabled={!!saving} onClick={() => void act(job, 'remove')}>Remove</Button></>}/>;
    })}</CompactActivityRows>{error && <NotificationMessage kind="error" message={error}/>}</Mantine.Stack></Mantine.Paper>;
}
export function NzbAddDialog({ open, onOpenChange, csrf, onStarted, bare }: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    csrf: string;
    onStarted?: () => void;
    bare?: boolean;
}) {
    const [url, setUrl] = useState('');
    const [file, setFile] = useState<File | null>(null);
    const [preview, setPreview] = useState<NzbPreview | null>(null);
    const [chosen, setChosen] = useState<number[]>([]);
    const [saveAs, setSaveAs] = useState('');
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
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
    function show(result: NzbPreview) {
        setPreview(result);
        setSaveAs(result.suggested_name || '');
        setChosen(result.files.filter(item => item.selected).map(item => item.index));
        if (result.on_server === 'missing')
            setError('This Usenet server does not have these articles.');
        else if (result.on_server === 'partial')
            setNotice('Only some of these articles are on your Usenet server.');
        else if (!result.accepted)
            setError(result.reason || 'This NZB is not media. Nothing was downloaded.');
    }
    async function scan() {
        setBusy('scan');
        setError('');
        setNotice('');
        setPreview(null);
        try {
            let result: NzbPreview;
            if (file) {
                if (!file.name.toLowerCase().endsWith('.nzb'))
                    throw new Error('Choose an NZB file.');
                if (file.size > 20000000)
                    throw new Error('Choose an NZB file smaller than 20 MB.');
                const form = new FormData();
                form.append('nzb', file);
                const response = await fetch('/api/nzb/preview', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrf }, body: form });
                const payload = await response.json().catch(() => ({ error: 'ReelDock did not return a valid response.' }));
                if (!response.ok)
                    throw new Error(payload.error || 'Could not read that NZB file.');
                result = payload;
            }
            else
                result = await api<NzbPreview>('/api/nzb/preview', undefined, { url }, csrf);
            show(result);
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
            await api('/api/nzb', undefined, { action: 'start', preview_id: preview.id, files: chosen, title: saveAs.trim() }, csrf);
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
    const ready=!!preview?.accepted && preview.on_server !== 'missing';
    const body = <Mantine.Stack onSubmit={event => { event.preventDefault(); if(busy||(ready&&!chosen.length))return;void(ready?start():scan()); }} gap="md" component="form" style={{flex:'1 0 auto',minWidth:0,width:'100%'}}><DownloadPanel fill status={notice} error={error} actions={ready?<><Button type="button" variant="secondary" disabled={!!busy} onClick={()=>{setPreview(null);setChosen([]);setSaveAs('');setError('');setNotice('');}}>Change source</Button><Button type="submit" style={{minWidth:160,height:44}} disabled={!!busy||!chosen.length}>{busy==='start'?'Starting…':'Start download'}</Button></>:<Button type="submit" style={{minWidth:160,height:44}} disabled={!!busy||(!url.trim()&&!file)}>{busy==='scan'?'Checking server…':'Scan'}</Button>}><Mantine.Stack gap="md">{!ready&&<Mantine.Stack gap="md"><Field label="NZB address"><TextInput aria-label="NZB address" type="url" placeholder="https://…/file.nzb" value={url} onChange={event => { setUrl(event.target.value); setFile(null); setPreview(null); setError(''); setNotice(''); }}/></Field><Field label="NZB file"><DropZone label="NZB file" description="Choose an .nzb file smaller than 20 MB, or drag it here." accept=".nzb,application/x-nzb" file={file} disabled={!!busy} onFile={chosenFile => {
            setFile(chosenFile);
            if (chosenFile)
                setUrl('');
            setPreview(null);
            setError('');
            setNotice('');
        }}/></Field></Mantine.Stack>}{preview && <Group title={preview.name ? <TorrentName key={preview.id} name={preview.name} lookup={'/api/nzb/preview/' + encodeURIComponent(preview.id) + '/lookup'}/> : 'File list'}>{preview.files.map(item => <Toggle key={item.index} title={item.path} detail={item.selectable ? size(item.size) + ' · ' + item.kind : item.reason || 'Not media'} value={chosen.includes(item.index)} disabled={!item.selectable || !!busy} onChange={on => setChosen(current => on ? [...current, item.index] : current.filter(index => index !== item.index))}/>)}{!preview.files.length && <Mantine.Text size="sm" c="dimmed">No files in this NZB.</Mantine.Text>}</Group>}{preview?.accepted && <><Field label="Save as" help="The video, subtitles and .nfo use this name. A year on a movie is the release year. A TV episode keeps its code, such as Show (2021) S04E110." note={preview.suggested_folder ? `Existing folder: ${preview.suggested_folder}` : ''}><TextInput aria-label="Save as" value={saveAs} onChange={event => setSaveAs(event.target.value)}/></Field><NativeMetadataReadiness lookup={'/api/nzb/preview/'+encodeURIComponent(preview.id)+'/lookup'} edited={saveAs.trim()!==(preview.suggested_name||'').trim()}/></>}</Mantine.Stack></DownloadPanel></Mantine.Stack>;
    return bare ? body : <Dialog open={open} onOpenChange={onOpenChange}><DialogContent><Mantine.Stack component="header" gap="xs" mb="md"><DialogTitle>NZB Download</DialogTitle><DialogDescription>Open an NZB address or file. ReelDock downloads the articles itself.</DialogDescription></Mantine.Stack>{body}</DialogContent></Dialog>;
}
export function useNzbActivity(csrf: string, onOpenMedia?:(item:DiscoverItem)=>void) {
    const { jobs, error, refresh } = useNzbJobs();
    const [saving, setSaving] = useState('');
    const [actionError, setActionError] = useState('');
    const [matching, setMatching] = useState<NzbJob | null>(null);
    async function act(job: NzbJob, action: string) {
        setSaving(job.id + action);
        setActionError('');
        try {
            await api('/api/nzb/' + encodeURIComponent(job.id), undefined, { action }, csrf);
            refresh();
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
    function stateOf(job: NzbJob) { return nzbActivityStatus(job).state; }
    function card(job: NzbJob) {
        const finished = job.progress >= 1 && job.state === 'stopped';
        const {state,label,description}=nzbActivityStatus(job);
        const started = job.added ? mediaDate(new Date(job.added * 1000).toISOString()) : '';
        const paths = finished && job.published?.length ? job.published : [];
        const live = job.state === 'downloading'&&state!=='processing';
        const openMedia=(item:DiscoverItem)=>{setMatching(null);onOpenMedia?.(item);};
        return <ListRow key={job.id} media={<img src="/preview/assets/nzb-icon.svg" alt=""/>} title={<TorrentName id={job.id} name={job.name} lookup={'/api/nzb/' + encodeURIComponent(job.id) + '/lookup'} mediaItem={job.mediaItem} onOpenMedia={onOpenMedia?openMedia:undefined}/>} badge={<DownloadState state={state} phaseLabel={label}/>} statusColour={activityStatusColour(state, label)} meta={<Mantine.Text size="sm" c="dimmed">NZB · Video{finished&&job.metadata_note?' · '+nativeMetadataSummary(job.metadata_note):''}</Mantine.Text>} progress={job.state === 'downloading' ? <Mantine.Progress value={Math.round(job.progress * 100)} aria-label={'Download progress for ' + job.name}/> : undefined} actions={<>
        {job.state === 'paused' ? <Button variant="secondary" disabled={!!saving} onClick={() => void act(job, 'resume')}>Resume</Button> : (job.state === 'downloading' || job.state === 'queued') && <Button variant="secondary" disabled={!!saving} onClick={() => void act(job, 'pause')}>Pause</Button>}
        {finished && <Button variant="secondary" disabled={!!saving} onClick={() => setMatching(job)}>Change metadata</Button>}
        {onOpenMedia&&<ActivityMediaMenuItem item={job.mediaItem} lookup={'/api/nzb/'+encodeURIComponent(job.id)+'/lookup'} onOpen={openMedia}/>}
        <Button variant="secondary" disabled={!!saving} onClick={() => void act(job, 'remove')}>Remove</Button>
      </>} details={<>
        {description && <span>{description}</span>}
        {(job.metadata_note || job.error) && <span>{job.metadata_note || job.error}</span>}
        {started && <span>Started: {started}</span>}
        <span>{rate(live ? job.down_rate : 0)} · {live ? job.connections : 0} connections</span>
        {(job.placements || []).length ? <span>Choose a Movies or TV Shows folder for this download.</span> : paths.length ? <span>Path: {paths.join(', ')}</span> : null}
      </>}/>;
    }
    const dialog = <Dialog open={!!matching} onOpenChange={open => {
            if (!open)
                setMatching(null);
        }}><DialogContent>{matching && <FixMatch url={'/api/nzb/' + encodeURIComponent(matching.id) + '/match'} path="" csrf={csrf} onChanged={refresh}/>}</DialogContent></Dialog>;
    const notice = (error || actionError) ? <Notice text={error || actionError} error/> : null;
    return { jobs, card, dialog, notice, stateOf, refresh };
}
