import surfaces from './components/MediaSurfaces.module.css';
import { HoverCard, Image, Menu, Stack, Text, UnstyledButton } from '@mantine/core';
import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Star } from 'lucide-react';
import { Group } from '@mantine/core';
import { api } from './api';
import type { DiscoverItem } from './Discover';
import TileActions from './collections/TileActions';

export type ActivityMediaCard={matched:boolean;title:string;year:string;genres:string[];rating:number|null;overview:string;cast:string[];poster:string;media_type:string;message:string;mediaItem?:DiscoverItem};
export function activityMediaItem(value:unknown):DiscoverItem|undefined{
 const item=value as Partial<DiscoverItem>|null;
 return item&&['movie','show'].includes(item.kind||'')&&typeof item.id==='string'&&!!item.id&&typeof item.title==='string'&&!!item.title?item as DiscoverItem:undefined;
}
function knownCard(item?:DiscoverItem):ActivityMediaCard|null{return item?{matched:true,title:item.title,year:item.year||'',genres:(item.genre||'').split(',').map(value=>value.trim()).filter(Boolean),rating:item.rating&&Number.isFinite(Number(item.rating))?Number(item.rating):null,overview:item.description||'',cast:[],poster:item.image||'',media_type:item.kind==='show'?'tv':'movie',message:'',mediaItem:item}:null;}
/** Only an explicit hover, focus, title click or opened action menu reads a lookup. */
function useActivityMedia(item:DiscoverItem|undefined,lookup?:string){
 const key=JSON.stringify([item?.kind,item?.id,lookup]),owner=useRef(key);owner.current=key;
 const [snapshot,setSnapshot]=useState<{key:string;card:ActivityMediaCard|null;error:string;loaded?:boolean}>({key,card:knownCard(item),error:''});
 const request=useRef<{key:string;controller:AbortController;promise:Promise<ActivityMediaCard|null>}|null>(null);
 useEffect(()=>()=>{if(request.current?.key===key){request.current.controller.abort();request.current=null;}},[key]);
 const card=snapshot.key===key?snapshot.card:knownCard(item),error=snapshot.key===key?snapshot.error:'';
 function load():Promise<ActivityMediaCard|null>{
  if(!lookup)return Promise.resolve(knownCard(item));
  if(snapshot.key===key&&snapshot.loaded)return Promise.resolve(snapshot.card);
  if(request.current?.key===key)return request.current.promise;
  request.current?.controller.abort();
  const controller=new AbortController(),current=()=>!controller.signal.aborted&&owner.current===key;
  const pending={key,controller,promise:Promise.resolve<ActivityMediaCard|null>(null)};
  pending.promise=api<ActivityMediaCard>(lookup,controller.signal,undefined,undefined,{feedback:'inline'}).then(result=>{
   if(!current())return null;
   setSnapshot(old=>current()?{key,card:result,error:'',loaded:true}:old);return result;
  }).catch(problem=>{if(current())setSnapshot(old=>current()?{key,card:old.key===key?old.card:knownCard(item),error:problem instanceof Error?problem.message:'Could not load programme information.'}:old);return null;}).finally(()=>{if(request.current===pending)request.current=null;});
  request.current=pending;return pending.promise;
 }
 return{card,error,load,current:()=>owner.current===key};
}
export function ActivityMediaMenuItem({item,lookup,onOpen}:{item?:DiscoverItem;lookup?:string;onOpen:(item:DiscoverItem)=>void}){
 const media=useActivityMedia(activityMediaItem(item),lookup),target=activityMediaItem(media.card?.mediaItem)||activityMediaItem(item);
 useEffect(()=>{if(!target)void media.load();},[lookup,item?.id]);
 return <Menu.Item mih={44} leftSection={<ArrowRight size={20} strokeWidth={1.8}/>} disabled={!target} title={!target?(media.error||media.card?.message||'Checking the exact programme identity…'):undefined} onClick={()=>{if(target)onOpen(target);}}>Go to {target?.kind==='show'?'show':target?.kind==='movie'?'movie':'programme'}</Menu.Item>;
}
export default function ActivityMediaTitle({name,item,lookup,onOpen,onClick}:{name:string;item?:DiscoverItem;lookup?:string;onOpen?:(item:DiscoverItem)=>void;onClick?:()=>void}){
 const media=useActivityMedia(activityMediaItem(item),lookup),[suppressed,setSuppressed]=useState(false),[notice,setNotice]=useState(''),intent=useRef(0);
 const target=activityMediaItem(media.card?.mediaItem)||activityMediaItem(item);
 useEffect(()=>{setNotice('');},[item?.kind,item?.id,lookup]);
 useEffect(()=>{const close=()=>{intent.current++;setSuppressed(true);};window.addEventListener('reeldock-overlay-open',close);return()=>{intent.current++;window.removeEventListener('reeldock-overlay-open',close);};},[]);
 async function open(){
  const click=++intent.current;
  if(target&&onOpen){setNotice('');onOpen(target);return;}
  if(onClick){onClick();return;}
  const result=await media.load(),resolved=activityMediaItem(result?.mediaItem);
  if(!media.current()||intent.current!==click)return;
  if(resolved&&onOpen)onOpen(resolved);else setNotice('Programme navigation is unavailable until this source has an exact show or movie match.');
 }
 const card=media.card,details=card?.matched?card:knownCard(target)||card;
 return <HoverCard interactive disabled={suppressed} width={360} openDelay={300} closeDelay={200} withinPortal position="bottom-start" role="dialog" onOpen={()=>void media.load()}>
  <HoverCard.Target><UnstyledButton type="button" mih={44} onMouseEnter={()=>setSuppressed(false)} onFocus={()=>setSuppressed(false)} onClick={()=>void open()} aria-label={'Open '+name} style={{textAlign:'left',fontWeight:600,overflowWrap:'anywhere',maxWidth:'100%'}}>{name}</UnstyledButton></HoverCard.Target>
  <HoverCard.Dropdown data-media-surface="preview" className={[surfaces.surface,surfaces.preview].filter(Boolean).join(' ')} maw="calc(100vw - 32px)"><Stack gap="sm">
   {details?.poster&&<Image src={details.poster} alt="" h={160} fit="cover" radius="md"/>}
   <Text fw={600}>{details?.title||name}{details?.year?' ('+details.year+')':''}</Text>
   {!card&&!media.error&&<Text size="sm">Looking up this title…</Text>}
   {details&&(card?.matched||target)?<>{!!details.genres?.length&&<Text size="sm" c="dimmed">{details.genres.join(', ')}</Text>}{details.rating!=null&&<Group gap={4} aria-label={`Rating ${details.rating.toFixed(1)} out of 10`}><Star size={16}/><Text size="sm">{details.rating.toFixed(1)}</Text></Group>}{details.overview&&<Text size="sm" lineClamp={5}>{details.overview}</Text>}{!!details.cast?.length&&<Text size="sm">Cast: {details.cast.join(', ')}</Text>}{card&&!card.matched&&<Text size="sm" c="dimmed" role="status">{card.message||'Showing saved programme details.'}</Text>}</>:card&&<Text size="sm">{card.message||'No unique metadata match for this title.'}</Text>}
   {target&&onOpen&&<TileActions item={target} inline/>}
   {(media.error||notice)&&<Text size="sm" c="dimmed" role="status">{media.error||notice}</Text>}
  </Stack></HoverCard.Dropdown>
 </HoverCard>;
}
