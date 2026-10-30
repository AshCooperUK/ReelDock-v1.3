import { NotificationMessage } from '../notifications';
import {useEffect,useRef,useState} from 'react';
import {Bookmark,BookmarkCheck,Heart} from 'lucide-react';
import {api} from '../api';
import {Button} from '../components/ui';
import {acquisitionItem} from '../discoverPayload';
import {favouriteWrite} from './collectionFavourites';
import {useTileCollections,refreshTileCollections} from './tileCollectionStore';
import {collectionIdentity,watchlistHint} from './tileIdentity';
import type {CollectionItem} from './CollectionShelf';
import type {MediaCard} from '../viewMedia/types';
import { Group, Stack } from '@mantine/core';

type Resolve={source:string;path:string}|{local:string};
export function mediaTileTarget(card:MediaCard):{item?:CollectionItem;resolve?:Resolve;label?:string;episode?:boolean}{
 if(card.kind==='channel'){
  const existing=card.action.type==='discover'&&card.action.item.kind==='channel'?card.action.item:undefined;
  const candidate=card.channelId||existing?.channelId||existing?.id||(card.action.type==='youtube'?card.action.id:card.id);
  const channelId=candidate.replace(/^(?:youtube:)?channel:/,'');
  if(!/^UC[A-Za-z0-9_-]{22}$/.test(channelId))return {label:card.title};
  return {item:{...existing,id:existing?.id||'youtube:channel:'+channelId,kind:'channel',channelId,title:card.title,image:card.image||existing?.image,backdrop:card.backdrop||existing?.backdrop,description:card.description||existing?.description}};
 }
 if(card.action.type==='discover')return {item:card.action.item};
 if(card.action.type==='vod')return {resolve:{source:card.action.source,path:card.action.item.path},label:card.title};
 if(card.action.type==='local')return {resolve:{local:card.action.id},label:card.title,episode:['show','shows'].includes(card.kind)&&typeof card.season==='number'&&typeof card.number==='number'};
 if(card.action.type==='local-group')return {};
 return {item:{id:'youtube:video:'+card.action.id,videoId:card.action.id,kind:'video',title:card.title,channelId:card.channelId,channel:card.channel,image:card.image,published:card.date,publishedApproximate:!!card.dateApproximate,description:card.description}};
}
export default function TileActions({item:initial,resolve,inline=false,onlyWatchlist=false,label='this title',episode:episodeHint=false,readOnly=false,favouriteDisabled=false}:{item?:CollectionItem;resolve?:Resolve;inline?:boolean;onlyWatchlist?:boolean;label?:string;episode?:boolean;readOnly?:boolean;favouriteDisabled?:boolean}){
 const rows=useTileCollections(),[resolved,setResolved]=useState<CollectionItem|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[visible,setVisible]=useState(false),[revision,setRevision]=useState(0);
 const host=useRef<HTMLDivElement>(null),sending=useRef(false);
 const query=resolve?JSON.stringify(resolve):'';
 useEffect(()=>{if(!resolve||!host.current)return;const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting)){setVisible(true);observer.disconnect();}},{rootMargin:'100px'});observer.observe(host.current);return()=>observer.disconnect();},[query]);
 useEffect(()=>{setResolved(null);setError('');if(!query||!visible)return;const c=new AbortController(),target=JSON.parse(query) as Resolve;
  api<{item:CollectionItem}>('local' in target?'/api/favourites/local-title/'+encodeURIComponent(target.local):'/api/favourites/vod-title?'+new URLSearchParams(target),c.signal,undefined,undefined,{expectedReadStatuses:[400]}).then(r=>{if(!c.signal.aborted)setResolved(r.item);}).catch(e=>{if(!c.signal.aborted)setError(e instanceof Error?e.message:'Title identity unavailable.');});return()=>c.abort();
 },[query,visible,revision]);
 const item=initial||resolved,base=item?.show||item;
 const episode=episodeHint||!!(item&&(item.episodeKey||item.show));
 const identity=base?collectionIdentity(base):'',faves=rows.favourites,watch=rows.watchlist;
 const group=base?.kind==='movie'?'movies':base?.kind==='show'?'shows':base?.kind==='channel'?'channels':'videos';
 const favourite=!!(item?.episodeKey?faves?.episodes?.some(v=>v.episodeKey===item.episodeKey):faves?.[group].some(v=>collectionIdentity(v)===identity));
 const saved=watch?.[group].find(v=>collectionIdentity(v)===identity),selected=!!saved;
 const title=item?.title||label,ready=!!item&&!!faves&&!!watch;
 async function toggle(which:'favourite'|'watchlist'){
  if(!item||!base||!ready||sending.current||readOnly||(which==='favourite'&&favouriteDisabled))return;sending.current=true;setBusy(true);setError('');
  try{
   const csrf=watch!.csrf!;
   if(which==='favourite'){
    if(base.kind==='video'&&!favourite)await api('/api/discover/youtube/register',undefined,{item:{...acquisitionItem(base),publishedApproximate:item.publishedApproximate}},csrf);
    const write=favouriteWrite({...item,favourite});const result=await api(write.url,undefined,write.body,csrf);
    window.dispatchEvent(new CustomEvent(write.event,{detail:base.kind==='channel'?undefined:base.kind==='video'||item.episodeKey?result:{key:identity,field:'favourite'}}));
   }else{
    if(base.kind==='video'&&!selected)await api('/api/discover/youtube/register',undefined,{item:{...acquisitionItem(base),publishedApproximate:item.publishedApproximate}},csrf);
    const result=await api('/api/watchlist/item',undefined,selected?{key:saved!.watchlistKey,watchlisted:false}:{item:{...acquisitionItem(base),imdbId:base.imdbId},watchlisted:true},csrf);
    window.dispatchEvent(new CustomEvent('reeldock-watchlist-changed',{detail:result}));
   }
   await refreshTileCollections();
  }catch(e){setError(e instanceof Error?e.message:'Could not save this choice.');}
  finally{sending.current=false;setBusy(false);}
 }
 const failure=error||rows.error,unavailable=!item&&!!error;
 if(episode&&onlyWatchlist)return null;
 return <Stack ref={host} gap="xs" data-inline={inline||undefined}>
  <Group gap="xs">{!onlyWatchlist&&<Button iconOnly variant="secondary" aria-label={(favourite?'Remove from favourites':'Add to favourites')+' · '+title} title={readOnly?'Unavailable in this layout preview':failure|| (favourite?'Remove from favourites':'Add to favourites')} aria-pressed={favourite} disabled={!ready||busy||readOnly||favouriteDisabled} onClick={()=>void toggle('favourite')}><Heart size={18} fill={favourite?'currentColor':'none'}/></Button>}{!episode&&<Button iconOnly variant="secondary" aria-label={(selected?'Remove from Watchlist':'Add to Watchlist')+' · '+(base?.title||title)} title={readOnly?'Unavailable in this layout preview':failure||(base?watchlistHint(base,selected):'Identifying this title…')} aria-pressed={selected} disabled={!ready||busy||readOnly} onClick={()=>void toggle('watchlist')}>{selected?<BookmarkCheck size={18}/>:<Bookmark size={18}/>}</Button>}</Group>
  {failure&&!unavailable&&<NotificationMessage kind="error" message={failure} actions={<> <Button variant="secondary" onClick={()=>{setError('');if(unavailable)setRevision(v=>v+1);else void refreshTileCollections();}}>Try again</Button> </>}/>}
 </Stack>;
}
