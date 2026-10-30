import * as Mantine from '@mantine/core';
import { notify } from '../notifications';
import { useState } from 'react';
import { Youtube } from 'lucide-react';
import { api } from '../api';
import { Button } from './ui';
import DownloadPanel from '../downloadUI/DownloadPanel';
import { Field } from '../SettingsUI';
import { TextInput } from '../TextField';
type VideoItem = {
    kind: 'video';
    id: string;
    videoId: string;
    title: string;
    channel?: string;
    channelId?: string;
    image?: string;
    description?: string;
    published?: string;
    publishedApproximate?: boolean;
};
type Resolved = {
    item: VideoItem;
    saved?: boolean;
    state?: string;
};
const message = (error: unknown) => error instanceof Error ? error.message : 'Something went wrong. Try again.';
/** Paste a YouTube video address, confirm the match, then queue it. */
export default function YouTubeDownload({ csrf, onStarted, onOpenChange }: {
    csrf: string;
    onStarted?: () => void;
    onOpenChange: (open: boolean) => void;
}) {
    const [url, setUrl] = useState('');
    const [found, setFound] = useState<Resolved | null>(null);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [quality, setQuality] = useState('1080');
    const [fallback, setFallback] = useState(true);
    async function check() {
        setBusy('check');
        setError('');
        setNotice('');
        setFound(null);
        try {
            setFound(await api<Resolved>('/api/discover/youtube/resolve', undefined, { url }, csrf));
        }
        catch (e) {
            setError(message(e));
        }
        finally {
            setBusy('');
        }
    }
    async function start() {
        if (!found)
            return;
        setBusy('start');
        setError('');
        setNotice('');
        try {
            const result = await api<{
                requests?: {
                    state: string;
                    message?: string;
                }[];
            }>('/api/discover/acquire', undefined, { item: found.item, quality, fallback, requestId: crypto.randomUUID() }, csrf);
            const queued = result.requests?.[0];
            notify({ kind: queued?.state === 'failed' ? 'error' : 'success', message: queued?.message || 'Download queued.' });
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
    const active = found ? found.saved || ['queued', 'downloading', 'cancelling'].includes(found.state || '') : false;
    return <Mantine.Stack onSubmit={event => { event.preventDefault();if(busy)return;if(found){if(!active)void start();}else void check(); }} gap="md" component="form" style={{flex:'1 0 auto',minWidth:0,width:'100%'}}>
    <DownloadPanel fill quality={quality} qualities={['720', '1080', '2160']} onQuality={found&&!active?setQuality:undefined} fallback={fallback} onFallback={found&&!active?setFallback:undefined} status={notice} error={error} actions={found?<><Button type="button" variant="secondary" disabled={!!busy} onClick={()=>{setFound(null);setError('');setNotice('');}}>Change source</Button><Button type="submit" style={{minWidth:160,height:44}} disabled={!!busy||active}>{busy==='start'?'Starting…':active?found.saved?'Saved in library':'Already in Activity':'Start download'}</Button></>:<Button type="submit" style={{minWidth:160,height:44}} disabled={!!busy||!url.trim()}>{busy==='check'?'Checking…':'Check video'}</Button>}>
      <Mantine.Stack gap="md">
        {!found&&<Field label="YouTube address" help="Paste the address of one YouTube video — watch, share (youtu.be), Shorts or Live links all work. The video saves into your library under its channel name.">
          <TextInput aria-label="YouTube address" type="text" placeholder="https://www.youtube.com/watch?v=… or https://youtu.be/…" value={url} onChange={event => { setUrl(event.target.value); setFound(null); }}/>
        </Field>}
        {found&&<Mantine.Paper role="status" withBorder p="md" radius="md"><Mantine.Group wrap="nowrap" align="flex-start" gap="md"><Mantine.Box w={72} h={72} style={{flexShrink:0}}>{found.item.image?<Mantine.Image w={72} h={72} radius="sm" fit="cover" src={found.item.image} alt="" onError={event=>{event.currentTarget.style.visibility='hidden';}}/>:<Mantine.ThemeIcon size={72} variant="light" radius="sm"><Youtube size={28}/></Mantine.ThemeIcon>}</Mantine.Box><Mantine.Stack gap={4} style={{minWidth:0}}><Mantine.Text fw={600}>{found.item.title}</Mantine.Text><Mantine.Text size="sm" c="dimmed">{found.item.channel||'YouTube'}{found.item.published?' · '+found.item.published:''}</Mantine.Text>{active&&<Mantine.Badge variant="light" color={found.saved?'green':'blue'}>{found.saved?'Already saved':'Already queued or downloading'}</Mantine.Badge>}</Mantine.Stack></Mantine.Group></Mantine.Paper>}
      </Mantine.Stack>
    </DownloadPanel>
  </Mantine.Stack>;
}
