import { Box, Fieldset, Stack, Text, VisuallyHidden } from '@mantine/core';
import { SelectField as NativeSelect } from '../components/SelectField';
import { useEffect, useState } from 'react';
import type { DiscoverItem } from '../Discover';
import { api } from '../api';
import { acquisitionItem } from '../discoverPayload';
import { Button, PopupShell } from '../components/ui';
import { Group, Row, Toggle, Notice, useAction } from '../SettingsUI';
import { TextInput } from '../TextField';

type Options={key:string;shown:boolean;favourite:boolean;downloadAll:boolean;downloadFuture:boolean;automaticSupported:boolean;automaticMessage:string;quality:string;minimum:number|null;globalMinimum:number};
export function ShowSettingsFields({value,save,showFavourite=true}:{value:Options;save:(field:string,value:boolean|string|number|null)=>void;showFavourite?:boolean}) {
 return <><Group title="Guide">
  <Toggle title="Show in guide" value={value.shown} onChange={v=>save('shown',v)} detail="Show this TV show and its known episode air dates in the Guide. Hiding it keeps its download settings. Unknown dates stay absent."/>
  {showFavourite&&<Toggle title="Favourite" value={value.favourite} onChange={v=>save('favourite',v)}/>}
 </Group><Group title="Automatic download">
  <Toggle title="Automatic Future Downloads" value={value.downloadFuture} disabled={!value.automaticSupported&&!value.downloadFuture} onChange={v=>save('downloadFuture',v)} detail="Keep checking for episodes airing from the day this is enabled. Turning off stops pending future searches; active downloads finish. Individually cancelled episodes stay held until you explicitly request that episode again."/>
  <Toggle title="Download All Seasons" value={value.downloadAll} disabled={!value.automaticSupported&&!value.downloadAll} onChange={v=>save('downloadAll',v)} detail="Request all currently released episodes across all seasons, including known numbered specials. Existing saved episodes are kept. Turning off stops pending batch searches; active downloads finish. Cancelled episodes stay held."/>
  {!value.automaticSupported&&<Text size="sm" role="status">{value.automaticMessage}</Text>}
  <Row title="Quality"><NativeSelect aria-label="TV show quality" value={value.quality} onChange={e=>save('quality',e.target.value)}><option value="default">Use global setting</option>{['720','1080','2160'].map(q=><option key={q} value={q}>{q==='2160'?'4K (2160p)':q+'p'}</option>)}</NativeSelect></Row>
  <Row title="Minimum length" detail={'Use the global NZB minimum ('+value.globalMinimum+' minutes), or choose a minimum for this show. Zero disables this additional minimum; samples remain excluded. Existing manual episode overrides are retained.'}><NativeSelect aria-label="TV show minimum length" value={value.minimum===null?'default':'custom'} onChange={e=>save('minimum',e.target.value==='default'?null:value.globalMinimum)}><option value="default">Use global setting</option><option value="custom">Custom minimum</option></NativeSelect></Row>
  {value.minimum!==null&&<Row title="Minimum length (minutes)"><TextInput key={value.minimum} aria-label="TV show minimum length (minutes)" type="number" min="0" max="1440" step="1" defaultValue={value.minimum} onBlur={e=>{if(!e.currentTarget.value||!e.currentTarget.checkValidity()){e.currentTarget.reportValidity();return;}const minutes=Number(e.currentTarget.value);if(minutes!==value.minimum)save('minimum',minutes);}}/></Row>}
 </Group></>;
}

export default function ShowOptions({item,displayItem=item,csrf,onPolicyChange,catalogueReady=true,embedded=false,showFavourite=true}:{item:DiscoverItem;displayItem?:DiscoverItem;csrf:string;onPolicyChange:()=>void;catalogueReady?:boolean;embedded?:boolean;showFavourite?:boolean}) {
 const [value,setValue]=useState<Options|null>(null),[error,setError]=useState(''),[revision,setRevision]=useState(0);
 const action=useAction(csrf);
 useEffect(()=>{const changed=()=>setRevision(v=>v+1),optionsChanged=(event:Event)=>{const key=(event as CustomEvent<{key?:string}>).detail?.key;if(!key||key===item.id)changed();};window.addEventListener('reeldock-watchlist-changed',changed);window.addEventListener('reeldock-show-options-changed',optionsChanged);return()=>{window.removeEventListener('reeldock-watchlist-changed',changed);window.removeEventListener('reeldock-show-options-changed',optionsChanged);};},[item.id]);
 useEffect(()=>{const c=new AbortController();setError('');api<Options>('/api/discovery-engine/show-options',c.signal,{item:acquisitionItem(item)},csrf).then(result=>{if(!c.signal.aborted)setValue(result);}).catch(e=>{if(!c.signal.aborted)setError(e.message);});return()=>c.abort();},[item.id,item.title,csrf,revision,catalogueReady]);
 async function save(field:string,next:boolean|string|number|null){const result=await action.run<Options>('/api/discovery-engine/show-options',{item:acquisitionItem(item),settings:{[field]:next}});if(result){setValue(result);if(field==='quality')onPolicyChange();window.dispatchEvent(new CustomEvent('reeldock-show-options-changed',{detail:{key:result.key,field}}));}}
 const body=<Stack gap="md">
  {value?<Fieldset variant="unstyled" p={0} disabled={action.busy}><Stack gap="lg"><ShowSettingsFields value={value} showFavourite={showFavourite} save={(field,next)=>void save(field,next)}/></Stack></Fieldset>:error?<><Notice text={error} error/><Button variant="secondary" onClick={()=>setRevision(n=>n+1)}>Try again</Button></>:<Text size="sm" role="status">Loading TV show options…</Text>}
  {action.busy&&<VisuallyHidden role="status">Saving…</VisuallyHidden>}<Notice text={action.notice}/><Notice text={action.error} error/>
 </Stack>;
 return embedded?body:<PopupShell banner={displayItem.backdrop||displayItem.image} title={displayItem.title} subtitle="TV Show Options">{body}</PopupShell>;
}
