import { Box, Fieldset, Stack, Text, VisuallyHidden } from '@mantine/core';
import { SelectField as NativeSelect } from '../components/SelectField';
import { useEffect, useState, type ReactNode } from 'react';
import type { DiscoverItem } from '../Discover';
import { api } from '../api';
import { acquisitionItem } from '../discoverPayload';
import { Button, PopupShell } from '../components/ui';
import { Group, Row, Toggle, Notice, useAction } from '../SettingsUI';
import { TextInput } from '../TextField';

type Options={key:string;item:DiscoverItem;shown:boolean;dates:boolean;favourite:boolean;automatic:boolean;quality:string;minimum:number|null;globalMinimum:number};
export function MovieSettingsFields({value,save,showFavourite=true}:{value:Options;save:(field:string,value:boolean|string|number|null)=>void;showFavourite?:boolean}) {
 return <><Group title="Guide">
  <Toggle title="Show in guide" value={value.shown} onChange={v=>save('shown',v)} detail="Show this movie as a row in the Guide. Hiding it keeps its options and download policy."/>
  <Toggle title="Release, premiere & source dates" value={value.dates} onChange={v=>save('dates',v)} detail="Show known release and premiere dates, plus the first time ReelDock records this movie in your sources. Premieres prefer UK, then US, then the earliest recorded premiere. Missing dates stay absent; a date without a time is labelled Date only."/>
  {showFavourite&&<Toggle title="Favourite" value={value.favourite} onChange={v=>save('favourite',v)}/>}
 </Group><Group title="Automatic download">
  <Toggle title="Automatic download" value={value.automatic} onChange={v=>save('automatic',v)} detail="Keep checking your sources and queue this movie until it is saved. Turning off stops pending automatic searches; a running download finishes. Cancelling a request holds it until you explicitly enable this option again."/>
  <Row title="Quality"><NativeSelect aria-label="Movie quality" value={value.quality} onChange={e=>save('quality',e.target.value)}><option value="default">Use global setting</option>{['720','1080','2160'].map(q=><option key={q} value={q}>{q==='2160'?'4K (2160p)':q+'p'}</option>)}</NativeSelect></Row>
  <Row title="Minimum length" detail={'Use the global movie/NZB minimum ('+value.globalMinimum+' minutes), or choose a minimum for this movie. Zero disables this additional minimum. Samples remain excluded; YouTube fallback keeps its existing full-movie checks.'}><NativeSelect aria-label="Movie minimum length" value={value.minimum===null?'default':'custom'} onChange={e=>save('minimum',e.target.value==='default'?null:value.globalMinimum)}><option value="default">Use global setting</option><option value="custom">Custom minimum</option></NativeSelect></Row>
  {value.minimum!==null&&<Row title="Minimum length (minutes)"><TextInput key={value.minimum} aria-label="Movie minimum length (minutes)" type="number" min="0" max="1440" step="1" defaultValue={value.minimum} onBlur={e=>{if(!e.currentTarget.value||!e.currentTarget.checkValidity()){e.currentTarget.reportValidity();return;}const minutes=Number(e.currentTarget.value);if(minutes!==value.minimum)save('minimum',minutes);}}/></Row>}
 </Group></>;
}

export default function MovieOptions({item,csrf,onPolicyChange,files,embedded=false,showFavourite=true}:{item:DiscoverItem;csrf:string;onPolicyChange?:()=>void;files?:ReactNode;embedded?:boolean;showFavourite?:boolean}) {
 const [value,setValue]=useState<Options|null>(null),[error,setError]=useState(''),[revision,setRevision]=useState(0);
 const action=useAction(csrf);
 useEffect(()=>{const changed=()=>setRevision(v=>v+1);window.addEventListener('reeldock-watchlist-changed',changed);return()=>window.removeEventListener('reeldock-watchlist-changed',changed);},[]);
 useEffect(()=>{const c=new AbortController();setError('');api<Options>('/api/discovery-engine/movie-options',c.signal,{item:acquisitionItem(item)},csrf).then(result=>{if(!c.signal.aborted)setValue(result);}).catch(e=>{if(!c.signal.aborted)setError(e.message);});return()=>c.abort();},[item.id,csrf,revision]);
 async function save(field:string,next:boolean|string|number|null){const result=await action.run<Options>('/api/discovery-engine/movie-options',{item:acquisitionItem(item),settings:{[field]:next}});if(result){setValue(result);if(field==='quality')onPolicyChange?.();window.dispatchEvent(new CustomEvent('reeldock-movie-options-changed',{detail:{key:result.key,field}}));}}
 const body=<Stack gap="md">
  {value?<Fieldset variant="unstyled" p={0} disabled={action.busy}><Stack gap="lg"><MovieSettingsFields value={value} showFavourite={showFavourite} save={(field,next)=>void save(field,next)}/></Stack></Fieldset>:error?<><Notice text={error} error/><Button variant="secondary" onClick={()=>setRevision(n=>n+1)}>Try again</Button></>:<Text size="sm" role="status">Loading movie options…</Text>}
  {action.busy&&<VisuallyHidden role="status">Saving…</VisuallyHidden>}<Notice text={action.notice}/><Notice text={action.error} error/>
  {files}
 </Stack>;
 // Inside a DetailSheet tab the fields render bare; standalone callers keep
 // the approved banner/title chrome.
 return embedded?body:<PopupShell banner={item.backdrop||item.image} title={item.title} subtitle="Movie Options">{body}</PopupShell>;
}
