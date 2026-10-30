import { Text } from '@mantine/core';
import { NotificationMessage } from '../notifications';
import { useEffect, useState } from 'react';
import { api } from '../api';
import { Button } from '../components/ui';
import VideoDownloadControls from '../VideoDownloadControls';
import type { MediaCard } from './types';

export function searchVideoItem(item:MediaCard) {
  if(item.action.type!=='youtube')throw new Error('Choose a YouTube result.');
  return {id:'youtube:video:'+item.action.id,videoId:item.action.id,kind:'video',
    title:item.title.slice(0,300),channelId:item.channelId,channel:item.channel?.slice(0,180),
    image:item.image?.slice(0,400),description:item.description?.slice(0,800),
    published:item.date,publishedApproximate:!!item.dateApproximate};
}

/** Public search results must exist locally before any download/status request runs. */
export default function SearchYouTubeDownload({item,csrf}:{item:MediaCard;csrf:string}) {
  const [ready,setReady]=useState(false),[error,setError]=useState(''),[revision,setRevision]=useState(0);
  const identity=item.action.type==='youtube'?item.action.id:'';
  useEffect(()=>{
    const c=new AbortController();setReady(false);setError('');
    api('/api/discover/youtube/register',c.signal,{item:searchVideoItem(item)},csrf)
      .then(()=>{if(!c.signal.aborted)setReady(true);})
      .catch(e=>{if(!c.signal.aborted)setError(e instanceof Error?e.message:'Could not prepare this video.');});
    return()=>c.abort();
  },[identity,csrf,revision]);
  if(error)return <NotificationMessage kind="error" message={error} actions={<> <Button variant="secondary" onClick={()=>setRevision(n=>n+1)}>Try again</Button> </>}/>;
  return ready?<VideoDownloadControls id={identity} title={item.title} poster={item.image} csrf={csrf} canDownload showCollections={false}/>:<Text size="sm" role="status">Preparing download options…</Text>;
}
