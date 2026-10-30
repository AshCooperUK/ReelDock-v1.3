import { SelectField } from './components/SelectField';
import { Box, Grid, Group as ActionGroup, Loader, NavLink, Paper, Stack, Text, Title } from '@mantine/core';
import { useMediaQuery } from '@mantine/hooks';
import { lazy, Suspense, useEffect, useState } from 'react';
import { ArrowLeft, CloudDownload, FileDown, FolderOpen, LayoutGrid, ListVideo, Monitor, ShieldCheck, SlidersHorizontal, Tags, Youtube } from 'lucide-react';
import ResetReelDock from './ResetReelDock';
import { TextInput } from './TextField';
import { Button } from './components/ui';
import { Field, Group, Notice, Row, useAction } from './SettingsUI';

const YouTubeSettings = lazy(() => import('./settings/youtube/YouTubeSettings'));
const VodSettings = lazy(() => import('./settings/m3u/VodSettings'));
const DownloadControls = lazy(() => import('./settings/DownloadControls'));
const MetadataSettings = lazy(() => import('./settings/MetadataSettings'));
const MediaSettings = lazy(() => import('./MediaSettings'));
const AutoDownloadSettings = lazy(() => import('./settings/auto-download/AutoDownloadSettings'));
const TorrentSettings = lazy(() => import('./settings/torrent/TorrentSettings'));
const NzbSettings = lazy(() => import('./settings/nzb/NzbSettings'));
const VpnSettings = lazy(() => import('./settings/VpnSettings'));
const SettingsAppearance = lazy(() => import('./settings/SettingsAppearance'));
const sections = [['General',SlidersHorizontal],['YouTube',Youtube],['VOD Settings',ListVideo],['NZB Settings',FileDown],['Torrent Settings',CloudDownload],['Auto-Download',FolderOpen],['Metadata',Tags],['VPN',ShieldCheck],['Media Players',Monitor],['Appearance',LayoutGrid],['Account & security',ShieldCheck]] as const;
const labels:Record<string,string>={'VOD Settings':'VOD connections','NZB Settings':'Usenet & NZB','Torrent Settings':'Torrents','Auto-Download':'Watched folders','Media Players':'Media servers'};
const labelFor=(name:string)=>labels[name]||name;
export function canonicalSettingsSection(initial:string) {return ['M3U Settings','Xtream'].includes(initial)?'VOD Settings':initial==='Media apps'?'Media Players':['Downloads','Folders'].includes(initial)?'General':sections.some(([name])=>name===initial)?initial:'General';}
function SettingsCategories({section,onSelect}:{section:string;onSelect:(name:string)=>void}) {
  const narrow=useMediaQuery('(max-width: 61.99em)');
  return narrow?<SelectField aria-label="Settings category" value={section} onChange={event=>onSelect(event.currentTarget.value)} data={sections.map(([name])=>({value:name,label:labelFor(name)}))}/>:<Paper withBorder radius="md" p="xs"><Stack component="nav" gap={4} aria-label="Settings categories">{sections.map(([name,Icon])=><NavLink component="button" type="button" key={name} label={labelFor(name)} leftSection={<Icon size={18}/>} active={section===name} aria-current={section===name?'page':undefined} onClick={()=>onSelect(name)} mih={44}/>)}</Stack></Paper>;
}
export default function LiveSettings({open,onClose,initial='General',csrf,appInfo,onSection,cookieFocusRequest=0}:{open:boolean;onClose:(v:boolean)=>void;initial?:string;csrf:string;appInfo?:{version:string;yt_dlp_version:string;worker_online:boolean}|null;onSection?:(name:string)=>void;cookieFocusRequest?:number}) {
  const [section,setSection]=useState(()=>canonicalSettingsSection(initial));
  useEffect(()=>{setSection(canonicalSettingsSection(initial));},[initial]);
  const selectSection=(name:string)=>{setSection(name);onSection?.(name);};
  if(!open)return null;
  return <Stack gap="xl">
    <ActionGroup justify="space-between" align="flex-start"><Stack gap={4}><Title order={1}>Settings</Title><Text c="dimmed">Connections, automation and the way ReelDock organises your media.</Text></Stack><Button variant="ghost" onClick={()=>onClose(false)}><ArrowLeft size={18}/>Back to ReelDock</Button></ActionGroup>
    <Grid gap="xl" align="flex-start"><Grid.Col span={{base:12,md:3}}><SettingsCategories section={section} onSelect={selectSection}/></Grid.Col><Grid.Col span={{base:12,md:9}}>
      <Stack gap="lg" key={section}><Title order={2} size="h3">{labelFor(section)}</Title><Suspense fallback={<ActionGroup role="status"><Loader size="sm"/><Text>Loading settings…</Text></ActionGroup>}>
        {section==='General'&&<><Group title="Installation"><Row title="Application"><Text>ReelDock</Text></Row><Row title="Version"><Text>{appInfo?.version||'Unavailable'}</Text></Row><Row title="yt-dlp"><Text>{appInfo?.yt_dlp_version||'Unavailable'}</Text></Row><Row title="Background worker"><Text>{appInfo?appInfo.worker_online?'Online':'Offline':'Unavailable'}</Text></Row></Group><DownloadControls csrf={csrf}/></>}
        {section==='YouTube'&&<YouTubeSettings csrf={csrf} cookieFocusRequest={cookieFocusRequest}/>}
        {section==='VOD Settings'&&<VodSettings csrf={csrf}/>}
        {section==='NZB Settings'&&<NzbSettings csrf={csrf}/>}
        {section==='Torrent Settings'&&<TorrentSettings csrf={csrf}/>}
        {section==='Auto-Download'&&<AutoDownloadSettings csrf={csrf}/>}
        {section==='Metadata'&&<MetadataSettings csrf={csrf}/>}
        {section==='VPN'&&<VpnSettings csrf={csrf}/>}
        {section==='Media Players'&&<MediaSettings csrf={csrf}/>}
        {section==='Appearance'&&<SettingsAppearance/>}
        {section==='Account & security'&&<AccountSettings csrf={csrf}/>}
      </Suspense></Stack>
    </Grid.Col></Grid>
  </Stack>;
}
function AccountSettings({csrf}:{csrf:string}) {
  const [current,setCurrent]=useState(''),[password,setPassword]=useState(''),[confirm,setConfirm]=useState(''),a=useAction(csrf);
  return <Stack gap="lg"><Group title="Change password"><Box component="form" onSubmit={async event=>{event.preventDefault();const result=await a.run<{url:string}>('/api/account/password',{current,password,confirm});if(result)location.assign(result.url);}}><Stack gap="md">
    <Field label="Current password"><TextInput type="password" autoComplete="current-password" required value={current} onChange={event=>setCurrent(event.target.value)}/></Field>
    <Field label="New password" help="Use at least 12 characters. You’ll sign in again after saving."><TextInput type="password" autoComplete="new-password" required minLength={12} maxLength={256} value={password} onChange={event=>setPassword(event.target.value)}/></Field>
    <Field label="Confirm new password"><TextInput type="password" autoComplete="new-password" required value={confirm} onChange={event=>setConfirm(event.target.value)}/></Field>
    <Notice text={a.error} error/><ActionGroup><Button type="submit" disabled={a.busy}>Change password</Button></ActionGroup>
  </Stack></Box></Group><Group title="Session"><Box component="form" method="post" action="/logout"><input type="hidden" name="csrf" value={csrf}/><Button type="submit" variant="secondary">Sign out</Button></Box></Group><ResetReelDock csrf={csrf}/></Stack>;
}
