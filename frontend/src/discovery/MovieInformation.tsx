import { Avatar, Box, Group, Paper, ScrollArea, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, UserRound } from 'lucide-react';
import { Button } from '../components/ui';
import { api } from '../api';
import { mediaDate } from '../mediaDate';
import type { DiscoverItem } from '../Discover';

type MovieInfo={facts:Record<string,string>;cast:{id:number;name:string;role:string;image:string}[];source:string;unavailable?:boolean;title?:string;year?:string;description?:string;image?:string;backdrop?:string};
function useTitleInformation(item:DiscoverItem,kind:'movie'|'show') {
 const [info,setInfo]=useState<MovieInfo|null>(null),[busy,setBusy]=useState(false),[unavailable,setUnavailable]=useState(false);
 useEffect(()=>{
  setInfo(null);setUnavailable(false);setBusy(false);
  if(item.kind!==kind)return;
  const c=new AbortController();setBusy(true);
  api<MovieInfo>('/api/view-media/'+kind+'-info?'+new URLSearchParams({id:item.id,imdb:(kind==='movie'?item.externalId||item.imdbId:item.imdbId)||'',...(kind==='show'?{tvdb:item.externalId||''}:{})}),c.signal,undefined,undefined,{feedback:'inline'})
   .then(next=>{if(!c.signal.aborted){setInfo(next);setUnavailable(!!next.unavailable);}})
   .catch(()=>{if(!c.signal.aborted)setUnavailable(true);})
   .finally(()=>{if(!c.signal.aborted)setBusy(false);});
  return()=>c.abort();
 },[item.id,item.kind,item.externalId,item.imdbId,kind]);
 return {info,busy,unavailable};
}
export const useMovieInformation=(item:DiscoverItem)=>useTitleInformation(item,'movie');
export const useShowInformation=(item:DiscoverItem)=>useTitleInformation(item,'show');

export function MovieFacts({info}:{info:MovieInfo|null}) {
 return info?<SimpleGrid component="dl" cols={{base:1,sm:2}} spacing="sm" m={0}>{Object.entries(info.facts).map(([label,value])=><Paper key={label} withBorder p="sm" radius="md"><Text component="dt" size="xs" c="dimmed">{label}</Text><Text component="dd" m={0} mt={4} size="sm">{label==='Released'||label==='Premiered'?mediaDate(value)||value:value}</Text></Paper>)}</SimpleGrid>:null;
}

const castInitials=(name:string)=>name.trim().split(/\s+/).map(word=>word[0]).filter(Boolean).slice(0,2).join('').toUpperCase();
export function MovieCast({info,busy,unavailable,titleKind='movie'}:{info:MovieInfo|null;busy:boolean;unavailable:boolean;titleKind?:'movie'|'show'}) {
 const label=titleKind==='show'?'TV show':'movie';
 const list=useRef<HTMLDivElement>(null);
 const scrollCast=(dir:number)=>{const el=list.current;if(el)el.scrollBy({left:dir*el.clientWidth*0.8,behavior:'smooth'});};
 if(busy)return <Text size="sm" role="status">Loading cast and {label} information…</Text>;
 if(unavailable)return <Text size="sm" role="status">Extra {label} information is unavailable right now.</Text>;
 if(!info?.cast.length)return null;
 return <Stack component="section" gap="sm" aria-label={'More '+label+' information'}>
  <Group justify="space-between"><Title order={3}>Cast</Title>{info.cast.length>3&&<Group gap="xs"><Button iconOnly variant="secondary" aria-label="Scroll cast left" onClick={()=>scrollCast(-1)}><ChevronLeft size={18}/></Button><Button iconOnly variant="secondary" aria-label="Scroll cast right" onClick={()=>scrollCast(1)}><ChevronRight size={18}/></Button></Group>}</Group>
  <ScrollArea viewportRef={list} type="auto" scrollbars="x"><Group component="ul" wrap="nowrap" align="flex-start" gap="md" aria-label={titleKind==='show'?'TV show cast':'Movie cast'} m={0} p={0} pb="sm" style={{listStyle:'none'}}>{info.cast.map(person=><Stack component="li" key={person.id} gap={4} w={100} style={{flexShrink:0}}><Avatar src={person.image} size={100} radius="md">{person.name.trim()?castInitials(person.name):<UserRound size={26}/>}</Avatar><Text fw={600} size="sm" lineClamp={2} title={person.name}>{person.name}</Text>{person.role&&<Text size="xs" c="dimmed" lineClamp={2} title={person.role}>{person.role}</Text>}</Stack>)}</Group></ScrollArea>
 </Stack>;
}
