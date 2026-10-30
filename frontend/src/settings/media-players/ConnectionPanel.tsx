import { Badge, Box, Group, Stack, Text } from '@mantine/core';
import type { ReactNode } from 'react';
import { Panel } from '../../components/layout';
import { Button } from '../../components/ui';
import { Field } from '../../SettingsUI';
import { TextInput } from '../../TextField';

export function ConnectionStatus({server,configured}:{server:string;configured:boolean}) {
 return <Badge variant="light" color={configured?'teal':'gray'} aria-label={server+' connection status'} data-connection-status="">{configured?'Configured':'Not connected'}</Badge>;
}

/** All media servers share this form; callers retain their transport and state. */
export function ConnectionPanel({server,configured,status,url,onUrl,credential,onCredential,credentialLabel='API key',credentialName='secret',credentialSaved,busy=false,username,onConnect,onDisconnect,note}:{
 server:string;configured:boolean;status?:string;url:string;onUrl:(value:string)=>void;
 credential:string;onCredential:(value:string)=>void;credentialLabel?:string;credentialName?:string;credentialSaved:boolean;
 busy?:boolean;username?:{value:string;onChange:(value:string)=>void};onConnect:()=>void|Promise<unknown>;onDisconnect?:()=>void|Promise<unknown>;note?:ReactNode;
}) {
 const normal=String(status||'').toLowerCase().replace(/[^a-z0-9]+/g,'');
 const detail=status&&!['','connected','configured','notconnected','notconfigured','disconnected'].includes(normal)?status:'';
 return <Box component="section" aria-label={server+' connection'} data-server-connection={server}>
  <Panel title="Connection" actions={<ConnectionStatus server={server} configured={configured}/>}>
   <form onSubmit={event=>{event.preventDefault();if(!busy&&url)void onConnect();}}>
    <Stack gap="md"><Box component="fieldset" disabled={busy} m={0} p={0} style={{border:0,minWidth:0}}><Stack gap="md">
     <Field label="Server URL" help="Enter the address reachable from ReelDock, including its port and any base path."><TextInput aria-label={server+' server URL'} name="url" type="url" required disabled={busy} value={url} onChange={event=>onUrl(event.target.value)}/></Field>
     {username&&<Field label="Username"><TextInput aria-label={server+' username'} name="username" autoComplete="off" disabled={busy} value={username.value} onChange={event=>username.onChange(event.target.value)}/></Field>}
     <Field label={credentialLabel} help="Stored privately on this server. Leave blank to keep the saved credential for this connection."><TextInput aria-label={server+' credential'} name={credentialName} type="password" autoComplete="new-password" disabled={busy} value={credential} onChange={event=>onCredential(event.target.value)} placeholder={credentialSaved?'Saved credential':''}/></Field>
     <Group gap="sm" wrap="wrap"><Button type="submit" disabled={busy||!url}>Connect</Button>{onDisconnect&&<Button type="button" variant="secondary" disabled={busy} onClick={()=>void onDisconnect()}>Disconnect</Button>}</Group>
    </Stack></Box></Stack>
   </form>
   {note&&<Text size="sm" c="dimmed">{note}</Text>}
   {detail&&<Text size="sm" c="dimmed" role="status">{detail}</Text>}
  </Panel>
 </Box>;
}
