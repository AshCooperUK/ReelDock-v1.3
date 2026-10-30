import { NotificationMessage } from './notifications';
import { useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, X } from 'lucide-react';
import { Button } from './components/ui';
import { api } from './api';
import { Badge, Group, Progress, Stack, Text } from '@mantine/core';
import VideoFavourite from './VideoFavourite';
import WatchlistButton from './collections/WatchlistButton';
import DownloadPanel from './downloadUI/DownloadPanel';
type Job = {
    state: string;
    progress: number;
    message: string;
};
type DownloadState = {
    job: Job | null;
    saved?: boolean;
    quality: string;
    worker_online: boolean;
};
/** Favourite, Watchlist and download controls for one catalogue video. ReelDock never plays video. */
export default function VideoDownloadControls({ id, title, poster, csrf, canDownload = false, readOnly = false, localOnly = false, settingsOnlyDownload = false, showCollections = true }: {
    id: string;
    title: string;
    poster?: string;
    csrf?: string;
    canDownload?: boolean;
    readOnly?: boolean;
    localOnly?: boolean;
    settingsOnlyDownload?: boolean;
    showCollections?: boolean;
}) {
    const [download, setDownload] = useState<DownloadState | null>(null), [quality, setQuality] = useState(''), [busy, setBusy] = useState(false), [actionError, setActionError] = useState('');
    const [statusRevision, setStatusRevision] = useState(0), current = useRef(id);
    useEffect(() => { current.current = id; setDownload(null); setQuality(''); setActionError(''); setBusy(false); }, [id]);
    useEffect(() => {
        const c = new AbortController();
        let timer: ReturnType<typeof setTimeout>;
        async function poll() { try {
            const data = await api<DownloadState>('/api/downloads/' + encodeURIComponent(id), c.signal);
            if (c.signal.aborted)
                return;
            setDownload(data);
            setQuality(value => value || data.quality);
            if (['queued', 'downloading', 'cancelling'].includes(data.job?.state || ''))
                timer = setTimeout(poll, 5000);
        }
        catch (e) {
            if (!c.signal.aborted && canDownload && csrf)
                setActionError(e instanceof Error ? e.message : 'Could not read download status.');
        } }
        void poll();
        return () => { c.abort(); clearTimeout(timer); };
    }, [id, canDownload, csrf, statusRevision]);
    const active = ['queued', 'downloading', 'cancelling'].includes(download?.job?.state || '');
    const saved = !!download?.saved, writable = canDownload && !!csrf && !readOnly && !localOnly && !!download, showAction = writable && (active || !saved);
    async function act(action: 'queue' | 'cancel') {
        const target = id;
        if (busy || !writable || (action === 'queue' && (saved || active)) || (action === 'cancel' && !active))
            return;
        setBusy(true);
        setActionError('');
        try {
            await api('/api/downloads', undefined, { id, action, quality }, csrf);
            if (current.current === target)
                setStatusRevision(v => v + 1);
        }
        catch (e) {
            if (current.current === target)
                setActionError(e instanceof Error ? e.message : 'Could not update the download.');
        }
        finally {
            if (current.current === target)
                setBusy(false);
        }
    }
    return <Stack component="section" gap="md" aria-label="Video actions">
  {localOnly && download && !saved && <NotificationMessage kind="error" message="This video is no longer saved locally."/>}
  <Group gap="sm" align="flex-start">{showCollections && <><VideoFavourite id={id} disabled={readOnly}/><WatchlistButton compact disabled={readOnly} item={{ id: 'youtube:video:' + id, kind: 'video', videoId: id, title, image: poster }}/></>}{saved && <Badge variant="light" color="green">Saved video</Badge>}
  {settingsOnlyDownload && showAction && <Button variant="secondary" aria-label={active ? 'Cancel download' : 'Download video'} title={active ? 'Cancel download' : 'Download video'} disabled={busy || download?.job?.state === 'cancelling'} onClick={() => void act(active ? 'cancel' : 'queue')}>{active ? <X size={18}/> : <ArrowDownToLine size={18}/>}</Button>}
  {showAction && !settingsOnlyDownload && <DownloadPanel compact quality={quality} qualities={['480', '720', '1080', '2160']} onQuality={active ? undefined : setQuality} busy={busy} disabled={download?.job?.state === 'cancelling'} action={active ? 'Cancel download' : 'Download'} onDownload={() => void act(active ? 'cancel' : 'queue')}/>}</Group>
  {active && <Stack gap="xs"><Text size="sm" role="status">{download?.job?.state === 'downloading' ? 'Downloading' : download?.job?.state === 'cancelling' ? 'Cancelling…' : 'Queued'}{!download?.worker_online ? ' · Waiting for worker' : ''}</Text>{download?.job?.state === 'downloading' && <Progress value={download.job.progress} aria-label="Download progress"/>}</Stack>}
  {actionError && <NotificationMessage kind="error" message={actionError} actions={<> <Button variant="secondary" onClick={() => { setActionError(''); setStatusRevision(v => v + 1); }}>Check status</Button> </>}/>}
  {download?.job?.state === 'failed' && <NotificationMessage kind="error" message={download.job.message || 'Download failed. Try again.'}/>}
 </Stack>;
}
