import { UnstyledButton as ControlTrigger } from '@mantine/core';
import { lazy, Suspense, useState } from 'react';
import { Loader } from '@mantine/core';
import TileActions from './collections/TileActions';
import type {MediaDuration} from './mediaDuration';
const VideoDownloadControls=lazy(()=>import('./VideoDownloadControls'));
const VideoSheet=lazy(()=>import('./VideoSheet'));

/** All video-title controls open the same information sheet with download controls. */
export default function VideoTitle({ id, title, channel, channelName, avatar, poster, csrf, onOpen, length }: { id: string; title: string; channel?: string; channelName?: string; avatar?: string; poster?: string; csrf?: string; onOpen?: () => void; length?:MediaDuration }) {
  const [open, setOpen] = useState(false);
  // Inside another detail sheet the caller takes over opening the video so the
  // detail surface never stacks a second overlay (channel sheet → video view).
  return <><ControlTrigger type="button" mih={44} w="100%" style={{textAlign:"left",fontWeight:600,overflowWrap:"anywhere"}} onClick={() => onOpen ? onOpen() : setOpen(true)} aria-label={'Open ' + title}>{title}</ControlTrigger>
    {!onOpen && open && <Suspense fallback={<Loader size="sm" aria-label="Loading video details"/>}><VideoSheet csrf={csrf||''} open={open} onOpenChange={setOpen} canManage={!!csrf}
      video={{...length,id,title,image:poster,channel:channelName||'Unknown channel',channelId:channel,avatar}}
      actions={csrf&&<TileActions item={{kind:'video',id:'video:'+id,videoId:id,title,image:poster,channel:channelName,channelId:channel}}/>}
      downloads={<VideoDownloadControls id={id} title={title} poster={poster} showCollections={!csrf}/>}/></Suspense>}</>;
}
