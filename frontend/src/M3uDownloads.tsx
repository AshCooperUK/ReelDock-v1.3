import DownloadPanel from './downloadUI/DownloadPanel';
import { useEffect, useState } from 'react';
import { Button } from './components/ui';
import { api } from './api';
import { Notice, useAction } from './SettingsUI';
import { onVisible, pageHidden } from './visibility';
export type Job = {
    id: string;
    source: string;
    path: string;
    title: string;
    kind: string;
    state: string;
    message: string;
    destination: string;
    progress: number | null;
    created: number;
    updated: number;
    artwork: string;
    subscription: string;
    description: string;
    sourceType?: 'strm';
    sourceName?: string;
};
const active = (state: string) => ['queued', 'running', 'cancelling'].includes(state);
export function useJobs() {
    const [data, setData] = useState<Job[]>([]), [error, setError] = useState('');
    useEffect(() => { const c = new AbortController(); let timer: ReturnType<typeof setTimeout>, busy = false; async function load() { if (busy)
        return; busy = true; clearTimeout(timer); try {
        const r = await api<{
            items: Job[];
        }>('/api/m3u-server-downloads', c.signal);
        if (!c.signal.aborted) {
            setData(r.items);
            setError('');
        }
    }
    catch (e) {
        if (!c.signal.aborted)
            setError(e instanceof Error ? e.message : 'Download status unavailable.');
    }
    finally {
        busy = false;
        if (!c.signal.aborted && !pageHidden())
            timer = setTimeout(load, 2000);
    } } void load(); const stopVisible = onVisible(() => void load()); return () => { c.abort(); clearTimeout(timer); stopVisible(); }; }, []);
    return { data, error };
}
export function ServerDownload({ csrf, source, path }: {
    csrf: string;
    source: string;
    path: string;
}) {
    const a = useAction(csrf), jobs = useJobs(), job = jobs.data.find(j => j.source === source && j.path === path);
    return <DownloadPanel busy={a.busy} disabled={!!job && (active(job.state) || job.state === 'complete')} onDownload={() => void a.run('/api/m3u-server-downloads', { action: 'download', source, path }, 'Queued for server download.')} action={job?.state === 'complete' ? 'Saved on server' : job && active(job.state) ? 'Download queued / running' : 'Download to server'} status={a.notice} error={a.error || jobs.error} actions={job && active(job.state) && <Button variant="secondary" disabled={a.busy || job.state === 'cancelling'} onClick={() => void a.run('/api/m3u-server-downloads', { action: 'cancel', id: job.id }, 'Cancellation requested.')}>Cancel server download</Button>}/>;
}
