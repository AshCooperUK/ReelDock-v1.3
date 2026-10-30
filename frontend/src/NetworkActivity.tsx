import { Badge, Box, Group, Paper, SegmentedControl, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { useEffect, useId, useState } from 'react';
import { ArrowDown, ArrowUp, Network } from 'lucide-react';
import { Help } from './SettingsUI';
import { api } from './api';

type Point = { time: number; download: number; upload: number };
type Traffic = { state: 'live' | 'warming' | 'waiting' | 'unavailable'; points: Point[]; interval: number };
const demoIn = [12,17,14,22,18,25,20,23,40,31,25,30,21,36,32,25,50,76,42,35,30,32,26,41,37,49,43,40,47,52];
const demoOut = [4,5,3,6,5,8,4,6,5,7,5,9,5,7,9,6,7,13,8,6,5,8,6,10,8,9,6,9,7,8];
function rate(value:number) { const unit=value>=1e6?'MB/s':value>=1e3?'KB/s':'B/s';const amount=value/(unit==='MB/s'?1e6:unit==='KB/s'?1e3:1);return `${amount.toFixed(amount>=100||unit==='B/s'?0:1)} ${unit}`; }

export default function NetworkActivity({live=false}:{live?:boolean}) {
 const [minutes,setMinutes]=useState(1),[data,setData]=useState<Traffic|null>(null),[error,setError]=useState(false);
 const id=useId().replace(/:/g,'');
 useEffect(()=>{
  if(!live)return;
  let timer:ReturnType<typeof setTimeout>|undefined;let active:AbortController|null=null;let stopped=false;
  async function poll(){
   if(stopped||document.hidden||active)return;
   active=new AbortController();const controller=active;
   try{const result=await api<Traffic>('/api/network-activity',controller.signal);if(!stopped&&!controller.signal.aborted){setData(result);setError(false);}}
   catch{if(!stopped&&!controller.signal.aborted)setError(true);}
   finally{if(active===controller)active=null;if(!stopped&&!document.hidden)timer=setTimeout(poll,5000);}
  }
  function visibility(){clearTimeout(timer);if(document.hidden){active?.abort();}else void poll();}
  document.addEventListener('visibilitychange',visibility);void poll();
  return()=>{stopped=true;clearTimeout(timer);active?.abort();document.removeEventListener('visibilitychange',visibility);};
 },[live]);
 const now=Date.now()/1000;
 const points=live?(error?[]:data?.points||[]):demoIn.map((v,i)=>({time:now-(29-i)*minutes*60/29,download:v*80000,upload:demoOut[i]*18000}));
 const visible=points.filter(p=>p.time>=now-minutes*60&&p.time<=now+5);
 const max=Math.max(1000,...visible.flatMap(p=>[p.download,p.upload]));
 const coordinate=(p:Point,key:'download'|'upload')=>`${Math.max(2,Math.min(238,2+(p.time-(now-minutes*60))/(minutes*60)*236))},${92-p[key]/max*84}`;
 const path=(key:'download'|'upload')=>visible.map(p=>coordinate(p,key)).join(' ');
 const current=visible.at(-1);
 const label=!live?'Demo':error?'Disconnected':data?.state==='live'?'Live':data?.state==='warming'?'Starting…':data?.state==='unavailable'?'Unavailable':'Worker offline';
 return <Paper component="section" withBorder radius="lg" p="md" aria-label={live?'Network activity':'Network activity preview'}><Stack gap="md">
  <Group justify="space-between"><Group gap="xs"><Network size={18}/><Title order={3} size="h5">Network</Title><Help text={live?'Traffic in and out of ReelDock, on both the web and worker containers, excluding loopback. Samples every 5 seconds. This is ReelDock only, not other apps on the machine. History resets when a container restarts.':'Sample traffic for the design preview.'}/></Group><Badge variant="light" color={data?.state==='live'?'green':'gray'}>{label}</Badge></Group>
  <SegmentedControl aria-label="Graph time window" value={String(minutes)} onChange={value=>setMinutes(Number(value))} data={[1,5,15].map(value=>({value:String(value),label:value+' min'}))} styles={{label:{minHeight:44,display:'flex',alignItems:'center',justifyContent:'center'}}}/>
  <Box style={{display:'grid',gridTemplateColumns:'64px minmax(0,1fr)',gap:8}}><Stack justify="space-between" h={144} aria-hidden="true">{[1,.5,0].map(fraction=><Text key={fraction} size="xs" c="dimmed">{rate(max*fraction)}</Text>)}</Stack><Box style={{minWidth:0}}><svg style={{width:'100%',height:144,display:'block'}} viewBox="0 0 240 100" preserveAspectRatio="none" role="img" aria-label={`${live?'ReelDock':'Sample'} download and upload activity over ${minutes} minutes${current?`. Download ${rate(current.download)}, upload ${rate(current.upload)}.`:'. No samples available.'}`}>
   <defs><linearGradient id={id} x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor="var(--mantine-primary-color-filled)" stopOpacity=".24"/><stop offset="100%" stopColor="var(--mantine-primary-color-filled)" stopOpacity="0"/></linearGradient></defs>
   {[8,50,92].map(y=><line key={y} x1="2" x2="238" y1={y} y2={y} stroke="var(--mantine-color-default-border)" strokeDasharray="2 4"/>)}
   {visible.length>1&&<><polygon points={`${coordinate(visible[0],'download').split(',')[0]},92 ${path('download')} ${coordinate(visible[visible.length-1],'download').split(',')[0]},92`} fill={`url(#${id})`}/><polyline points={path('download')} fill="none" stroke="var(--mantine-primary-color-filled)" strokeWidth="1.7" vectorEffect="non-scaling-stroke" strokeLinejoin="round"/><polyline points={path('upload')} fill="none" stroke="var(--mantine-color-violet-5)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" strokeDasharray="4 2" strokeLinejoin="round"/></>}
  </svg><Group justify="space-between" aria-hidden="true"><Text size="xs" c="dimmed">−{minutes} min</Text><Text size="xs" c="dimmed">Now</Text></Group></Box></Box>
  <SimpleGrid cols={2}><Paper withBorder radius="md" p="sm"><Group gap="xs"><ArrowDown size={16}/><Text size="sm" c="dimmed">Download</Text></Group><Text fw={600} size="lg">{current?rate(current.download):'—'}</Text></Paper><Paper withBorder radius="md" p="sm"><Group gap="xs"><ArrowUp size={16}/><Text size="sm" c="dimmed">Upload</Text></Group><Text fw={600} size="lg">{current?rate(current.upload):'—'}</Text></Paper></SimpleGrid>
  {(error||data?.state!=='live')&&<Text size="xs" c="dimmed">{!live?'Sample traffic':error?'Reconnecting…':data?.state==='live'?'':data?.state==='warming'?'Collecting the first sample…':data?.state==='unavailable'?'Network counters are unavailable.':'Waiting for the ReelDock worker.'}</Text>}
 </Stack></Paper>;
}
