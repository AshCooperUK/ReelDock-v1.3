import { Text as PresentationText } from '@mantine/core';
import { SelectField as NativeSelect } from '../components/SelectField';
import { Stack as SettingsStack } from '@mantine/core';

import { useEffect, useState } from 'react';
import { api } from '../api';
import { Group,Row,Toggle,Notice } from '../SettingsUI';
import { useEngineSettings } from './useEngineSettings';
type Status={counts:Record<string,number>;updatedAt:string|null;stage:string;message:string;scope:string};
export default function CatalogueSettings({csrf}:{csrf:string}){
 const preferences=useEngineSettings<{enabled:boolean;minutes:number}>('catalogue',csrf);
 const [status,setStatus]=useState<Status|null>(null),[error,setError]=useState('');
 useEffect(()=>{const c=new AbortController();let timer:ReturnType<typeof setTimeout>;
  async function load(){try{const next=await api<Status>('/api/view-media/catalogue-status',c.signal);if(!c.signal.aborted){setStatus(next);setError('');}}catch(e){if(!c.signal.aborted)setError(e instanceof Error?e.message:'Could not read catalogue status.');}finally{if(!c.signal.aborted)timer=setTimeout(load,10000);}}
  void load();return()=>{c.abort();clearTimeout(timer);};
 },[preferences.values?.enabled,preferences.values?.minutes]);
 return <><fieldset style={{ border: 0, margin: 0, padding: 0, minWidth: 0 }} disabled={!preferences.values||preferences.busy}><SettingsStack gap="md"><Group title="Browse & search catalogue">
  <Toggle title="Pre-download metadata" value={preferences.values?.enabled??false} onChange={enabled=>void preferences.save({enabled})} detail="Save movie/TV lists and known source records on this server for quick browsing and search. This downloads metadata only. Existing source limits still apply. Results display together; new provider matches are saved in the background for your next search. Turning this off restores live browsing and keeps saved metadata."/>
  <Row title="Refresh interval"><NativeSelect aria-label="Catalogue refresh interval" value={preferences.values?.minutes??60} onChange={e=>void preferences.save({minutes:Number(e.target.value)})}>{[[30,'Every 30 minutes'],[60,'Every hour'],[180,'Every 3 hours'],[360,'Every 6 hours'],[720,'Every 12 hours'],[1440,'Every day']].map(([value,label])=><option key={value} value={value}>{label}</option>)}</NativeSelect></Row>
  <Row title="Saved catalogue" detail={status?.scope||'Saved source observations; this is not a complete copy of every provider archive.'}><PresentationText component="span" size="sm">{status?`${status.counts.movie||0} movies · ${status.counts.show||0} TV shows · ${status.counts.video||0} YouTube videos`:'Reading…'}</PresentationText></Row>
  <Row title="Refresh status"><PresentationText component="span" size="sm">{preferences.values?.enabled?(status?.stage||'Waiting for worker'):'Pre-download off'}{status?.updatedAt&&' · Updated '+new Date(status.updatedAt).toLocaleString()}</PresentationText></Row>
 </Group></SettingsStack></fieldset><Notice text={preferences.loadError||preferences.error||error||status?.message||''} error/><Notice text={preferences.busy?'Saving…':preferences.notice}/></>;
}
