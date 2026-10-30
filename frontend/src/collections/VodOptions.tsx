import { Badge, Box, Fieldset, Group as MantineGroup, Image, Paper, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { NotificationMessage } from '../notifications';
import {useEffect,useState} from 'react';
import {api} from '../api';
import {acquisitionItem} from '../discoverPayload';
import type {DiscoverItem} from '../Discover';
import {Button,DialogTitle,DialogDescription} from '../components/ui';
import MovieOptions from '../discovery/MovieOptions';
import ShowOptions from '../discovery/ShowOptions';

/** VOD enters the existing title options using a server-resolved catalogue ID. */
export default function VodOptions({source,path,embedded=false,showFavourite=true}:{source:string;path:string;embedded?:boolean;showFavourite?:boolean}){
 const [value,setValue]=useState<{item:DiscoverItem;csrf:string}|null>(null),[ready,setReady]=useState(false),[error,setError]=useState(''),[revision,setRevision]=useState(0);
 useEffect(()=>{const c=new AbortController();let timer:ReturnType<typeof setTimeout>;setError('');setReady(false);
  async function prepare(item:DiscoverItem,csrf:string){try{const result=await api<{loading:boolean;error?:string}>('/api/discovery-engine/title',c.signal,{item:acquisitionItem(item)},csrf);if(c.signal.aborted)return;if(result.loading)timer=setTimeout(()=>void prepare(item,csrf),1500);else setReady(true);}catch{if(!c.signal.aborted)setReady(true);}}
  api<{item:DiscoverItem;csrf:string}>('/api/favourites/vod-title?'+new URLSearchParams({source,path}),c.signal).then(r=>{if(c.signal.aborted)return;setValue(r);if(r.item.kind==='show')void prepare(r.item,r.csrf);}).catch(e=>{if(!c.signal.aborted)setError(e instanceof Error?e.message:'Could not open title options.');});
  return()=>{c.abort();clearTimeout(timer);};
 },[source,path,revision]);
 return value?value.item.kind==='movie'?<MovieOptions embedded={embedded} showFavourite={showFavourite} item={value.item} csrf={value.csrf}/>:<ShowOptions embedded={embedded} showFavourite={showFavourite} item={value.item} csrf={value.csrf} catalogueReady={ready} onPolicyChange={()=>{}}/>:<><Stack gap="xs" component="header"><DialogTitle>Title options</DialogTitle><DialogDescription>VOD</DialogDescription></Stack><Stack gap="md">{error?<NotificationMessage kind="error" message={error} actions={<> <Button variant="secondary" onClick={()=>setRevision(n=>n+1)}>Try again</Button> </>}/>:<Text size="sm" role="status">Opening title options…</Text>}</Stack></>;
}
