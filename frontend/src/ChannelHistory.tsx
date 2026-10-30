import { Group, Paper, Stack, Text } from '@mantine/core';
import { useCallback, useEffect, useRef, useState } from 'react';
import { NotificationMessage } from './notifications';
import { api } from './api';
import { Button } from './components/ui';
import { channelActionLabel } from './ChannelOptions';

export type HistoryEvent={id:string;kind:string;title:string;state:string;message:string;created:number};
const kindLabel:Record<string,string>={download:'Download','management':'Management','channel-task':'Channel action'};
const when=(seconds:number)=>Number.isFinite(seconds)?new Date(seconds*1000).toLocaleString('en-GB'):'';

/** Recorded downloads, management events and the latest channel action — never claimed to be exhaustive. */
export default function ChannelHistory({id}:{id:string}) {
 const [items,setItems]=useState<HistoryEvent[]>([]),[more,setMore]=useState(false),[busy,setBusy]=useState(true),[error,setError]=useState(''),[loaded,setLoaded]=useState(false),[scope,setScope]=useState(id);
 const offset=useRef(0),loading=useRef(false),controller=useRef(new AbortController());
 const load=useCallback(async()=>{
  if(loading.current)return;loading.current=true;setBusy(true);setError('');
  const signal=controller.current.signal;
  try{
   const page=await api<{items:HistoryEvent[];total:number;more:boolean}>('/api/guide/channel/'+encodeURIComponent(id)+'/history?limit=20&offset='+offset.current,signal);
   if(signal.aborted)return;
   offset.current+=page.items.length;
   setItems(old=>[...old,...page.items.filter(item=>!old.some(known=>known.id===item.id))]);setMore(page.more);setLoaded(true);
  }catch(e){if(!signal.aborted)setError(e instanceof Error?e.message:'Could not load history.');}
  finally{if(!signal.aborted){loading.current=false;setBusy(false);}}
 },[id]);
 useEffect(()=>{controller.current=new AbortController();offset.current=0;loading.current=false;setScope(id);setItems([]);setMore(false);setLoaded(false);setError('');void load();return()=>controller.current.abort();},[load]);
 const current=scope===id,shown=current?items:[],hasMore=current&&more,isLoaded=current&&loaded;
 return <Stack component="section" gap="md" aria-label="Channel history">
  <Text size="sm" c="dimmed">Recorded downloads, management events and latest channel action.</Text>
  {error&&<NotificationMessage kind="error" message={error} actions={<> <Button variant="secondary" onClick={()=>void load()}>Try again</Button> </>}/>}
  {(busy||!current)&&!shown.length&&!error&&<Text size="sm" role="status">Loading history…</Text>}
  {isLoaded&&!shown.length&&!error&&<Text size="sm">No recorded history for this channel yet.</Text>}
  {!!shown.length&&<Stack component="ul" gap="sm" m={0} p={0} style={{listStyle:"none"}}>{shown.map(item=><Paper component="li" key={item.id} withBorder p="md" radius="md"><Stack gap={4}><Text fw={600}>{item.kind==='channel-task'?channelActionLabel(item.title):item.title||kindLabel[item.kind]||'Event'}</Text><Text size="xs" c="dimmed">{[kindLabel[item.kind]||item.kind,item.state,when(item.created)].filter(Boolean).join(' · ')}</Text>{item.message&&<Text size="sm">{item.message}</Text>}</Stack></Paper>)}</Stack>}
  {hasMore&&<Group><Button variant="secondary" disabled={busy} onClick={()=>void load()}>{busy?'Loading…':'Load more'}</Button></Group>}
 </Stack>;
}
