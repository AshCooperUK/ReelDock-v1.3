import { Text as PresentationText } from '@mantine/core';
import { SelectField as NativeSelect } from '../../components/SelectField';
import { Stack as SettingsStack, Group as SettingsActions } from '@mantine/core';

import { useEffect, useState } from 'react';
import { Button } from '../../components/ui';
import { Group, Loading, Notice, Row, Toggle, useAction, useSettings } from '../../SettingsUI';
import { useEngineSettings } from '../useEngineSettings';
import { ConnectionPanel } from './ConnectionPanel';
type Player='jellyfin'|'plex'|'kodi';
type Connection={url:string;username:string;user:string;library:string;connected:boolean;secret_saved:boolean;name:string;users:{id:string;name:string}[];libraries:{id:string;name:string}[];status:string;last_sync:string;items:number;watched:number;favourites:number};
export default function ServerSettings({player,csrf}:{player:Player;csrf:string}) {
  const resource=useSettings<Connection>('/api/media-players/'+player),action=useAction(csrf);
  const preferences=useEngineSettings<Record<string,boolean|number>>('media-players',csrf);
  const [draft,setDraft]=useState<{url:string;username:string}|null>(null),[secret,setSecret]=useState('');
  const data=resource.data;
  // Poll read-only status while the panel is open; never replace connection drafts.
  useEffect(()=>{const timer=window.setInterval(()=>resource.reload(),10000);return()=>window.clearInterval(timer);},[player]);
  if(!data)return <Loading error={resource.error} retry={resource.reload}/>;
  if(!preferences.values)return <Loading error={preferences.loadError} retry={preferences.reload}/>;
  const values=preferences.values, fields=draft||{url:data.url,username:data.username};
  const label=player==='jellyfin'?'Jellyfin':player==='plex'?'Plex':'Kodi';
  const busy=action.busy||preferences.busy;
  async function run(body:object,notice='Saved.') {const result=await action.run<Connection>('/api/media-players/'+player,body,notice);if(result)resource.setData(result);return !!result;}
  return <SettingsStack gap="lg"><ConnectionPanel server={label} configured={data.connected} status={data.status} url={fields.url} onUrl={value=>setDraft({...fields,url:value})} credential={secret} onCredential={setSecret} credentialLabel={player==='kodi'?'Password':player==='plex'?'Plex token':'API key'} credentialSaved={data.secret_saved} busy={busy} username={player==='kodi'?{value:fields.username,onChange:value=>setDraft({...fields,username:value})}:undefined} note={player==='kodi'?'Enable HTTP remote control in Kodi. Use its server address without /jsonrpc.':undefined} onConnect={async()=>{if(await run({action:'connect',...fields,secret},'Connection checked and saved.')){setSecret('');setDraft(null);preferences.reload();}}} onDisconnect={data.connected?async()=>{if(await run({action:'disconnect'},'Disconnected.')){setDraft(null);setSecret('');preferences.reload();}}:undefined}/>{data.connected&&<><Group title="Library integration">
    {player==='jellyfin'&&<Row title="User" detail="Library and watched/favourite state are read for this user."><NativeSelect aria-label="Jellyfin user" disabled={busy} value={data.user} onChange={e=>void run({action:'selection',user:e.target.value})}><option value="">Choose user</option>{data.users.map(user=><option key={user.id} value={user.id}>{user.name}</option>)}</NativeSelect></Row>}
    {player==='plex'&&<Row title="Library" detail="Only movie and TV libraries are included. Watched state belongs to the account whose Plex token you supplied."><NativeSelect aria-label="Plex library" disabled={busy} value={data.library} onChange={e=>void run({action:'selection',library:e.target.value})}><option value="">All movie & TV libraries</option>{data.libraries.map(library=><option key={library.id} value={library.id}>{library.name}</option>)}</NativeSelect></Row>}
    <Toggle title="Sync library status" detail="Periodically import movie and episode counts and watched state into ReelDock. Jellyfin also provides favourite state. This does not change playback history on YouTube or another player." value={!!values[player+'_sync']} disabled={busy||(player==='jellyfin'&&!data.user)} onChange={value=>void preferences.save({[player+'_sync']:value})}/>
    <Toggle title="Automatic library scans" detail="Ask this server to scan its video libraries at the chosen interval. Plex uses the selected library; Jellyfin and Kodi scan their video libraries. Server indexing runs independently and can use significant resources." value={!!values[player+'_auto_scan']} disabled={busy} onChange={value=>void preferences.save({[player+'_auto_scan']:value})}/>
    <Row title="Check interval"><NativeSelect aria-label={label+' check interval'} disabled={busy} value={Number(values[player+'_minutes'])} onChange={e=>void preferences.save({[player+'_minutes']:Number(e.target.value)})}>{[5,15,30,60,180,360].map(minutes=><option key={minutes} value={minutes}>{minutes<60?minutes+' minutes':minutes/60+' hour'+(minutes>60?'s':'')}</option>)}</NativeSelect></Row>
  </Group><SettingsActions gap="sm" wrap="wrap"><Button variant="secondary" disabled={busy||(player==='jellyfin'&&!data.user)} onClick={()=>void run({action:'sync'},'Library status sync queued.')}>Sync now</Button><Button variant="secondary" disabled={busy} onClick={()=>void run({action:'scan'},'Library scan queued.')}>Scan library</Button></SettingsActions></>}
  {data.last_sync&&<Group title="Library status"><Row title="Last sync"><PresentationText component="span" size="sm">{new Date(Number(data.last_sync)*1000).toLocaleString()}</PresentationText></Row><Row title="Library items"><PresentationText component="span" size="sm">{data.items}</PresentationText></Row><Row title="Watched"><PresentationText component="span" size="sm">{data.watched}</PresentationText></Row>{player==='jellyfin'&&<Row title="Favourites"><PresentationText component="span" size="sm">{data.favourites}</PresentationText></Row>}</Group>}
  <Notice text={resource.error||action.error||preferences.error} error/><Notice text={busy?'Saving…':action.notice||preferences.notice}/>
  </SettingsStack>;
}
