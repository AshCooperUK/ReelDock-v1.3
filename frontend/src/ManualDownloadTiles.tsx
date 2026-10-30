import { Group, Paper, Progress, Stack, Text, Title } from '@mantine/core';
import ListRow, { CompactActivityRows } from './components/ListRow';
import { ActivityStatus, activityStatusColour } from './components/ActivityStatus';
import { UnstyledButton as ControlTrigger } from '@mantine/core';
import { NotificationMessage } from './notifications';
import { lazy, Suspense, useState } from 'react';
import { DetailSheet } from './components/DetailSheet';
import { Film, Youtube } from 'lucide-react';
import { api } from './api';
import { Button } from './components/ui';
import { YouTubeActivityRow, MediaHover } from './DownloadActivityRow';
import { manualYoutubeEntries } from './activityView';
import { refreshDownloads, useDownloadFeed } from './downloadFeed';
import { useJobs, type Job } from './M3uDownloads';
const M3uDetails = lazy(() => import('./M3uLibrary').then(module => ({ default: module.M3uDetails })));
export function YoutubeDownloadProgress({ csrf }: {
    csrf: string;
}) {
    const feed = useDownloadFeed(), entries = manualYoutubeEntries(feed.items);
    const [busy, setBusy] = useState(''), [error, setError] = useState('');
    async function cancel(id: string) {
        setBusy(id);
        setError('');
        try {
            await api('/api/downloads', undefined, { id, action: 'cancel' }, csrf);
            await refreshDownloads();
        }
        catch (e) {
            setError(e instanceof Error ? e.message : 'Could not cancel download.');
        }
        finally {
            setBusy('');
        }
    }
    if (!entries.length)
        return null;
    return <Paper component="section" withBorder radius="lg" p="md" aria-label="YouTube downloads"><Stack gap="sm"><Group gap="xs"><Youtube size={18}/><Title order={3} size="h5">YouTube downloads</Title></Group><CompactActivityRows>{entries.map(entry => <YouTubeActivityRow key={entry.key} entry={entry} csrf={csrf} actions={<Button variant="secondary" disabled={!!busy || entry.item.state === 'cancelling'} onClick={() => void cancel(entry.item.id)}>{entry.item.state === 'cancelling' ? 'Cancelling…' : 'Cancel'}</Button>}/>)}</CompactActivityRows>{(error || feed.error) && <NotificationMessage kind="error" message={error || feed.error}/>}</Stack></Paper>;
}
export function VodDownloadProgress({ csrf }: {
    csrf: string;
}) {
    const jobs = useJobs(), active = jobs.data.filter(job => ['queued', 'running', 'cancelling'].includes(job.state));
    const [busy, setBusy] = useState(''), [error, setError] = useState(''), [selected, setSelected] = useState<Job | null>(null);
    async function cancel(id: string) {
        setBusy(id);
        setError('');
        try {
            await api('/api/m3u-server-downloads', undefined, { id, action: 'cancel' }, csrf);
        }
        catch (e) {
            setError(e instanceof Error ? e.message : 'Could not cancel download.');
        }
        finally {
            setBusy('');
        }
    }
    if (!active.length && !jobs.error && !selected)
        return null;
    return <><Paper component="section" withBorder radius="lg" p="md" aria-label="VOD downloads"><Stack gap="sm"><Group gap="xs"><Film size={18}/><Title order={3} size="h5">VOD downloads</Title></Group><CompactActivityRows>{active.map(job => <ListRow key={job.id} title={<MediaHover name={job.title} card={{ title: job.title, overview: job.description, image: job.artwork }}><ControlTrigger type="button" mih={44} onClick={() => setSelected(job)}>{job.title}</ControlTrigger></MediaHover>} media={<Film size={24}/>} badge={<ActivityStatus state={job.state}/>} statusColour={activityStatusColour(job.state)} progress={<Progress value={job.progress ?? 0} aria-label={'Download progress for ' + job.title}/>} details={job.message ? <Text size="sm">{job.message}</Text> : undefined} actions={<Button variant="secondary" disabled={!!busy || job.state === 'cancelling'} onClick={() => void cancel(job.id)}>{job.state === 'cancelling' ? 'Cancelling…' : 'Cancel'}</Button>}/>)}</CompactActivityRows>{(error || jobs.error) && <NotificationMessage kind="error" message={error || jobs.error}/>}</Stack></Paper>{selected && <Suspense fallback={<DetailSheet open title={selected.title} onOpenChange={open => { if (!open)
        setSelected(null); }}><Text role="status">Opening media details…</Text></DetailSheet>}><M3uDetails source={selected.source} item={selected} onClose={() => setSelected(null)}/></Suspense>}</>;
}
