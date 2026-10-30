import surfaces from './components/MediaSurfaces.module.css';
import type { ReactNode } from 'react';
import { Card, Group, Stack, Text, ThemeIcon } from '@mantine/core';
import { ArrowRight, FileDown, Network } from 'lucide-react';
import { addDownloadEvent } from './appNavigation';
import { useTorrentJobs } from './TorrentDownloads';
import { useNzbJobs } from './NzbDownloads';
export function DashboardShortcut({ title, subtitle, detail, icon, onClick }: {
    title: string;
    subtitle: ReactNode;
    detail: ReactNode;
    icon: ReactNode;
    onClick: () => void;
}) {
    return <Card component="button" type="button" data-media-surface="tile" className={[surfaces.surface,surfaces.tile].filter(Boolean).join(' ')} withBorder radius="lg" p="md" onClick={onClick} style={{ textAlign: 'left', width: '100%', height: '100%', cursor: 'pointer', minWidth: 0 }}><Group wrap="nowrap" gap="md"><ThemeIcon variant="light" radius="md" size={52}>{icon}</ThemeIcon><Stack gap={4} style={{ flex: 1, minWidth: 0 }}><Text component="span" fw={600}>{title}</Text><Text component="span" size="sm" c="dimmed">{subtitle}</Text><Text component="span" size="xs">{detail}</Text></Stack><ArrowRight size={18} aria-hidden="true"/></Group></Card>;
}
export default function FutureDownloadTiles() {
    const torrents = useTorrentJobs();
    const nzbs = useNzbJobs();
    const activeTorrents = torrents.jobs.filter(job => job.state === 'downloading' || job.state === 'queued').length;
    const activeNzbs = nzbs.jobs.filter(job => job.state === 'downloading' || job.state === 'queued').length;
    return <>
    <DashboardShortcut title="Torrent Download" subtitle="Download using torrent files or magnet links." detail={activeTorrents ? `${activeTorrents} active` : 'Scan before download'} icon={<Network size={24}/>} onClick={() => window.dispatchEvent(new CustomEvent(addDownloadEvent, { detail: 'torrent' }))}/>
    <DashboardShortcut title="NZB Download" subtitle="Download from Usenet with NZB files." detail={activeNzbs ? `${activeNzbs} active` : 'Scan before download'} icon={<FileDown size={24}/>} onClick={() => window.dispatchEvent(new CustomEvent(addDownloadEvent, { detail: 'nzb' }))}/>
  </>;
}
