import { Alert, Badge, Group, Paper, Radio, Stack, Text } from '@mantine/core';
import { useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, Search } from 'lucide-react';
import { api } from './api';
import { Button, Dialog, DialogContent, DialogDescription, DialogTitle } from './components/ui';
import LoadingStatus from './components/LoadingStatus';
import { pageHidden } from './visibility';

export type SourceChoice={requestId:string;attemptKey:string;eligible:true};
export type SourceAlternative={id:string;title:string;quality:string;size:number|null;sizeKnown:boolean;differencePercent:number|null;recommended:boolean;availability:'unchecked';source?:string};
export type SourceAlternativesPage={requestId:string;attemptKey:string;revision:string;state:'idle'|'searching'|'ready'|'failed';loading:boolean;original:{title:string;quality:string;size:number|null;source:string};items:SourceAlternative[];message:string;error:string;refreshAfterMs:number};
const prompted=new Map<string,Set<string>>(),initialSearches=new Map<string,Set<string>>(),scopes=new Map<string,Promise<string|null>>();
export function sourceChoiceKey(choice:SourceChoice){return JSON.stringify([choice.requestId,choice.attemptKey]);}
export function validSourceChoice(value:unknown):value is SourceChoice{
 const choice=value as Partial<SourceChoice>|null;
 return !!choice&&choice.eligible===true&&typeof choice.requestId==='string'&&choice.requestId.length>0&&choice.requestId.length<=200&&typeof choice.attemptKey==='string'&&choice.attemptKey.length>0&&choice.attemptKey.length<=500;
}
function promptScope(csrf:string){
 let scope=scopes.get(csrf);if(scope)return scope;
 scope=typeof crypto!=='undefined'&&crypto.subtle?crypto.subtle.digest('SHA-256',new TextEncoder().encode(csrf)).then(bytes=>'reeldock-source-choice:'+Array.from(new Uint8Array(bytes),n=>n.toString(16).padStart(2,'0')).join('')).catch(()=>null):Promise.resolve(null);
 scopes.set(csrf,scope);if(scopes.size>8)scopes.delete(scopes.keys().next().value!);return scope;
}
export async function sourceChoicePrompted(csrf:string,choice:SourceChoice){
 const key=sourceChoiceKey(choice);if(prompted.get(csrf)?.has(key))return true;
 const scope=await promptScope(csrf);if(!scope)return false;
 try{const saved=JSON.parse(sessionStorage.getItem(scope)||'[]');return Array.isArray(saved)&&saved.includes(key);}catch{return false;}
}
export async function markSourceChoicePrompted(csrf:string,choice:SourceChoice){
 const key=sourceChoiceKey(choice),seen=prompted.get(csrf)||new Set<string>();seen.add(key);prompted.set(csrf,seen);
 if(prompted.size>8)prompted.delete(prompted.keys().next().value!);
 const scope=await promptScope(csrf);if(!scope)return;
 try{const saved=JSON.parse(sessionStorage.getItem(scope)||'[]');sessionStorage.setItem(scope,JSON.stringify([...new Set([...(Array.isArray(saved)?saved.filter(value=>typeof value==='string'):[]),key])].slice(-100)));}catch{/* In-memory dismissal remains available when browser storage is disabled. */}
}
export async function initialSourceSearchStarted(csrf:string,choice:SourceChoice){
 const key=sourceChoiceKey(choice);if(initialSearches.get(csrf)?.has(key))return true;
 const scope=await promptScope(csrf);if(!scope)return false;
 try{const saved=JSON.parse(sessionStorage.getItem(scope+':initial-search')||'[]');return Array.isArray(saved)&&saved.includes(key);}catch{return false;}
}
export async function markInitialSourceSearch(csrf:string,choice:SourceChoice){
 const key=sourceChoiceKey(choice),seen=initialSearches.get(csrf)||new Set<string>();seen.add(key);initialSearches.set(csrf,seen);
 if(initialSearches.size>8)initialSearches.delete(initialSearches.keys().next().value!);
 const scope=await promptScope(csrf);if(!scope)return;
 try{const saved=JSON.parse(sessionStorage.getItem(scope+':initial-search')||'[]');sessionStorage.setItem(scope+':initial-search',JSON.stringify([...new Set([...(Array.isArray(saved)?saved.filter(value=>typeof value==='string'):[]),key])].slice(-100)));}catch{/* Never repeat an initial write merely because browser storage is disabled. */}
}
export async function claimInitialSourceSearch(csrf:string,choice:SourceChoice,active:()=>boolean){
 const previous=await initialSourceSearchStarted(csrf,choice);
 if(!active()||previous||initialSearches.get(csrf)?.has(sourceChoiceKey(choice)))return false;
 await markInitialSourceSearch(csrf,choice);return active();
}
export function alternativeSize(size:number|null){return typeof size==='number'&&Number.isFinite(size)&&size>0?(size>=1073741824?(size/1073741824).toFixed(2)+' GB':(size/1048576).toFixed(1)+' MB'):'Size unknown';}
export function alternativeDifference(value:number|null){return typeof value!=='number'||!Number.isFinite(value)?'Size difference unknown':value===0?'Same size':Math.abs(value).toFixed(1)+'% '+(value>0?'larger':'smaller');}
function quality(value:string){return /^\d{3,4}$/.test(value)?value+'p':value||'Quality unknown';}
function checkedPage(value:SourceAlternativesPage,choice:SourceChoice){
 if(!value||value.requestId!==choice.requestId||value.attemptKey!==choice.attemptKey||typeof value.revision!=='string'||!Array.isArray(value.items)||!value.original||!['idle','searching','ready','failed'].includes(value.state))throw new Error('This source choice changed. Refresh Activity and open alternatives again.');
 return value;
}

/** One dialog owns comparison, searching and selection; it never retries a POST. */
export default function SourceAlternatives({choice,autoChoices=[],allowAuto=true,autoSearch=true,csrf,onClose,onSelected}:{choice?:SourceChoice|null;autoChoices?:SourceChoice[];allowAuto?:boolean;autoSearch?:boolean;csrf:string;onClose:()=>void;onSelected:()=>void}){
 const [automatic,setAutomatic]=useState<SourceChoice|null>(null),[visibility,setVisibility]=useState(0);
 const [page,setPage]=useState<SourceAlternativesPage|null>(null),[reading,setReading]=useState(false),[sending,setSending]=useState<'search'|'select'|null>(null),[error,setError]=useState(''),[selected,setSelected]=useState(''),[revision,setRevision]=useState(0);
 const current=choice||automatic,currentKey=current?sourceChoiceKey(current):'',eligibleKeys=autoChoices.map(sourceChoiceKey).join('|');
 const considered=useRef(new Set<string>()),readRequest=useRef<AbortController|null>(null),actionRequest=useRef<AbortController|null>(null),loadedKey=useRef('');
 useEffect(()=>{considered.current=new Set();setAutomatic(null);loadedKey.current='';setPage(null);setSelected('');},[csrf]);
 useEffect(()=>{const visible=()=>{if(!pageHidden())setVisibility(value=>value+1);};document.addEventListener('visibilitychange',visible);return()=>document.removeEventListener('visibilitychange',visible);},[]);
 useEffect(()=>{
  if(automatic&&!autoChoices.some(value=>sourceChoiceKey(value)===sourceChoiceKey(automatic)))setAutomatic(null);
 },[eligibleKeys,automatic]);
 useEffect(()=>{
  if(!choice)return;considered.current.add(sourceChoiceKey(choice));void markSourceChoicePrompted(csrf,choice);
 },[csrf,choice&&sourceChoiceKey(choice)]);
 useEffect(()=>{
  if(!allowAuto||choice||automatic||pageHidden()||!autoChoices.length||document.querySelector('[role="dialog"]'))return;
  let alive=true;
  void (async()=>{for(const candidate of autoChoices){
   if(considered.current.has(sourceChoiceKey(candidate)))continue;
   const seen=await sourceChoicePrompted(csrf,candidate);
   if(!alive||pageHidden()||document.querySelector('[role="dialog"]'))return;
   considered.current.add(sourceChoiceKey(candidate));if(seen)continue;
   autoChoices.forEach(value=>considered.current.add(sourceChoiceKey(value)));
   void markSourceChoicePrompted(csrf,candidate);setAutomatic(candidate);return;
  }})();
  return()=>{alive=false;};
 },[csrf,eligibleKeys,allowAuto,choice,automatic,visibility]);
 useEffect(()=>{setSending(null);return()=>{actionRequest.current?.abort();actionRequest.current=null;};},[csrf,currentKey]);
 useEffect(()=>{
  if(!current)return;
  const target=current;let alive=true,timer:ReturnType<typeof setTimeout>|undefined;
  if(loadedKey.current!==currentKey){loadedKey.current=currentKey;setPage(null);setSelected('');setError('');}
  async function read(){
   if(!alive||pageHidden()||readRequest.current||actionRequest.current)return;
   const controller=new AbortController();readRequest.current=controller;setReading(true);
   try{const result=checkedPage(await api<SourceAlternativesPage>('/api/source-alternatives?'+new URLSearchParams({request:target.requestId}),controller.signal,undefined,undefined,{feedback:'inline'}),target);
    if(alive&&!controller.signal.aborted){setPage(result);setError('');setSelected(value=>result.items.some(item=>item.id===value)?value:(result.items.find(item=>item.recommended)||result.items[0])?.id||'');if(result.loading)timer=setTimeout(read,Math.max(1000,Math.min(5000,result.refreshAfterMs||1500)));}
   }catch(problem){if(alive&&!controller.signal.aborted)setError(problem instanceof Error?problem.message:'Could not read alternatives.');}
   finally{if(readRequest.current===controller){readRequest.current=null;if(alive)setReading(false);}}
  }
  function visible(){clearTimeout(timer);if(pageHidden()){readRequest.current?.abort();readRequest.current=null;setReading(false);}else void read();}
  void read();document.addEventListener('visibilitychange',visible);
  return()=>{alive=false;clearTimeout(timer);readRequest.current?.abort();readRequest.current=null;document.removeEventListener('visibilitychange',visible);};
 },[csrf,currentKey,revision]);
 function close(){if(current)void markSourceChoicePrompted(csrf,current);setAutomatic(null);onClose();}
 async function action(kind:'search'|'select'){
  if(!current||actionRequest.current)return;
  if(kind==='select'&&(!page||page.loading||page.state!=='ready'||!page.revision||!page.items.some(item=>item.id===selected)))return;
  const controller=new AbortController();actionRequest.current=controller;readRequest.current?.abort();readRequest.current=null;setSending(kind);setError('');
  try{
   if(kind==='search'){
    void markInitialSourceSearch(csrf,current);
    await api('/api/source-alternatives',controller.signal,{action:'search',requestId:current.requestId},csrf,{feedback:'inline'});
    if(!controller.signal.aborted){setPage(value=>value?{...value,loading:true,state:'searching'}:value);setRevision(value=>value+1);}
   }else{
    const result=await api<{ok:boolean;requestId:string;replacement:{id:string;state:string}}>('/api/source-alternatives',controller.signal,{action:'select',requestId:current.requestId,candidateId:selected,revision:page!.revision},csrf,{feedback:'inline'});
    if(!controller.signal.aborted){if(result.ok!==true||result.requestId!==current.requestId||!result.replacement?.id)throw new Error('The replacement could not be confirmed. Refresh Activity before choosing again.');onSelected();close();}
   }
  }catch(problem){if(!controller.signal.aborted)setError(problem instanceof Error?problem.message:'Could not update this source choice.');}
  finally{if(actionRequest.current===controller){actionRequest.current=null;setSending(null);}}
 }
 useEffect(()=>{
  if(!autoSearch||!current||!page||page.requestId!==current.requestId||page.attemptKey!==current.attemptKey||page.state!=='idle'||page.loading||reading||sending||error||pageHidden())return;
  let alive=true;
  void claimInitialSourceSearch(csrf,current,()=>alive&&!pageHidden()&&!actionRequest.current).then(claimed=>{if(claimed&&alive)void action('search');});
  return()=>{alive=false;};
 },[csrf,currentKey,autoSearch,page?.state,page?.revision,page?.loading,reading,sending,error,visibility]);
 if(!current)return null;
 const working=!!sending||!!page?.loading,canDownload=!!page&&page.state==='ready'&&!working&&!reading&&!error&&page.items.some(item=>item.id===selected)&&!!page.revision;
 return <Dialog open onOpenChange={opened=>{if(!opened)close();}}><DialogContent>
  <Stack gap="lg"><Stack gap="xs"><DialogTitle>Choose another source</DialogTitle><DialogDescription>The selected source failed. Compare replacements for this download.</DialogDescription></Stack>
   {page&&<Paper withBorder radius="md" p="md"><Stack gap={4}><Text fw={600} style={{overflowWrap:'anywhere'}}>{page.original.title}</Text><Text size="sm" c="dimmed">Original source · {quality(page.original.quality)} · {alternativeSize(page.original.size)} · {page.original.source||'NZB'}</Text></Stack></Paper>}
   <LoadingStatus active={reading&&!page} label="Reading saved alternatives…"/>
   {error&&<Alert color="red" title="Could not update alternatives"><Stack gap="sm"><Text size="sm" style={{overflowWrap:'anywhere'}}>{error}</Text><Button variant="secondary" disabled={!!sending} onClick={()=>setRevision(value=>value+1)}>Refresh alternatives</Button></Stack></Alert>}
   {page?.message&&<Text size="sm" c="dimmed" role="status">{page.message}</Text>}{page?.error&&<Alert color="red">{page.error}</Alert>}
   {page?.loading&&<LoadingStatus active label="Searching configured sources…"/>}
   {!!page?.items.length&&<Radio.Group label="Alternative sources" value={selected} onChange={setSelected} name="source-alternative"><Stack gap="sm" mt="xs">{page.items.map((item,index)=><Paper key={item.id} withBorder radius="md" p="sm"><Radio value={item.id} disabled={working} label={<Stack gap={4}><Group gap="xs" wrap="wrap"><Text fw={600} size="sm">Option {index+1}</Text>{item.recommended&&<Badge variant="light">Recommended</Badge>}</Group><Text size="sm" style={{overflowWrap:'anywhere'}}>{item.title}</Text><Text size="sm" c="dimmed">{quality(item.quality)} · {alternativeSize(item.size)} · {item.source||'NZB'}</Text><Text size="xs" c="dimmed">{alternativeDifference(item.differencePercent)}</Text></Stack>} styles={{body:{alignItems:'flex-start',minHeight:44},labelWrapper:{minWidth:0,flex:1},label:{whiteSpace:'normal'}}}/></Paper>)}</Stack></Radio.Group>}
   {page?.state==='ready'&&!page.items.length&&<Text size="sm">No suitable alternatives were found within the current quality and size settings.</Text>}
   <Text size="xs" c="dimmed">Availability has not been verified. A matching release may still be missing articles on your Usenet server.</Text>
   <Group gap="sm" wrap="wrap"><Button variant="secondary" disabled={working||reading} onClick={()=>void action('search')}><Search size={18}/>{sending==='search'?'Starting search…':'Search alternatives'}</Button><Button disabled={!canDownload} onClick={()=>void action('select')}><ArrowDownToLine size={18}/>{sending==='select'?'Queuing…':'Download selected'}</Button><Button variant="ghost" disabled={!!sending} onClick={close}>Dismiss</Button></Group>
  </Stack>
 </DialogContent></Dialog>;
}
