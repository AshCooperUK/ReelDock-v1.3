import surfaces from '../components/MediaSurfaces.module.css';
import { usePageSearch } from '../pageSearch';
import { Box, Divider, Group, Image, Paper, Stack, Text, Title } from '@mantine/core';
import { UnstyledButton as ControlTrigger } from '@mantine/core';
import { NotificationMessage } from '../notifications';
import {useEffect,useRef,useState} from 'react';
import {Bookmark,Heart,Plus,Search,Youtube} from 'lucide-react';
import {api} from '../api';
import {Button,Dialog,DialogContent,DialogTitle,DialogDescription} from '../components/ui';
import {TextInput} from '../TextField';
import ChannelIdentity from '../ChannelIdentity';
import {openChannel,channelEvent} from '../channelNavigation';
import DiscoveryTitle from '../discovery/DiscoveryTitle';
import VideoDownloadControls from '../VideoDownloadControls';
import VideoSheet from '../VideoSheet';
import {attachScrollPaging,trackPagingMomentum} from '../viewMedia/scrollPaging';
import CollectionShelf,{ordered,type CollectionItem,type CollectionSort} from './CollectionShelf';
import {FilterBar,MediaGrid} from '../components/browse';
import {SkeletonCard} from '../components/states';

type Data={movies:CollectionItem[];shows:CollectionItem[];episodes?:CollectionItem[];channels:CollectionItem[];videos:CollectionItem[]};
const events=['reeldock-watchlist-changed','reeldock-movie-options-changed','reeldock-show-options-changed','reeldock-channel-changed','reeldock-channel-removed','reeldock-video-favourite-changed','reeldock-episode-favourite-changed'];
export const collectionSorts=[{value:'az',label:'A–Z'},{value:'za',label:'Z–A'}];
export default function Favourites({csrf}:{csrf:string}){
 const [data,setData]=useState<Data|null>(null),[error,setError]=useState(''),[detail,setDetail]=useState<CollectionItem|null>(null),[picker,setPicker]=useState(false),[sort,setSort]=useState<CollectionSort>('az');
 const query=usePageSearch('Favourites');
 const request=useRef<AbortController|null>(null),pickerScroll=useRef<HTMLDivElement>(null);
 async function refresh(){request.current?.abort();const c=new AbortController();request.current=c;try{const next=await api<Data>('/api/favourites',c.signal);if(!c.signal.aborted){setData(next);setError('');}}catch(e){if(!c.signal.aborted)setError(e instanceof Error?e.message:'Could not load favourites.');}}
 useEffect(()=>{void refresh();const changed=()=>void refresh();events.forEach(e=>window.addEventListener(e,changed));window.addEventListener('focus',changed);return()=>{request.current?.abort();events.forEach(e=>window.removeEventListener(e,changed));window.removeEventListener('focus',changed);};},[]);
 // A channel identity inside the picker opens the global channel sheet; close
 // the picker so the two overlays never stack.
 useEffect(()=>{const close=()=>setPicker(false);window.addEventListener(channelEvent,close);return()=>window.removeEventListener(channelEvent,close);},[]);
 function open(item:CollectionItem){if(item.kind==='channel')openChannel({id:item.channelId||item.id,name:item.title,avatar:item.avatar});else setDetail(item);}
 return <Stack gap="xl">
  <FilterBar sorts={collectionSorts} sort={sort} onSort={value=>setSort(value as CollectionSort)} sortLabel="Sort favourites"/>
  {error&&<NotificationMessage kind="error" message={error} actions={<> <Button variant="secondary" onClick={()=>void refresh()}>Try again</Button> </>}/>}
  {!data&&!error&&<div role="status" aria-label="Loading favourites"><MediaGrid>{Array.from({length:8},(_,i)=><Box component="li" key={i}><SkeletonCard/></Box>)}</MediaGrid></div>}
  {data&&<><CollectionShelf id="movies" title="Movies" items={data.movies} kind="movie" open={open} genreFilter query={query} sort={sort}/><Divider/><CollectionShelf id="shows" title="TV Shows" items={data.shows} kind="show" open={open} genreFilter query={query} sort={sort}/><Divider/><CollectionShelf id="episodes" title="TV Episodes" items={data.episodes||[]} kind="show" open={open} genreFilter query={query} sort={sort} emptyNoun="TV episodes"/>
  <Divider/><Group gap="sm"><Youtube size={24}/><Title order={2}>YouTube</Title></Group>
  <CollectionShelf id="channels" title="Favourite channels" items={data.channels} kind="channel" open={open} genreFilter query={query} sort={sort}/>
  <CollectionShelf id="videos" title="Favourite videos" items={data.videos} kind="video" open={open} genreFilter csrf={csrf} query={query} sort={sort} extra={<Button variant="secondary" onClick={()=>setPicker(true)}><Plus size={16}/>Add videos</Button>}/></>}
  {detail&&(detail.kind==='movie'||detail.kind==='show')&&<DiscoveryTitle globalSearch item={detail.show||detail} initialEpisode={detail.episode} csrf={csrf} onClose={()=>setDetail(null)}/>}
  {detail?.kind==='video'&&<VideoSheet csrf={csrf} open onOpenChange={v=>{if(!v)setDetail(null);}} video={{id:detail.videoId!,title:detail.title,image:detail.image,backdrop:detail.backdrop,channel:detail.channel,channelId:detail.channelId}} downloads={<VideoDownloadControls id={detail.videoId!} title={detail.title} poster={detail.image} csrf={csrf} canDownload/>} description={detail.description}/>}
  <Dialog open={picker} onOpenChange={setPicker}><DialogContent scrollRef={pickerScroll}><Box component="header" p="md" pr={60}><DialogTitle>Add favourite videos</DialogTitle><DialogDescription>Choose individual videos from ReelDock</DialogDescription></Box>{picker&&<VideoPicker csrf={csrf} scrollRoot={pickerScroll} done={()=>setPicker(false)}/>}</DialogContent></Dialog>
 </Stack>;
}

export function VideoPicker({csrf,done,collection='favourites',scrollRoot}:{csrf:string;done:()=>void;collection?:'favourites'|'watchlist';scrollRoot?:{current:HTMLDivElement|null}}){
 const watchlist=collection==='watchlist',Icon=watchlist?Bookmark:Heart;
 const selected=(item:CollectionItem)=>!!(watchlist?item.watchlisted:item.favourite);
 const [query,setQuery]=useState(''),[search,setSearch]=useState(''),[items,setItems]=useState<CollectionItem[]>([]),[next,setNext]=useState<number|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[sending,setSending]=useState<string[]>([]);
 const controller=useRef<AbortController|null>(null),loading=useRef(false),body=useRef<HTMLDivElement>(null),edge=useRef<HTMLDivElement>(null);
 useEffect(()=>{const timer=setTimeout(()=>setSearch(query.trim()),250);return()=>clearTimeout(timer);},[query]);
 async function load(offset=0){if(offset&&loading.current)return;controller.current?.abort();const c=new AbortController();controller.current=c;loading.current=true;setBusy(true);setError('');try{const r=await api<{items:CollectionItem[];nextOffset:number|null}>((watchlist?'/api/watchlist/videos?':'/api/favourites/videos?')+new URLSearchParams({q:search,offset:String(offset)}),c.signal);if(!c.signal.aborted){setItems(old=>offset?[...old,...r.items.filter(i=>!old.some(o=>o.videoId===i.videoId))]:r.items);setNext(r.nextOffset);}}catch(e){if(!c.signal.aborted)setError(e instanceof Error?e.message:'Could not load videos.');}finally{if(!c.signal.aborted){loading.current=false;setBusy(false);}}}
 useEffect(()=>{setItems([]);setNext(null);void load();return()=>controller.current?.abort();},[search]);
 useEffect(()=>{const target=scrollRoot?.current||body.current;if(!target)return;if(busy)return trackPagingMomentum(target);if(next===null||error)return;return attachScrollPaging(target,()=>edge.current?edge.current.getBoundingClientRect().top-target.getBoundingClientRect().top:Infinity,()=>void load(next));},[busy,next,search,error]);
 async function toggle(item:CollectionItem){const id=item.videoId!;if(sending.includes(id))return;setSending(old=>[...old,id]);setError('');try{if(watchlist){const r=await api<{watchlisted:boolean;key:string}>('/api/watchlist/item',undefined,{item,watchlisted:!selected(item)},csrf);setItems(old=>old.map(v=>v.videoId===id?{...v,watchlisted:r.watchlisted}:v));window.dispatchEvent(new CustomEvent('reeldock-watchlist-changed',{detail:r}));}else{const r=await api<{id:string;favourite:boolean}>('/api/favourites/video/'+encodeURIComponent(id),undefined,{favourite:!item.favourite},csrf);setItems(old=>old.map(v=>v.videoId===id?{...v,favourite:r.favourite}:v));window.dispatchEvent(new CustomEvent('reeldock-video-favourite-changed',{detail:r}));}}catch(e){setError(e instanceof Error?e.message:'Could not save selection.');}finally{setSending(old=>old.filter(i=>i!==id));}}
 return <><Stack p="md" gap="md" ref={body} style={{minHeight:0}}><Box><TextInput aria-label="Search saved videos" value={query} onChange={e=>setQuery(e.target.value)} placeholder="Search title or channel…" onClear={()=>setQuery('')} leftSection={<Search size={18}/>}/></Box>
 {error&&<NotificationMessage kind="error" message={error} actions={<> <Button variant="secondary" onClick={()=>void load()}>Try again</Button> </>}/>}
 <Stack gap="sm">{ordered(items).map(v=><Paper key={v.videoId} data-media-surface="tile" className={[surfaces.surface,surfaces.tile].filter(Boolean).join(' ')} withBorder p="sm" radius="md"><Stack gap="xs"><ControlTrigger disabled={sending.includes(v.videoId!)} w="100%" aria-label={(watchlist?(selected(v)?'Remove from Watchlist: ':'Add to Watchlist: '):(selected(v)?'Remove favourite: ':'Favourite: '))+v.title} aria-pressed={selected(v)} onClick={()=>void toggle(v)}><Group wrap="nowrap">{v.image&&<Image src={v.image} alt="" loading="lazy" w={72} h={48} radius="sm"/>}<Stack gap={4} style={{flex:1,minWidth:0}}><Text fw={600} size="sm" lineClamp={2}>{v.title}</Text><Text size="xs" c="dimmed">{v.published?.slice(0,10)}</Text></Stack><Icon size={20} fill={selected(v)?'currentColor':'none'}/></Group></ControlTrigger><ChannelIdentity id={v.channelId} name={v.channel||'YouTube channel'} avatar={v.avatar}/></Stack></Paper>)}</Stack>
 <div ref={edge} role="status">{busy?'Loading videos…':!error&&!items.length?'No saved videos match this search.':''}</div></Stack><Group component="footer" p="md" justify="space-between"><Text size="sm" c="dimmed">Saved in ReelDock</Text><Button onClick={done}>Done</Button></Group></>;
}
