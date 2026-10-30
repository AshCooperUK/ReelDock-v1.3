import { Stack as SettingsStack, Group as SettingsActions, SimpleGrid, Text, Title } from '@mantine/core';
import { useState, type ReactNode } from 'react';
import { TextInput } from '../../TextField';
import { Button } from '../../components/ui';
import { Field, Notice, Toggle, useAction } from '../../SettingsUI';
export type XtreamLogin={id:string;name:string;state:string;message:string;server:string;username:string;password_saved:boolean;enabled:boolean;hours:number};
export function XtreamForm({csrf,login,onDone,connectionChoice}:{csrf:string;login:XtreamLogin|null;onDone:()=>void;connectionChoice?:ReactNode}){
 const[name,setName]=useState(login?.name||''),[server,setServer]=useState(login?.server||''),[username,setUsername]=useState(login?.username||''),[password,setPassword]=useState(''),[enabled,setEnabled]=useState(login?login.enabled:true),a=useAction(csrf),[status,setStatus]=useState('');
 const body={id:login?.id,name,server,username,password,enabled};
 async function test(){setStatus('');const result=await a.run<{message:string}>('/api/xtream',{...body,action:'test'},'');if(result)setStatus(result.message);}
 return <SettingsStack gap="lg"><SettingsStack gap={4}><Title order={2} size="h3">{login?'Edit Xtream login':connectionChoice?'Add connection':'Add Xtream login'}</Title><Text c="dimmed">Server and account.</Text></SettingsStack>{connectionChoice}<form onSubmit={async e=>{e.preventDefault();if(await a.run('/api/xtream',{...body,action:'save'},login?'Saved.':'Saved. The first import has been queued.'))onDone();}}><SettingsStack gap="md"><fieldset style={{ border: 0, margin: 0, padding: 0, minWidth: 0 }} disabled={a.busy}><SettingsStack gap="md">
  <Field label="Name"><TextInput required maxLength={100} value={name} onChange={e=>setName(e.target.value)}/></Field>
  <Field label="Server URL"><TextInput type="url" autoComplete="off" spellCheck={false} required placeholder="http://provider.example:8080" value={server} onChange={e=>setServer(e.target.value)}/></Field>
  <SimpleGrid cols={{base:1,sm:2}} spacing="md"><Field label="Username"><TextInput required autoComplete="off" spellCheck={false} maxLength={200} value={username} onChange={e=>setUsername(e.target.value)}/></Field>
  <Field label="Password"><TextInput type="password" autoComplete="new-password" required={!login} maxLength={200} placeholder={login?'Leave blank to keep saved password':''} value={password} onChange={e=>setPassword(e.target.value)}/></Field>
  </SimpleGrid><Toggle title="Enabled" detail="Imports films, series and live channels on the refresh interval in this connection’s Import settings (24 hours for new logins)." value={enabled} onChange={setEnabled}/>
  <SettingsActions gap="sm" wrap="wrap"><Button type="button" variant="secondary" onClick={()=>void test()}>Test connection</Button><Button type="submit">{login?'Save':'Add login'}</Button></SettingsActions>
 </SettingsStack></fieldset></SettingsStack></form><Notice text={status}/><Notice text={a.error} error/></SettingsStack>;
}
