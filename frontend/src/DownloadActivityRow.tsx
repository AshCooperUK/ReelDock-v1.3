import surfaces from './components/MediaSurfaces.module.css';
import VideoTitle from './VideoTitle';
import ChannelIdentity from './ChannelIdentity';
import { useEffect, useState, type ReactNode } from 'react';
import { Box, HoverCard, Image, Loader, Progress, Stack, Text, ThemeIcon } from '@mantine/core';
import ListRow from './components/ListRow';
import { ActivityStatus, activityStatusColour } from './components/ActivityStatus';
import { Film } from 'lucide-react';
import { downloadLabel, type ActivityPreview } from './downloadFeed';
import type { ActivityEntry } from './activityView';
import { downloadPicture as picture } from './downloadArtwork';
export function MediaHover({ name, card, children, actions }: {
    name: string;
    card: ActivityPreview;
    children: ReactNode;
    actions?: ReactNode;
    onClick?:()=>void;
}) {
    const [failedImages, setFailedImages] = useState<string[]>([]);
    const [suppressed,setSuppressed]=useState(false);
    useEffect(()=>{const close=()=>setSuppressed(true);window.addEventListener('reeldock-overlay-open',close);return()=>window.removeEventListener('reeldock-overlay-open',close);},[]);
    const image = [picture(card.image), picture(card.fallbackImage)].find(src => src && !failedImages.includes(src));
    return <HoverCard interactive role="dialog" disabled={suppressed} width={360} openDelay={300} closeDelay={180} withinPortal position="bottom-start"><HoverCard.Target><Box component="span" onMouseEnter={()=>setSuppressed(false)} onFocus={()=>setSuppressed(false)} style={{ display: 'inline-block', maxWidth: '100%' }}>{children}</Box></HoverCard.Target><HoverCard.Dropdown data-media-surface="preview" className={[surfaces.surface,surfaces.preview].filter(Boolean).join(' ')} maw="calc(100vw - 32px)"><Stack gap="sm">{image ? <Image src={image} alt="" h={150} radius="md" fit="cover" referrerPolicy="no-referrer" onError={() => setFailedImages(previous => [...previous, image])}/> : <ThemeIcon variant="light" size={64}><Film size={28}/></ThemeIcon>}<Text fw={600}>{card.title || name}</Text>{card.channel && <Text size="sm" c="dimmed">{card.channel}</Text>}{card.overview && <Text size="sm" lineClamp={5}>{card.overview}</Text>}{actions}</Stack></HoverCard.Dropdown></HoverCard>;
}
export function YouTubeActivityRow({ entry, csrf, actions }: {
    entry: ActivityEntry;
    csrf: string;
    actions?: ReactNode;
}) {
    if (entry.kind === 'download') {
        const download = entry.item;
        const card: ActivityPreview = { title: download.title, channel: download.channel_name, overview: download.description, image: picture(download.video?.thumbnail) || picture(download.banner), fallbackImage: /^[A-Za-z0-9_-]{11}$/.test(download.id) ? 'https://i.ytimg.com/vi/' + download.id + '/hqdefault.jpg' : undefined };
        const title = <VideoTitle id={download.id} title={download.title} channel={download.channel} channelName={download.channel_name} avatar={download.avatar} poster={picture(download.video?.thumbnail)} csrf={csrf}/>;
        const phase = entry.state === 'queued' ? 'Queued' : downloadLabel(download), state = entry.state === 'queued' ? 'queued' : 'downloading';
        return <ListRow title={<MediaHover name={download.title} card={card}>{title}</MediaHover>} media={<Image src={card.image} alt="" fallbackSrc={card.fallbackImage}/>} badge={<ActivityStatus state={state} phaseLabel={phase}/>} statusColour={activityStatusColour(state, phase)} actions={actions} meta={<ChannelIdentity id={download.channel} name={download.channel_name || 'Unknown channel'} avatar={download.avatar}/>} progress={entry.state === 'running' && (typeof download.progress === 'number' ? <Progress value={download.progress} aria-label={'Download progress: ' + download.title}/> : <Loader size="sm" aria-label={'Working on ' + download.title}/>)} details={download.message ? <Text size="sm">{download.message}</Text> : undefined}/>;
    }
    return null;
}
