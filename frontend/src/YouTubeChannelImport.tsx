import { FileInput, Group as Actions, Stack as SettingsStack, Text } from '@mantine/core';
import { TextArea } from './TextField';
import { useEffect, useRef, useState } from 'react';
import { Button, PanelButton } from './components/ui';
import { Field, Group, Notice, Row, useAction } from './SettingsUI';
import { api } from './api';
type Queue={queued:number;items:{url:string;state:string;message:string}[]};
export default function YouTubeChannelImport({csrf}:{csrf:string}) {
 const [csvFile,setCsvFile]=useState<File|null>(null),csvReset=useRef<() => void>(null);
 const [channels,setChannels]=useState(''),[queue,setQueue]=useState<Queue|null>(null),[error,setError]=useState(''),[revision,setRevision]=useState(0),a=useAction(csrf);
 useEffect(()=>{const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;
  async function load(){try{const data=await api<Queue>('/api/youtube/channels',controller.signal);if(!controller.signal.aborted){setQueue(data);setError('');}}catch(e){if(!controller.signal.aborted)setError(e instanceof Error?e.message:'Could not load channel imports.');}finally{if(!controller.signal.aborted)timer=setTimeout(load,5000);}}
  void load();return()=>{controller.abort();clearTimeout(timer);};
 },[revision]);
 return <PanelButton title="Add or import channels"><SettingsStack gap="md">
  <Text size="sm" c="dimmed">Add channels to ReelDock using their YouTube channel links, @handles or channel IDs, one per line. You can also import the subscriptions CSV from Google Takeout. This does not change your subscriptions on YouTube.</Text>
  <form onSubmit={async e=>{e.preventDefault();const result=await a.run<{message:string}>('/api/youtube/channels',{channels},'Channel checks queued.');if(result){setChannels('');setCsvFile(null);csvReset.current?.();setRevision(v=>v+1);}}}><SettingsStack gap="md">
   <Field label="Channels"><TextArea name="channels" rows={5} maxLength={500000} value={channels} onChange={e=>setChannels(e.target.value)} required/></Field>
   <FileInput inputWrapperOrder={['label','input','description','error']} label="YouTube CSV" aria-label="YouTube CSV" name="channels_csv" accept=".csv,text/csv" placeholder="Choose CSV file" description="Choose a CSV smaller than 500 KB, or paste channels above." value={csvFile} resetRef={csvReset} disabled={a.busy} clearable clearButtonProps={{ 'aria-label': 'Clear CSV file' }} onChange={async file=>{setCsvFile(file);if(!file)return;if(file.size>500000){setCsvFile(null);csvReset.current?.();setError('Choose a CSV smaller than 500 KB.');return;}try{setChannels(await file.text());setError('');}catch{setCsvFile(null);csvReset.current?.();setError('The CSV could not be read. Choose it again or paste channel links.');}}}/>
   <Actions gap="sm" wrap="wrap"><Button type="submit" disabled={a.busy||!channels.trim()}>Add channels</Button></Actions>
  </SettingsStack></form>
  <Notice text={a.error||error} error/><Notice text={a.notice}/>
  {queue&&<Group title="Channel checks"><Row title="Waiting"><Text size="sm">{queue.queued}</Text></Row>{queue.items.map(item=><Row key={item.url} title={item.url.replace('https://www.youtube.com/','')} detail={item.message}><Text size="sm">{item.state==='complete'?'Added':item.state==='failed'?'Retry pending':'Waiting'}</Text></Row>)}</Group>}
 </SettingsStack></PanelButton>;
}
