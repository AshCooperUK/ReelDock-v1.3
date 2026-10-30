import { Paper, Stack, Text, Title } from '@mantine/core';
import { useCallback, useEffect, useState } from 'react';
import { NotificationMessage } from './notifications';
import { api } from './api';
import { Button } from './components/ui';
import { mediaDate } from './mediaDate';

export type Forecast={id:string;channel:string;date:string;time:string;title:string;state:string;description:string;confidence:string};

/** Estimated (never confirmed) uploads: plain read-only entries with no video actions. */
export default function ChannelForecasts({id}:{id:string}) {
 const [data,setData]=useState<{id:string;items:Forecast[]}|null>(null),[error,setError]=useState(''),[revision,setRevision]=useState(0);
 const items=data?.id===id?data.items:null;
 const retry=useCallback(()=>setRevision(value=>value+1),[]);
 useEffect(()=>{
  const c=new AbortController();setData(null);setError('');
  api<{items:Forecast[]}>('/api/guide/channel/'+encodeURIComponent(id)+'/forecasts',c.signal).then(result=>{if(!c.signal.aborted)setData({id,items:result.items});}).catch(e=>{if(!c.signal.aborted)setError(e instanceof Error?e.message:'Could not load estimated uploads.');});
  return()=>c.abort();
 },[id,revision]);
 return <Stack component="section" gap="md" aria-label="Estimated uploads"><Title order={3}>Estimated uploads</Title>
  {error&&<NotificationMessage kind="error" message={error} actions={<> <Button variant="secondary" onClick={retry}>Try again</Button> </>}/>}
  {!items&&!error&&<Text size="sm" role="status">Checking estimated uploads…</Text>}
  {items&&!items.length&&<Text size="sm" c="dimmed">No estimated uploads in the next month.</Text>}
  {!!items?.length&&<Stack component="ul" gap="sm" m={0} p={0} style={{listStyle:"none"}}>{items.map(item=><Paper component="li" key={item.id} withBorder p="md" radius="md"><Stack gap={4}><Text fw={600}>{mediaDate(item.date)||item.date} · {item.time}</Text><Text size="xs" c="dimmed">Expected · {item.confidence||'Estimate'} confidence</Text>{item.description&&<Text size="sm">{item.description}</Text>}</Stack></Paper>)}</Stack>}
 </Stack>;
}
