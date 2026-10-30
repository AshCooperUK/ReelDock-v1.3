import { Disclosure as PanelButton } from '../../SettingsUI';
import { Text as PresentationText } from '@mantine/core';
import { SelectField as NativeSelect } from '../../components/SelectField';
import { Stack as SettingsStack } from '@mantine/core';

import { useState } from 'react';
import { Field, Group, Loading, Notice, Row, Toggle, useAction, useSettings } from '../../SettingsUI';
import { ConnectionPanel } from './ConnectionPanel';
type Emby={url:string;user:string;root:string;key_saved:boolean;auto_refresh:boolean;watched:boolean;favourites:boolean;status:string;watched_status:string;cookie_check:string;favourites_status:string;refresh_status:string;library_status:string;yt_dlp_version:string;users:{id:string;name:string}[];attempts:{id:string;title:string;state:string;message:string}[]};
export default function EmbySettings({csrf}:{csrf:string}) {
 const r=useSettings<Emby>('/api/emby'),a=useAction(csrf),[key,setKey]=useState('');const d=r.data;
 async function preference(key:string,value:boolean|string){if(!d)return;const result=await a.run<Emby>('/api/emby',{action:'preferences',preferences:{[key]:value}});if(result)r.setData(current=>current?{...current,user:result.user,root:result.root,watched:result.watched,favourites:result.favourites,auto_refresh:result.auto_refresh}:result);}
 if(!d)return <Loading error={r.error} retry={r.reload}/>;
 return <SettingsStack gap="lg">
  <ConnectionPanel server="Emby" configured={d.key_saved} status={d.status} url={d.url} onUrl={value=>r.setData({...d,url:value})} credential={key} onCredential={setKey} credentialName="key" credentialSaved={d.key_saved} busy={a.busy} onConnect={async()=>{const result=await a.run<Emby>('/api/emby',{action:'save',url:d.url,user:d.user,root:d.root,auto_refresh:d.auto_refresh,watched:d.watched,favourites:d.favourites,key});if(result){r.setData(result);setKey('');}}}/>
  <Group title="Emby preferences">
   <PresentationText size="sm" c="dimmed">Changes below save automatically.{!d.key_saved?' Connect to Emby to enable these preferences.':''}</PresentationText>
   {d.users.length?<Field label="Emby user"><NativeSelect disabled={a.busy||!d.key_saved} value={d.user} onChange={e=>void preference('user',e.target.value)}><option value="">Choose user</option>{d.users.map(user=><option key={user.id} value={user.id}>{user.name}</option>)}</NativeSelect></Field>:<Row title="Emby user"><PresentationText size="sm" c="dimmed">{d.key_saved?'No users are available from this server.':'Available after connecting.'}</PresentationText></Row>}
   <Toggle title="Sync favourites" value={d.favourites} disabled={a.busy||!d.key_saved} onChange={v=>void preference('favourites',v)}/>
   <Toggle title="Automatic library scans" detail="Emby scans can use significant NAS resources. Leave off to manage scans in Emby." value={d.auto_refresh} disabled={a.busy||!d.key_saved} onChange={v=>void preference('auto_refresh',v)}/>
  </Group>
  <PanelButton title="Connection details"><Row title="Library scans"><PresentationText component="span" size="sm">{d.refresh_status}</PresentationText></Row><Row title="Favourites"><PresentationText component="span" size="sm">{d.favourites_status}</PresentationText></Row></PanelButton>
  <Notice text={a.error} error/><Notice text={a.busy?'Saving…':a.notice}/>
 </SettingsStack>;
}
