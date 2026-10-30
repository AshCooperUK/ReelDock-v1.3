import { NotificationMessage } from './notifications';
import { api } from './api';
import { useEffect, useRef, useState } from 'react';
import ChannelAvatar from './ChannelAvatar';
import { ChannelUploads, type Info, type SheetVideo } from './ChannelInfo';
import ChannelOverview from './ChannelOverview';
import ChannelForecasts from './ChannelForecasts';
import ChannelHistory from './ChannelHistory';
import { Anchor, Text } from '@mantine/core';
import ChannelOptions, { type Subscription } from './ChannelOptions';
import VideoSheet from './VideoSheet';
import VideoDownloadControls from './VideoDownloadControls';
import type { ChannelTarget } from './channelNavigation';
import { Button } from './components/ui';
import { DetailSheet, type DetailSheetTab } from './components/DetailSheet';
import { Loading, useSettings } from './SettingsUI';
import TileActions from './collections/TileActions';
import { channelRefreshEvent, type ChannelRefresh } from './channelRefresh';

export default function ChannelDetail({target,readOnly,mode,close}:{target:ChannelTarget;readOnly:boolean;mode:(m:string)=>void;close:()=>void}){
 const r=useSettings<{items:Subscription[];csrf:string}>('/api/subscriptions?channel='+encodeURIComponent(target.id));
 const remembered=useRef<{id:string;data:NonNullable<typeof r.data>}|null>(null);
 if(r.data)remembered.current={id:target.id,data:r.data};
 const subscription=r.data||(remembered.current?.id===target.id?remembered.current.data:null);
 const channel=subscription?.items[0];
 const reload=useRef(r.reload);reload.current=r.reload;
 useEffect(()=>{const changed=(event:Event)=>{if((event as CustomEvent<{key?:string}>).detail?.key===target.id)reload.current();};window.addEventListener('reeldock-watchlist-changed',changed);return()=>window.removeEventListener('reeldock-watchlist-changed',changed);},[target.id]);
 const publicInfo=useSettings<{channel:Info;csrf:string;metadata?:ChannelRefresh}>('/api/guide/channel/'+encodeURIComponent(target.id)+'/info');
 const rememberedInfo=useRef<{id:string;data:NonNullable<typeof publicInfo.data>}|null>(null);
 if(publicInfo.data)rememberedInfo.current={id:target.id,data:publicInfo.data};
 const details=publicInfo.data||(rememberedInfo.current?.id===target.id?rememberedInfo.current.data:null);
 const infoReload=useRef(publicInfo.reload);infoReload.current=publicInfo.reload;
 const [metadata,setMetadata]=useState<ChannelRefresh>();
 useEffect(()=>{if(publicInfo.data?.metadata)setMetadata(publicInfo.data.metadata);},[publicInfo.data]);
 useEffect(()=>{if(!metadata?.refreshAfterMs)return;const timer=window.setInterval(()=>{if(!document.hidden)infoReload.current();},Math.max(1000,Math.min(5000,metadata.refreshAfterMs)));return()=>window.clearInterval(timer);},[target.id,metadata]);
 const requested=useRef<{id:string;controller:AbortController}|null>(null),[refreshError,setRefreshError]=useState('');
 useEffect(()=>{
  if(readOnly||!channel||channel.id!==target.id||!subscription?.csrf||requested.current?.id===target.id)return;
  const controller=new AbortController(),owner={id:target.id,controller};let acknowledged=false;requested.current=owner;setRefreshError('');
  api<{refresh:ChannelRefresh;metadata?:ChannelRefresh}>('/api/guide/channel/'+encodeURIComponent(target.id),controller.signal,{action:'prioritise_recent'},subscription.csrf,{feedback:'inline'}).then(result=>{
   if(!controller.signal.aborted&&requested.current===owner){acknowledged=true;if(result.metadata)setMetadata(result.metadata);infoReload.current();window.dispatchEvent(new CustomEvent(channelRefreshEvent,{detail:{id:target.id,refresh:result.refresh}}));}
  }).catch(error=>{if(!controller.signal.aborted&&requested.current===owner)setRefreshError(error instanceof Error?error.message:'This channel could not be moved ahead. Saved information is still available.');});
  return()=>{controller.abort();if(!acknowledged&&requested.current===owner)requested.current=null;};
 },[target.id,channel?.id,subscription?.csrf,readOnly]);
 const [subscribing,setSubscribing]=useState(false),[subscriptionError,setSubscriptionError]=useState('');
 // A video opened from the uploads list swaps the channel sheet for the shared
 // video sheet (one overlay; SheetBack / the close X returns to the channel).
 const [video,setVideo]=useState<SheetVideo|null>(null);
 useEffect(()=>{
  if(!video?.metadata_pending)return;
  const id=video.id,c=new AbortController();let timer:ReturnType<typeof setTimeout>;
  const poll=async()=>{if(document.hidden)return;try{const result=await api<{details?:SheetVideo[]}>('/api/download-status?ids='+encodeURIComponent(id),c.signal,undefined,undefined,{feedback:'inline'});if(c.signal.aborted)return;const fresh=result.details?.find(item=>item.id===id&&!item.metadata_pending);if(fresh){setVideo(old=>old?.id===id?{...old,...fresh}:old);return;}}catch{/* Saved details remain usable during a quiet metadata retry. */}if(!c.signal.aborted)timer=setTimeout(poll,2000);};
  const visible=()=>{if(!document.hidden)void poll();};void poll();document.addEventListener('visibilitychange',visible);return()=>{c.abort();clearTimeout(timer);document.removeEventListener('visibilitychange',visible);};
 },[video?.id,video?.metadata_pending]);
 async function subscribe(){if(!r.data||subscribing)return;setSubscribing(true);setSubscriptionError('');try{await api('/api/discover/subscribe',undefined,{action:'subscribe',item:{id:'youtube:channel:'+target.id,kind:'channel',channelId:target.id,title:details?.channel.name||target.name||'YouTube channel',image:details?.channel.avatar||target.avatar}},r.data.csrf);r.reload();window.dispatchEvent(new Event('reeldock-channel-changed'));}catch(e){setSubscriptionError(e instanceof Error?e.message:'Could not add this channel.');}finally{setSubscribing(false);}}
 const changed=(updated:Subscription)=>{r.setData(old=>old?{...old,items:[updated]}:old);window.dispatchEvent(new CustomEvent('reeldock-channel-changed',{detail:updated}));};
 const name=channel?.name||details?.channel.name||target.name||'Channel';
 const info:Info|undefined=details?.channel||(channel as unknown as Info|undefined);
 const loadingBody=!subscription?<Loading error={r.error} retry={r.reload}/>:!info&&!publicInfo.error?<Loading error="" retry={publicInfo.reload}/>:null;
 const overview=<>
  <Anchor fw={600} href={`https://www.youtube.com/channel/${target.id}`} target="_blank" rel="noopener noreferrer">{name}</Anchor>
  {readOnly&&<Text size="sm" c="dimmed">Layout preview · Channel settings and downloads are read-only.</Text>}
  {subscriptionError&&<NotificationMessage kind="error" message={subscriptionError}/>}
  {info?<ChannelOverview channel={info}/>:loadingBody}
  {publicInfo.error&&<NotificationMessage kind="error" message={publicInfo.error}/>}
</>;

 const releases=<>
  {readOnly&&<Text size="sm" c="dimmed">Layout preview · Channel settings and downloads are read-only.</Text>}
  {loadingBody||((channel||details)&&<><ChannelForecasts id={target.id}/><ChannelUploads id={target.id} name={name} csrf={subscription?.csrf||details?.csrf||''} isPublic={!channel&&details?.channel.public} readOnly={readOnly} onVideoOpen={setVideo} title="Uploads"/></>)}
  {publicInfo.error&&<NotificationMessage kind="error" message={publicInfo.error}/>}
 </>;
 // Opening this tab never saves, subscribes or toggles anything; readOnly and
 // unsubscribed channels get an honest panel instead of writable controls.
 const settings=readOnly?<Text size="sm" c="dimmed">Layout preview · Channel settings are read-only here.</Text>
  :!r.data?<Loading error={r.error} retry={r.reload}/>
  :!channel?<Text c="dimmed">Add this channel to ReelDock to choose its download settings. Opening this tab saves nothing.</Text>
  :<><ChannelOptions embedded showFavourite={false} channel={channel} csrf={r.data.csrf} onChange={changed} onRemoved={()=>{window.dispatchEvent(new CustomEvent('reeldock-channel-removed',{detail:target.id}));}} onClose={close}/></>;
 const tabs:DetailSheetTab[]=[
  {id:'overview',label:'Overview',content:overview},
  {id:'downloads',label:'Downloads',content:releases},
  {id:'manage',label:'Manage',content:settings},
  {id:'history',label:'History',content:<ChannelHistory id={target.id}/>},
 ];
 const active=tabOf(target.mode);
 if(video){
  const videoCsrf=details?.csrf||r.data?.csrf||'';
  const videoDownloads=<VideoDownloadControls key={video.id} id={video.id} title={video.title} poster={video.image||video.thumbnail} csrf={videoCsrf} canDownload={!readOnly&&video.state==='published'} readOnly={readOnly} showCollections={false}/>;
  return <VideoSheet csrf={videoCsrf} open onOpenChange={open=>{if(!open)setVideo(null);}}
   video={{...video,id:video.id,title:video.title,image:video.image||video.thumbnail,banner:details?.channel.banner||channel?.banner,channel:name,channelId:target.id,avatar:channel?.avatar||details?.channel.avatar||target.avatar}}
   readOnly={readOnly}
   // No in-sheet back link: the sheet's close X already returns to the channel.

   actions={<TileActions readOnly={readOnly} item={{kind:'video',id:'video:'+video.id,videoId:video.id,title:video.title,image:video.image||video.thumbnail,channel:name,channelId:target.id}}/>}
   downloads={video.metadata_pending&&video.state==='unknown'?<><Text size="sm" c="dimmed" role="status">Checking video details…</Text>{videoDownloads}</>:videoDownloads}
   description={video.description}/>;
 }
 const avatar=channel?.avatar||details?.channel.avatar||target.avatar;
 const handle=channel?.handle||details?.channel.handle;
 return <DetailSheet open closeOnChannelNavigation={false} onOpenChange={open=>{if(!open)close();}}
  banner={details?.channel.banner||channel?.banner}
  media={<ChannelAvatar src={avatar} detail/>}
  title={name}
  subtitle={<>{handle&&<>{handle} · </>}<Anchor href={`https://www.youtube.com/channel/${encodeURIComponent(target.id)}`} target="_blank" rel="noopener noreferrer" underline="always" aria-label={`Open ${name} on YouTube (opens in a new tab)`}>YouTube Channel</Anchor>{r.data&&<> · {channel?'Channel added':'Not added'}</>}</>}
  actions={<><TileActions readOnly={readOnly} item={{id:target.id,kind:'channel',channelId:target.id,title:name,image:avatar}}/>{!channel&&r.data&&<Button disabled={subscribing||readOnly} onClick={()=>void subscribe()}>{subscribing?'Adding…':'Add channel'}</Button>}</>}
  tabs={tabs}
  footer={refreshError?<NotificationMessage kind="error" message={refreshError}/>:undefined}
  tab={active}
  onTab={mode}
 />;
}
const tabOf=(mode?:string)=>['options','settings','manage'].includes(mode||'')?'manage':['releases','downloads'].includes(mode||'')?'downloads':mode==='history'?'history':'overview';
