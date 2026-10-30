import { Badge, Group, Paper, Stack, Text } from '@mantine/core';
import { useEffect, useRef, useState } from 'react';
import { TextInput } from '../TextField';
import DownloadPanel from '../downloadUI/DownloadPanel';
import { notify } from '../notifications';
import { api } from '../api';
import { SelectField } from './SelectField';
import DropZone from './DropZone';
import { Button } from './ui';

type Preview={id:string;name:string;title:string;kind:'movies'|'series'};
const FILE_LIMIT=16_384;
function fileProblem(file:File|null){
 if(!file)return '';
 if(!file.name.toLowerCase().endsWith('.strm'))return 'Choose a .strm file.';
 if(!file.size)return 'That .strm file is empty.';
 if(file.size>FILE_LIMIT)return 'Choose a .strm file no larger than 16 KB.';
 return '';
}

/** Parse one local link file, confirm its name and destination, then queue it. */
export default function StrmDownload({csrf,onStarted,onOpenChange}:{csrf:string;onStarted?:()=>void;onOpenChange?:(open:boolean)=>void}){
 const [file,setFile]=useState<File|null>(null),[preview,setPreview]=useState<Preview|null>(null);
 const [title,setTitle]=useState(''),[kind,setKind]=useState<'movies'|'series'>('movies');
 const [busy,setBusy]=useState(''),[error,setError]=useState('');
 const requestRef=useRef<AbortController|null>(null),pending=useRef(false);
 useEffect(()=>()=>requestRef.current?.abort(),[]);
 async function submit(){
  if(pending.current||!file||fileProblem(file)||(preview&&(!title.trim()||title.trim().length>140)))return;
  pending.current=true;setBusy(preview?'start':'check');setError('');
  const controller=new AbortController();requestRef.current=controller;
  try{
   const body=new FormData();body.append('strm',file);
   if(preview){body.append('preview_id',preview.id);body.append('title',title.trim());body.append('kind',kind);}
   const result=await api<Preview|{id:string;state:string}>(preview?'/api/strm-downloads':'/api/strm/preview',controller.signal,body,csrf,{feedback:'inline'});
   if(controller.signal.aborted)return;
   if(preview){notify({kind:'success',message:'STRM download queued. Follow its progress in Activity.'});onStarted?.();onOpenChange?.(false);}
   else{const checked=result as Preview;setPreview(checked);setTitle(checked.title);setKind(checked.kind);}
  }catch(problem){if(!controller.signal.aborted)setError(problem instanceof Error?problem.message:'Could not read this STRM file. Try again.');}
  finally{if(requestRef.current===controller){requestRef.current=null;pending.current=false;if(!controller.signal.aborted)setBusy('');}}
 }
 function changeFile(next:File|null){setFile(next);setPreview(null);setTitle('');setError(fileProblem(next));}
 return <Stack component="form" gap="md" style={{flex:'1 0 auto',minWidth:0,width:'100%'}} onSubmit={event=>{event.preventDefault();void submit();}}>
  <DownloadPanel fill error={error} actions={preview?<><Button type="button" variant="secondary" disabled={!!busy} onClick={()=>{setPreview(null);setTitle('');setError('');}}>Change source</Button><Button type="submit" style={{minWidth:160,height:44}} disabled={!!busy||!title.trim()||title.trim().length>140}>{busy==='start'?'Starting…':'Start download'}</Button></>:<Button type="submit" style={{minWidth:160,height:44}} disabled={!!busy||!file||!!fileProblem(file)}>{busy==='check'?'Checking…':'Check file'}</Button>}>
   {!preview?<Stack gap="sm"><DropZone label="STRM file" description="Choose a .strm file no larger than 16 KB, or drag it here." accept=".strm,text/plain" file={file} onFile={changeFile} disabled={!!busy}/><Text size="sm" c="dimmed">Import a link to one downloadable HTTP or HTTPS video file. Live streams, playlists and YouTube links use other download options.</Text></Stack>:<Stack gap="md"><Paper withBorder p="md" radius="md" role="status"><Stack gap="xs"><Group justify="space-between" gap="sm"><Text fw={600} style={{overflowWrap:'anywhere'}}>{preview.name}</Text><Badge variant="light">STRM</Badge></Group><Text size="sm" c="dimmed">The file was read. The media downloads after you choose Start download.</Text></Stack></Paper><TextInput label="Save as" aria-label="STRM save as" description="The finished video uses this name in your movie or TV library. Keep the episode number for a TV episode." value={title} maxLength={140} disabled={!!busy} onChange={event=>setTitle(event.currentTarget.value)}/><SelectField aria-label="STRM media type" description="Choose which media library receives the video." value={kind} disabled={!!busy} onChange={event=>setKind(event.currentTarget.value as 'movies'|'series')} data={[{value:'movies',label:'Movie'},{value:'series',label:'TV episode'}]}/></Stack>}
  </DownloadPanel>
 </Stack>;
}
