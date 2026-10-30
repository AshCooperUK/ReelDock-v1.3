import { useEffect, useRef, useState } from 'react';
import { Box, Tabs } from '@mantine/core';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from './ui';
import { addDownloadEvent, type AddDownloadKind } from '../appNavigation';
import { TorrentAddDialog } from '../TorrentDownloads';
import { NzbAddDialog } from '../NzbDownloads';
import YouTubeDownload from './YouTubeDownload';
import StrmDownload from './StrmDownload';
const descriptions: Record<AddDownloadKind, string> = {
    torrent: 'Add a torrent — paste an address or drop a file.',
    nzb: 'Add an NZB — paste an address or drop a file.',
    youtube: 'Add a YouTube video — paste its address.',
    strm: 'Add a STRM file — check its video link, then download the media.',
};
/** One "Add download" entry point hosting the torrent, NZB and YouTube forms as tabs. */
export default function AddDownload({ open, onOpenChange, csrf, onStarted }: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    csrf: string;
    onStarted?: () => void;
}) {
    const [kind, setKind] = useState<AddDownloadKind>('torrent');
    const scrollRef=useRef<HTMLDivElement>(null);
    useEffect(()=>{if(open&&scrollRef.current)scrollRef.current.scrollTop=0;},[kind,open]);
    // Other surfaces (Home tiles) open this dialog preselected on their type.
    useEffect(() => {
        const choose = (event: Event) => {
            const detail = (event as CustomEvent<AddDownloadKind>).detail;
            if (detail === 'torrent' || detail === 'nzb' || detail === 'youtube' || detail === 'strm')
                setKind(detail);
            onOpenChange(true);
        };
        window.addEventListener(addDownloadEvent, choose);
        return () => window.removeEventListener(addDownloadEvent, choose);
    }, [onOpenChange]);
    const panelStyle=(value:AddDownloadKind)=>({ minWidth:0, flex:1, display:kind===value?'flex':'none', flexDirection:'column' as const });
    return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent stableHeight scrollRef={scrollRef}>
    <Tabs value={kind} onChange={value => {
            if (value)
                setKind(value as AddDownloadKind);
        }} keepMounted={false} style={{ display: 'flex', flexDirection: 'column', flex:1, minWidth:0 }}>
      <Box component="header" mb="md" style={{ flexShrink: 0 }}><DialogTitle>Add download</DialogTitle><DialogDescription>{descriptions[kind]}</DialogDescription>
        <Tabs.List aria-label="Download type" mt="md" grow>
          <Tabs.Tab value="torrent" mih={44} px={{base:4,sm:12}} style={{flex:'1 1 0',minWidth:0}}>Torrent</Tabs.Tab>
          <Tabs.Tab value="nzb" mih={44} px={{base:4,sm:12}} style={{flex:'1 1 0',minWidth:0}}>NZB</Tabs.Tab>
          <Tabs.Tab value="youtube" mih={44} px={{base:4,sm:12}} style={{flex:'1 1 0',minWidth:0}}>YouTube</Tabs.Tab>
          <Tabs.Tab value="strm" mih={44} px={{base:4,sm:12}} style={{flex:'1 1 0',minWidth:0}}>STRM</Tabs.Tab>
        </Tabs.List>
      </Box>
      <Tabs.Panel value="torrent" style={panelStyle('torrent')}><TorrentAddDialog bare open={open} onOpenChange={onOpenChange} csrf={csrf} onStarted={onStarted}/></Tabs.Panel>
      <Tabs.Panel value="nzb" style={panelStyle('nzb')}><NzbAddDialog bare open={open} onOpenChange={onOpenChange} csrf={csrf} onStarted={onStarted}/></Tabs.Panel>
      <Tabs.Panel value="youtube" style={panelStyle('youtube')}><YouTubeDownload csrf={csrf} onStarted={onStarted} onOpenChange={onOpenChange}/></Tabs.Panel>
      <Tabs.Panel value="strm" style={panelStyle('strm')}><StrmDownload csrf={csrf} onStarted={onStarted} onOpenChange={onOpenChange}/></Tabs.Panel>
    </Tabs>
  </DialogContent></Dialog>;
}
