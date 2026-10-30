import { MediaArtwork, MediaCard, MediaGrid } from './components/browse';
import { Group, Stack, Switch, Text, Title, UnstyledButton } from '@mantine/core';
import { NotificationMessage } from './notifications';
import { mediaDate } from './mediaDate';
import { mediaLength, mediaSeconds, type MediaDuration } from './mediaDuration';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from './components/ui';
import VideoSheet from './VideoSheet';
import { api } from './api';
import VideoDownloadControls from './VideoDownloadControls';
import TileActions from './collections/TileActions';
import { channelRefreshDelay, channelRefreshEvent, channelUploadPage, mergeChannelUploads, type ChannelBrowse, type ChannelRefresh } from './channelRefresh';
export type ChannelVideoItem=MediaDuration&{id:string;title:string;state:string;date:string;duration?:string;thumbnail?:string;description?:string;kind?:string};
type Item=ChannelVideoItem;
export const showShortsPreference='reeldock-channel-show-shorts';
export function visibleChannelUploads<T extends MediaDuration&{kind?:string}>(items:T[],showShorts:boolean,minimumMinutes=0):T[]{
 if(showShorts)return items;
 const minimum=Number.isFinite(minimumMinutes)&&minimumMinutes>0?minimumMinutes*60:0;
 return items.filter(item=>{if(item.kind==='shorts')return false;const seconds=mediaSeconds(item);return seconds===undefined||seconds>=minimum;});
}
export function readShowShorts(storage?:Pick<Storage,'getItem'>):boolean{try{return storage?.getItem(showShortsPreference)==='true';}catch{return false;}}
export default function ChannelVideos({id,name,csrf,readOnly=false,onOpen,title='Uploads'}:{id:string;name:string;csrf:string;readOnly?:boolean;onOpen?:(item:Item)=>void;title?:string}) {
 const [items,setItems]=useState<Item[]>([]),[selection,setSelection]=useState<Item|null>(null),[more,setMore]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const [saved,setSaved]=useState<Record<string,boolean>>({}),[queued,setQueued]=useState<Record<string,boolean>>({}),[downloadError,setDownloadError]=useState('');

 const [refresh,setRefresh]=useState<ChannelRefresh>(),[browse,setBrowse]=useState<ChannelBrowse<Item>>();
 const [minimumMinutes,setMinimumMinutes]=useState(0),minimumRef=useRef(0);
 const [showShorts,setShowShorts]=useState(()=>{try{return readShowShorts(localStorage);}catch{return false;}}),[filterPagingPaused,setFilterPagingPaused]=useState(false);
 const statusRevision=useRef(0),refreshPending=useRef(false),hasMore=useRef(true),loadedItems=useRef<Item[]>([]),cachedItems=useRef<Item[]>([]);
 const browseCursor=useRef('0'),browseState=useRef<ChannelBrowse<Item>|undefined>(undefined),consumedPages=useRef(new Set<string>()),requestedPages=useRef(new Set<string>()),prefetched=useRef(false),failedBrowseCursor=useRef<string|null>(null);
 const offset=useRef(0),loading=useRef(false),target=useRef<HTMLDivElement>(null),controller=useRef(new AbortController());
 const applyBrowse=useCallback((next?:ChannelBrowse<Item>)=>{
  if(!next||next.cursor!==browseCursor.current)return;
  browseState.current=next;setBrowse(next);
  if(next.state==='idle'&&!next.page&&next.hasMore&&(requestedPages.current.has(next.cursor)||failedBrowseCursor.current===next.cursor)){
   failedBrowseCursor.current=next.cursor;setError('This upload check did not finish. Choose Try again to continue.');
  }else failedBrowseCursor.current=null;
  if(next.page?.cursor===next.cursor&&!consumedPages.current.has(next.cursor)){
   const known=new Set(loadedItems.current.map(item=>item.id));
   const added=next.page.items.filter(item=>!known.has(item.id));
   if(visibleChannelUploads(added,false,minimumRef.current).length)setFilterPagingPaused(false);
   else if(!next.page.items.length||visibleChannelUploads(next.page.items,false,minimumRef.current).length<next.page.items.length)setFilterPagingPaused(true);
   consumedPages.current.add(next.cursor);const merged=mergeChannelUploads(loadedItems.current,next.page.items);loadedItems.current=merged;setItems(merged);
  }
 },[]);
 const load=useCallback(async(_quiet=false,firstPage=false)=>{
  if(document.hidden||(!firstPage&&!hasMore.current))return;
  if(loading.current){if(firstPage)refreshPending.current=true;return;}
  loading.current=true;setBusy(true);setError('');const signal=controller.current.signal,previousOffset=offset.current,previousMore=hasMore.current,start=firstPage?0:previousOffset;
  try{
   const data=await api<{items:Item[];more:boolean;total?:number;minimumVideoMinutes?:number;notice?:string;refresh?:ChannelRefresh;browse?:ChannelBrowse<Item>}>('/api/guide/channel/'+encodeURIComponent(id)+'?offset='+start+'&browse_cursor='+encodeURIComponent(browseCursor.current),signal,undefined,undefined,{feedback:'inline'});
   if(signal.aborted)return;
   const minimum=typeof data.minimumVideoMinutes==='number'&&Number.isInteger(data.minimumVideoMinutes)&&data.minimumVideoMinutes>=0&&data.minimumVideoMinutes<=120?data.minimumVideoMinutes:0;
   minimumRef.current=minimum;setMinimumMinutes(minimum);
   const page=channelUploadPage(cachedItems.current,data,previousOffset,previousMore,firstPage);
   cachedItems.current=page.items;offset.current=page.offset;hasMore.current=page.more;
   if(!firstPage){const known=new Set(loadedItems.current.map(item=>item.id)),added=data.items.filter(item=>!known.has(item.id));if(visibleChannelUploads(added,false,minimumRef.current).length)setFilterPagingPaused(false);else if(visibleChannelUploads(data.items,false,minimumRef.current).length<data.items.length)setFilterPagingPaused(true);}
   loadedItems.current=mergeChannelUploads(loadedItems.current,data.items,firstPage);setItems(loadedItems.current);applyBrowse(data.browse);
   setMore(page.more);setNotice(data.refresh?.notice||data.notice||'');setRefresh(data.refresh);
  }catch(error){if(!signal.aborted){failedBrowseCursor.current=null;setError(error instanceof Error?error.message:'Could not load videos.');}}
  finally{if(!signal.aborted){loading.current=false;setBusy(false);if(refreshPending.current){refreshPending.current=false;void load(true,true);}}}
 },[id,applyBrowse]);
 const requestBrowse=useCallback(async()=>{
  const state=browseState.current;
  if(readOnly||!csrf||document.hidden||loading.current||failedBrowseCursor.current!==null||!state||state.state==='paused'||state.state==='retry'||state.state==='queued'||state.state==='running')return;
  const cursor=consumedPages.current.has(state.cursor)?state.nextCursor:state.cursor;
  if(cursor===null||requestedPages.current.has(cursor))return;
  const requests=requestedPages.current;requests.add(cursor);browseCursor.current=cursor;loading.current=true;setBusy(true);setError('');const signal=controller.current.signal;let acknowledged=false;
  try{const data=await api<{browse:ChannelBrowse<Item>}>('/api/guide/channel/'+encodeURIComponent(id),signal,{action:'browse_uploads',cursor},csrf,{feedback:'inline'});if(signal.aborted)return;acknowledged=true;applyBrowse(data.browse);}
  catch(error){if(!signal.aborted){failedBrowseCursor.current=cursor;setError(error instanceof Error?error.message:'Older uploads could not be checked.');}}
  finally{if(!acknowledged)requests.delete(cursor);if(!signal.aborted){loading.current=false;setBusy(false);if(refreshPending.current){refreshPending.current=false;void load(true,true);}}}
 },[id,csrf,readOnly,applyBrowse,load]);
 const retry=()=>{const cursor=failedBrowseCursor.current;setError('');if(cursor!==null){requestedPages.current.delete(cursor);failedBrowseCursor.current=null;void requestBrowse();}else void load(true,true);};
 useEffect(()=>{
  controller.current=new AbortController();offset.current=0;loading.current=false;refreshPending.current=false;loadedItems.current=[];cachedItems.current=[];browseCursor.current='0';browseState.current=undefined;consumedPages.current=new Set();requestedPages.current=new Set();prefetched.current=false;failedBrowseCursor.current=null;statusRevision.current++;minimumRef.current=0;setMinimumMinutes(0);setItems([]);setNotice('');setRefresh(undefined);setBrowse(undefined);hasMore.current=true;setMore(true);setSaved({});setQueued({});setDownloadError('');setSelection(null);setFilterPagingPaused(false);void load();
  const prioritised=(event:Event)=>{const detail=(event as CustomEvent<{id:string;refresh?:ChannelRefresh}>).detail;if(detail?.id!==id)return;if(detail.refresh){setRefresh(detail.refresh);setNotice(detail.refresh.notice);}void load(true,true);};
  const visibility=()=>{controller.current.abort();loading.current=false;refreshPending.current=false;setBusy(false);if(!document.hidden){controller.current=new AbortController();void load(true,true);}};
  window.addEventListener(channelRefreshEvent,prioritised);document.addEventListener('visibilitychange',visibility);
  return()=>{controller.current.abort();window.removeEventListener(channelRefreshEvent,prioritised);document.removeEventListener('visibilitychange',visibility);};
 },[load]);
 const visibleItems=visibleChannelUploads(items,showShorts,minimumMinutes),filterPagingBlocked=!showShorts&&(filterPagingPaused||!visibleItems.length);
 useEffect(()=>{if((!more&&!browse?.hasMore)||busy||error||filterPagingBlocked||!target.current)return;const root=target.current.closest<HTMLElement>('[data-ui-dialog-scroll],[data-dialog-scroll]');const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting)&&!loading.current){if(hasMore.current)void load();else void requestBrowse();}},{root,rootMargin:'240px 0px'});observer.observe(target.current);return()=>observer.disconnect();},[more,busy,error,filterPagingBlocked,items.length,browse?.cursor,browse?.nextCursor,browse?.state,browse?.hasMore,load,requestBrowse]);
 useEffect(()=>{if(prefetched.current||busy||readOnly||!csrf||!browse||browse.cursor!=='0'||!visibleItems.some(item=>item.metadata_pending&&!item.duration))return;prefetched.current=true;void requestBrowse();},[items,showShorts,browse,busy,csrf,readOnly,requestBrowse]);
 useEffect(()=>{const delay=browse&&browse.refreshAfterMs>0?Math.max(1000,Math.min(5000,browse.refreshAfterMs)):channelRefreshDelay(refresh,!items.length,notice,more);if(!delay)return;const timer=window.setInterval(()=>{if(!document.hidden)void load(true,true);},delay);return()=>window.clearInterval(timer);},[refresh,browse,items.length,notice,more,load]);
 useEffect(()=>{setSelection(old=>old?items.find(item=>item.id===old.id)||old:null);},[items]);
 useEffect(()=>{
  if(!items.length)return;let active=new AbortController(),timer:ReturnType<typeof setTimeout>;
  async function poll(){
   if(document.hidden)return;const c=active,revision=statusRevision.current;
   try{
    const statuses:Record<string,boolean>={},jobs:Record<string,boolean>={};
    for(let i=0;i<items.length;i+=100){
     const result=await api<{downloaded:Record<string,boolean>;jobs?:Record<string,string>;details?:Item[]}>('/api/download-status?ids='+items.slice(i,i+100).map(item=>encodeURIComponent(item.id)).join(','),c.signal,undefined,undefined,{feedback:'inline'});
     if(c.signal.aborted)return;Object.assign(statuses,result.downloaded);
     if(result.details){let changed=false;const next=loadedItems.current.map(item=>{const fresh=item.metadata_pending?result.details?.find(detail=>detail.id===item.id&&!detail.metadata_pending):undefined;if(fresh){changed=true;return fresh;}return item;});if(changed){loadedItems.current=next;setItems(next);}}
     for(const [video,state]of Object.entries(result.jobs||{}))jobs[video]=['queued','downloading','cancelling'].includes(state);
    }
    if(!c.signal.aborted&&revision===statusRevision.current){setSaved(statuses);setQueued(jobs);}
   }catch(error){if(!c.signal.aborted)setDownloadError(error instanceof Error?error.message:'Could not check saved videos.');}
   finally{if(!c.signal.aborted&&!document.hidden)timer=setTimeout(poll,15000);}
  }
  const visibility=()=>{active.abort();clearTimeout(timer);if(!document.hidden){active=new AbortController();void poll();}};
  void poll();document.addEventListener('visibilitychange',visibility);
  return()=>{active.abort();clearTimeout(timer);document.removeEventListener('visibilitychange',visibility);};
 },[items,id]);

 const videoDownloads=selection&&<VideoDownloadControls key={selection.id} id={selection.id} title={selection.title} poster={selection.thumbnail} csrf={csrf} canDownload={!readOnly&&selection.state==='published'} readOnly={readOnly} showCollections={false}/>;
 const changeShowShorts=(checked:boolean)=>{setShowShorts(checked);try{localStorage.setItem(showShortsPreference,String(checked));}catch{/* Display preferences remain usable when browser storage is unavailable. */}};
 const canLoadMore=more||(!readOnly&&!!csrf&&!!browse?.hasMore),browseWaiting=!!browse&&['queued','running','retry','paused'].includes(browse.state);
 const loadMore=()=>{setFilterPagingPaused(false);if(hasMore.current)void load();else void requestBrowse();};
 return <Stack gap="md"><Group justify="space-between" align="center" wrap="wrap" gap="sm" mih={44}><Title order={3}>{title}</Title><Switch label="Show Shorts" checked={showShorts} onChange={event=>changeShowShorts(event.currentTarget.checked)} styles={{root:{minHeight:44,display:'flex',alignItems:'center'},body:{minHeight:44,alignItems:'center'},label:{minHeight:44,display:'flex',alignItems:'center',cursor:'pointer'}}}/></Group>{downloadError&&<NotificationMessage kind="error" message={downloadError}/>}
 <MediaGrid label="Channel uploads">{visibleItems.map(item=>{const open=()=>onOpen?onOpen(item):setSelection(item);return <MediaCard key={item.id} length={mediaLength(item)}
  art={<UnstyledButton w="100%" style={{display:'block'}} onClick={open} aria-label={'Open '+item.title}><MediaArtwork src={item.thumbnail} kind="video"/></UnstyledButton>}
  title={<UnstyledButton w="100%" mih={44} style={{textAlign:'left'}} onClick={open}><Text fw={600} lineClamp={2}>{item.title}</Text></UnstyledButton>}
  meta={mediaDate(item.date)||'Date unavailable'}
  status={saved[item.id]?'Downloaded with ReelDock':queued[item.id]?'Download queued':item.metadata_pending?'Checking details':item.state==='scheduled'?'Scheduled':item.state==='live'?'Live now':saved[item.id]===undefined?'Checking download status':'Not downloaded'}
  actions={<TileActions inline readOnly={readOnly} favouriteDisabled={item.state!=='published'} item={{id:'youtube:video:'+item.id,videoId:item.id,kind:'video',title:item.title,image:item.thumbnail,channel:name,channelId:id,published:item.date,description:item.description}}/>}/>;})}</MediaGrid>{!showShorts&&items.length>0&&!visibleItems.length&&<Text c="dimmed" size="sm">{minimumMinutes>0?'No loaded uploads match this filter.':'Only Shorts are in the loaded uploads.'} {canLoadMore?'Show Shorts or load more uploads.':'Turn on Show Shorts to view them.'}</Text>}{filterPagingBlocked&&canLoadMore&&!error&&<Group><Button variant="secondary" disabled={busy||(!more&&browseWaiting)} onClick={loadMore}>Load more uploads</Button></Group>}{error&&<NotificationMessage kind="error" message={error} actions={<> <Button variant="secondary" disabled={busy} onClick={retry}>Try again</Button> </>}/>}<div ref={target} role="status" style={{minHeight:1}}>{browse?.notice|| (busy&&!items.length&&!notice?'Loading videos…':!items.length?(notice||'No channel uploads synced yet.'):notice|| (canLoadMore?(filterPagingBlocked?'Choose Load more uploads to continue':'Scroll for more'):''))}</div>{!onOpen&&selection&&<VideoSheet csrf={csrf} open onOpenChange={open=>{if(!open)setSelection(null);}} readOnly={readOnly}
 video={{...selection,id:selection.id,title:selection.title,image:selection.thumbnail,channel:name,channelId:id}}
 actions={<TileActions item={{kind:'video',id:'video:'+selection.id,videoId:selection.id,title:selection.title,image:selection.thumbnail,channel:name,channelId:id}} readOnly={readOnly}/>}
 downloads={selection.metadata_pending&&selection.state==='unknown'?<><Text size="sm" c="dimmed" role="status">Checking video details…</Text>{videoDownloads}</>:videoDownloads}
 description={selection.description}/>}</Stack>;
}
