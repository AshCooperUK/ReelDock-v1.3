import { usePageSearch } from '../pageSearch';
import { Box, Divider, Group, Image, Paper, Stack, Text, Title } from '@mantine/core';
import { NotificationMessage } from '../notifications';
import {useEffect,useRef,useState} from 'react';
import {Youtube} from 'lucide-react';
import {api} from '../api';
import {Button} from '../components/ui';
import CollectionShelf,{type CollectionItem,type CollectionSort} from './CollectionShelf';
import {FilterBar,MediaGrid} from '../components/browse';
import {SkeletonCard} from '../components/states';
import {collectionSorts} from './Favourites';
import DiscoveryTitle from '../discovery/DiscoveryTitle';
import VideoDownloadControls from '../VideoDownloadControls';
import VideoSheet from '../VideoSheet';
import {openChannel} from '../channelNavigation';
import {withFavourites,favouriteWrite,type CollectionData as Data} from './collectionFavourites';

const events=['reeldock-watchlist-changed','reeldock-movie-options-changed','reeldock-show-options-changed','reeldock-library-changed','reeldock-channel-changed','reeldock-video-favourite-changed'];
export default function Watchlist({csrf}:{csrf:string}){
 const [data,setData]=useState<Data|null>(null),[error,setError]=useState(''),[detail,setDetail]=useState<CollectionItem|null>(null),[removing,setRemoving]=useState<string[]>([]);
 const [favouriting,setFavouriting]=useState<string[]>([]),[sort,setSort]=useState<CollectionSort>('az');
 const query=usePageSearch('Watchlist');
 const request=useRef<AbortController|null>(null),pending=useRef(new Set<string>()),favouritePending=useRef(new Set<string>());
 async function refresh(){request.current?.abort();const c=new AbortController();request.current=c;try{const [r,f]=await Promise.all([api<Data>('/api/watchlist',c.signal),api<Data>('/api/favourites',c.signal)]);if(!c.signal.aborted){setData(withFavourites(r,f));setError('');}}catch(e){if(!c.signal.aborted)setError(e instanceof Error?e.message:'Could not load Watchlist.');}}
 useEffect(()=>{void refresh();const changed=()=>void refresh();const timer=setInterval(()=>{if(!document.hidden)void refresh();},30000);events.forEach(e=>window.addEventListener(e,changed));window.addEventListener('focus',changed);return()=>{request.current?.abort();clearInterval(timer);events.forEach(e=>window.removeEventListener(e,changed));window.removeEventListener('focus',changed);};},[]);
 async function remove(item:CollectionItem){const key=item.watchlistKey!;if(pending.current.has(key))return;pending.current.add(key);setRemoving([...pending.current]);setError('');try{const r=await api('/api/watchlist/item',undefined,{key,watchlisted:false},csrf);window.dispatchEvent(new CustomEvent('reeldock-watchlist-changed',{detail:r}));}catch(e){setError(e instanceof Error?e.message:'Could not remove this item.');}finally{pending.current.delete(key);setRemoving([...pending.current]);}}
 async function favourite(item:CollectionItem){const key=item.watchlistKey!;if(favouritePending.current.has(key))return;favouritePending.current.add(key);setFavouriting([...favouritePending.current]);setError('');try{const write=favouriteWrite(item);await api(write.url,undefined,write.body,csrf);window.dispatchEvent(new CustomEvent(write.event,{detail:item.kind==='video'?{id:item.videoId,favourite:!item.favourite,csrf}:item.kind==='channel'?undefined:{key,field:'favourite'}}));}catch(e){setError(e instanceof Error?e.message:'Could not save this favourite.');}finally{favouritePending.current.delete(key);setFavouriting([...favouritePending.current]);}}
 function open(item:CollectionItem){if(item.kind==='channel')openChannel({id:item.channelId||item.id,name:item.title,avatar:item.avatar});else setDetail(item);}
 const shared={open,genreFilter:true,collection:'watchlist' as const,onRemove:(item:CollectionItem)=>void remove(item),removing,onFavourite:(item:CollectionItem)=>void favourite(item),favouriting,query,sort};
 return <Stack gap="xl">
  <FilterBar sorts={collectionSorts} sort={sort} onSort={value=>setSort(value as CollectionSort)} sortLabel="Sort Watchlist"/>
  {error&&<NotificationMessage kind="error" message={error} actions={<> <Button variant="secondary" onClick={()=>void refresh()}>Try again</Button> </>}/>}
  {!data&&!error&&<div role="status" aria-label="Loading Watchlist"><MediaGrid>{Array.from({length:8},(_,i)=><Box component="li" key={i}><SkeletonCard/></Box>)}</MediaGrid></div>}
  {data&&<><CollectionShelf id="watch-movies" title="Movies" kind="movie" items={data.movies} {...shared}/><Divider/><CollectionShelf id="watch-shows" title="TV Shows" kind="show" items={data.shows} {...shared}/>
  <Divider/><Group gap="sm"><Youtube size={24}/><Title order={2}>YouTube</Title></Group>
  {!!data.channels.length&&<CollectionShelf id="watch-channels" title="Channels" kind="channel" items={data.channels} {...shared}/>}
  {!!data.videos.length&&<CollectionShelf id="watch-videos" title="Videos" kind="video" items={data.videos} {...shared} csrf={csrf}/>}</>}
  {detail&&(detail.kind==='movie'||detail.kind==='show')&&<DiscoveryTitle item={detail} globalSearch csrf={csrf} onClose={()=>setDetail(null)}/>}
  {detail?.kind==='video'&&<VideoSheet csrf={csrf} open onOpenChange={v=>{if(!v)setDetail(null);}} video={{id:detail.videoId!,title:detail.title,image:detail.image,backdrop:detail.backdrop,channel:detail.channel,channelId:detail.channelId}} downloads={<VideoDownloadControls id={detail.videoId!} title={detail.title} poster={detail.image} csrf={csrf} canDownload/>} description={detail.description}/>}
 </Stack>;
}
