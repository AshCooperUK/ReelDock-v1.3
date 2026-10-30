import { Group, Paper, Stack, Text } from '@mantine/core';
import { NotificationMessage } from '../notifications';
import {useEffect,useState,type ReactNode} from 'react';
import {ArrowDownToLine} from 'lucide-react';
import {api} from '../api';
import {Button} from '../components/ui';
import type {DiscoverItem} from '../Discover';
import MovieReleaseSelect from './MovieReleaseSelect';
import { DownloadChoiceLayout, DownloadSourcePicker } from './ReleaseChoicePicker';

type Release={id:string;title:string;size:number;source:string;reason:string;vod?:{source:string;path:string}};
type Episode={key:string;title:string;season:number|null;number:number|null};
export default function EpisodeDownload({item,episode,csrf,globalSearch,onQueued,fallbackAction,favouriteAction,standalone=false}:{item:DiscoverItem;episode:Episode;csrf:string;globalSearch:boolean;onQueued:()=>Promise<unknown>;fallbackAction?:ReactNode;favouriteAction?:ReactNode;standalone?:boolean}){
 const [releases,setReleases]=useState<Release[]>([]),[source,setSource]=useState(''),[release,setRelease]=useState(''),[busy,setBusy]=useState(true),[sending,setSending]=useState(false),[message,setMessage]=useState(''),[error,setError]=useState('');
 useEffect(()=>{
  const c=new AbortController();setBusy(true);setReleases([]);setSource('');setRelease('');setMessage('');setError('');
  type Vod={sources:{label:string;type:string;entries:{source:string;path:string;title:string;season:number|null;number:number|null}[]}[]};
  const vod=globalSearch?api<Vod>('/api/view-media/download-sources?'+new URLSearchParams({id:item.id,kind:'show',externalId:item.externalId||'',imdbId:item.imdbId||'',title:item.title,year:item.year||''}),c.signal):Promise.resolve({sources:[]});
  Promise.allSettled([api<{items:Release[];message:string}>('/api/media-management',c.signal,{action:'releases',key:episode.key},csrf),vod] as const).then(([nzb,vod])=>{
   if(c.signal.aborted)return;
   const options:Release[]=nzb.status==='fulfilled'?[...nzb.value.items]:[];
   if(vod.status==='fulfilled')for(const provider of vod.value.sources.filter(s=>s.type==='vod'))for(const entry of provider.entries.filter(e=>e.season===episode.season&&e.number===episode.number&&e.number!==null))options.push({id:'vod:'+entry.source+':'+entry.path,title:entry.title,size:0,source:provider.label,reason:'',vod:{source:entry.source,path:entry.path}});
   setReleases(options);const first=options.find(r=>!r.reason)||options[0];setSource(first?.source||'');setRelease(first&&!first.reason?first.id:'');
   setError([nzb.status==='rejected'?nzb.reason.message:'',vod.status==='rejected'?vod.reason.message:''].filter(Boolean).join(' '));
   setMessage(nzb.status==='fulfilled'&&!options.length?nzb.value.message:'');
  }).finally(()=>{if(!c.signal.aborted)setBusy(false);});
  return()=>c.abort();
 },[episode.key,episode.season,episode.number,item.id,csrf,globalSearch]);
 const chosen=releases.find(r=>r.id===release&&r.source===source&&!r.reason);
 async function download(){
  if(!chosen||sending)return;setSending(true);setMessage('');setError('');
  try{
   if(chosen.vod)await api('/api/discovery-engine/download',undefined,{key:episode.key,vod:chosen.vod},csrf);
   else await api('/api/media-management',undefined,{action:'search',key:episode.key,value:chosen.id},csrf);
   await onQueued();window.dispatchEvent(new Event('reeldock-library-changed'));
  }catch(e){setError(e instanceof Error?e.message:'Could not request this episode.');}
  finally{setSending(false);}
 }
 const downloadAction=!busy&&!chosen&&fallbackAction?fallbackAction:<Button variant="secondary" aria-label={(sending?'Saving request':'Download')+' · '+episode.title} title={sending?'Saving request':'Download'} disabled={busy||sending||!chosen} onClick={()=>void download()}><ArrowDownToLine size={18}/>{sending?"Adding…":"Download"}</Button>;
 const action=<>{favouriteAction}{downloadAction}</>;
 return <Paper withBorder p="md" radius="md"><Stack gap="sm">{releases.length?<DownloadChoiceLayout source={<DownloadSourcePicker sources={Array.from(new Set(releases.map(r=>r.source)))} value={source} onChange={value=>{setSource(value);setRelease(releases.find(r=>r.source===value&&!r.reason)?.id||'');}}/>} file={<MovieReleaseSelect releases={releases.filter(r=>r.source===source)} value={release} onChange={setRelease} label="Download file"/>} action={action}/>:<Group>{busy&&<Text size="sm" role="status">Finding available files…</Text>}{action}</Group>}{message&&<Text size="sm" role="status">{message}</Text>}<NotificationMessage kind="error" message={error}/></Stack></Paper>;
}
