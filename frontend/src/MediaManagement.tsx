import surfaces from './components/MediaSurfaces.module.css';
import { Badge, Box, Card, Divider, Group, Paper, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { SelectField as NativeSelect } from './components/SelectField';
import type { LocalChange } from './viewMedia/localRename';
import { NotificationMessage } from './notifications';
import { useEffect, useState, type ReactNode } from 'react';
import { CheckCircle2, Clock, Download, Search, Wrench, AlertCircle } from 'lucide-react';
import { api } from './api';
import type { DiscoverItem } from './Discover';
import { Button } from './components/ui';
import { ConfirmInline, SheetBack } from './components/DetailSheet';
import ChannelIdentity from './ChannelIdentity';
import { Field, Toggle } from './SettingsUI';
import { TextInput } from './TextField';
import { actionsFor, actionLabel, actionEnabled, actionDisabledReason, type ActionId, type PopupContext } from './popupActions';
type Entry={key:string;kind:string;title:string;series:string;season:number|null;number:number|null;date:string;dateLabel:string;status:string;monitored:boolean;automatic:boolean;hold:boolean;removed:boolean;quality:string;fallback:boolean;file:boolean;metadataReady:boolean;filename:string;bytes:number;fileQuality:string;active:boolean;message:string;channelId:string;channel:string;image:string};
type SeriesSettings={quality:string;fallback:boolean;monitoring:string};
type Data={items:Entry[];seriesSettings?:SeriesSettings};
type Release={id:string;title:string;quality:string;size:number;source:string;reason:string};
const statusIcon=(row:Entry)=>row.file?<CheckCircle2 size={16}/>:row.status==='Downloading'?<Download size={16}/>:row.status==='Searching'?<Search size={16}/>:['Processing','Extracting','Repairing','Assembling'].includes(row.status)?<Wrench size={16}/>:row.status==='Needs attention'?<AlertCircle size={16}/>:<Clock size={16}/>;
const size=(bytes:number)=>bytes?(bytes/1024**3).toFixed(2)+' GiB':'Size unknown';
const qualityRank=(quality:string)=>{const found=String(quality||'').match(/2160|1080|720|480/);return found?({'480':0,'720':1,'1080':2,'2160':3}as Record<string,number>)[found[0]]??-1:-1;};
const renameSeed=(row:Entry)=>row.kind==='movie'?(row.filename||'').replace(/\.[^.]+$/,'')||row.title:row.title;
const entryContext=(row:Entry):PopupContext=>({
 kind: row.kind==='movie'||row.kind==='show'||row.kind==='episode'||row.kind==='video'?row.kind:'unknown',
 file:row.file,active:row.active,hold:row.hold,removed:row.removed,
 automatic:row.automatic,metadataReady:row.metadataReady,
});
// Entry action rows resolve through the shared popupActions schema — labels,
// order and visibility rules are identical to every other media surface.
// Rendered inline (not as a component) so tests can walk the element tree.
function entryButtons(row:Entry,busy:boolean,run:(id:ActionId)=>void) {
 const ctx=entryContext(row);
 const specs=actionsFor(ctx,'entry');
 const render=(spec:(typeof specs)[number])=><Button key={spec.id} variant={spec.variant||'secondary'} disabled={busy||!actionEnabled(spec,ctx)} title={actionDisabledReason(spec,ctx)} onClick={()=>run(spec.id)}>{actionLabel(spec,ctx)}</Button>;
 const main=specs.filter(spec=>spec.group!=='danger'),danger=specs.filter(spec=>spec.group==='danger');
 return <Stack gap="sm">
  <Group gap="sm">{main.map(render)}{row.removed&&<span>{row.file?'Saved file retained':'No saved file'}</span>}</Group>
  {!!danger.length&&<Group gap="sm" justify="flex-end">{danger.map(render)}</Group>}
 </Stack>;
}
// Sub-flows render as in-sheet panes — never a nested dialog. Each pane gets a
// quiet "← Back to managed media" link; the destructive confirm is ConfirmInline.
function Pane({title,subtitle,back,children,footer}:{title:ReactNode;subtitle?:ReactNode;back:()=>void;children:ReactNode;footer?:ReactNode}) {
 return <Stack component="section" gap="md">
  <SheetBack label="managed media" onBack={back}/>
  <header><Title order={3}>{title}</Title>{subtitle&&<Text c="dimmed" size="sm">{subtitle}</Text>}</header>
  {children}
  {footer&&<Group gap="sm">{footer}</Group>}
 </Stack>;
}
export default function MediaManagement({csrf,item,manageKey,episode,localId,onOpenTab,filesOnly=false}:{csrf:string;item?:DiscoverItem;manageKey?:string;episode?:{season:number;number:number};localId?:string;onOpenTab?:(tab:string,row?:Entry)=>void;filesOnly?:boolean}) {
 const [rows,setRows]=useState<Entry[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState(''),[loaded,setLoaded]=useState(false),[notice,setNotice]=useState('');
 const [season,setSeason]=useState('all'),[filter,setFilter]=useState('current');
 const [releases,setReleases]=useState<{row:Entry;items:Release[];message:string;replace:boolean}|null>(null),[confirm,setConfirm]=useState<{row:Entry;action:'remove'|'delete';folder?:boolean}|null>(null);
 const [rename,setRename]=useState<{row:Entry;value:string}|null>(null);
 const [history,setHistory]=useState<{title:string;items:{message:string;created:number}[]}|null>(null);
 const [settings,setSettings]=useState<string|null>(null),[seriesSettings,setSeriesSettings]=useState<SeriesSettings>();
 const [localKey,setLocalKey]=useState('');
 const selected=rows.find(r=>r.key===settings);
 const scopeKey=manageKey||localKey;
 const scope=scopeKey?'key='+encodeURIComponent(scopeKey):item?'parent='+encodeURIComponent(item.id):'';
 const url='/api/media-management'+(scope?'?'+scope:'');
 // Episode enrolment needs a verified show identity and numbering; other rows
 // fall back to a key-scoped read of the record that already exists.
 const canEnrol=!!item&&(!episode||!!(item.externalId&&Number.isInteger(episode.season)&&Number.isInteger(episode.number)));
 async function refresh(){const data=await api<Data>(url);setRows(data.items);setSeriesSettings(data.seriesSettings);}
 useEffect(()=>{let disposed=false;const c=new AbortController();let timer:ReturnType<typeof setTimeout>;
  async function load(first=false){
   try{
    let endpoint=url;
    if(!filesOnly&&localId&&!scopeKey){const enrolled=await api<{key:string}>('/api/media-management',c.signal,{action:'enrol-local',value:{id:localId}},csrf);if(disposed||c.signal.aborted)return;setLocalKey(enrolled.key);endpoint='/api/media-management?key='+encodeURIComponent(enrolled.key);}
    else if(first&&!filesOnly&&canEnrol)await api('/api/media-management',undefined,{action:'enrol',item,...(episode?{episode}:{})},csrf);
    const data=await api<Data>(endpoint,c.signal);if(!disposed){setRows(data.items);setSeriesSettings(data.seriesSettings);setLoaded(true);if(first)setError('');}
   }catch(e){if(!disposed)setError(e instanceof Error?e.message:'Could not load media.');}finally{if(!disposed)timer=setTimeout(()=>void load(),10000);}
  }
  void load(true);return()=>{disposed=true;c.abort();clearTimeout(timer);};
 },[item?.id,csrf,url,filesOnly]);
 async function act(action:string,row?:Entry,value?:unknown,confirmed=false){setBusy(true);setNotice('Saving…');setError('');try{const data=await api<{message?:string;item?:{localChanges?:LocalChange[]}}>(url,undefined,{action,key:row?.key,value,confirm:confirmed},csrf);setConfirm(null);setReleases(null);setRename(null);setNotice(typeof data?.message==='string'&&data.message?data.message:'Saved');window.dispatchEvent(new CustomEvent('reeldock-library-changed',{detail:{localChanges:data?.item?.localChanges}}));}catch(e){setNotice('');setError(e instanceof Error?e.message:'Could not save.');}finally{setBusy(false);void refresh().catch(()=>{});}}
 async function compare(row:Entry,replace=false){setBusy(true);setError('');try{const data=await api<{items:Release[];message:string;fileQuality?:string}>(url,undefined,{action:'releases',key:row.key},csrf);const floor=qualityRank(data.fileQuality||row.fileQuality);setReleases({row,items:replace&&floor>=0?data.items.filter(r=>{const rank=qualityRank(r.quality);return rank<0||rank>floor;}):data.items,message:data.message,replace});}catch(e){setError(e instanceof Error?e.message:'Could not search.');}finally{setBusy(false);}}
 async function showHistory(row:Entry){setBusy(true);try{const data=await api<{items:{message:string;created:number}[]}>(url,undefined,{action:'history',key:row.key},csrf);setHistory({title:row.title,...data});}catch(e){setError(String(e));}finally{setBusy(false);}}
 async function monitorSeason(value:boolean){setBusy(true);setError('');try{await api(url,undefined,{action:'season',parent:item?.id,season:Number(season),value},csrf);await refresh();}catch(e){setError(String(e));}finally{setBusy(false);}}
 async function saveSeries(field:string,value:unknown){setBusy(true);setNotice('Saving…');setError('');try{await api(url,undefined,{action:'series-settings',parent:item?.id,field,value},csrf);await refresh();setNotice('Saved');}catch(e){setNotice('');setError(e instanceof Error?e.message:'Could not save settings.');}finally{setBusy(false);}}
 const runEntry=(row:Entry)=>(id:ActionId)=>{
  switch(id){
   case 'settings':setSettings(row.key);break;
   case 'correctTitle':setRename({row,value:renameSeed(row)});break;
   case 'repairMetadata':void act('repair-metadata',row);break;
   case 'findBetterQuality':void compare(row,true);break;
   case 'compareReleases':void compare(row);break;
   case 'searchNow':case 'download':void act('search',row);break;
   case 'cancelAcquisition':void act('cancel',row);break;
   case 'resumeAcquisition':void act('resume',row);break;
   case 'history':if(onOpenTab)onOpenTab('history',row);else void showHistory(row);break;
   case 'removeEntry':setConfirm({row,action:'remove'});break;
   case 'deleteFile':setConfirm({row,action:'delete'});break;
   case 'restoreEntry':void act('restore',row);break;
   default:break;
  }
 };
 const listedRows=filesOnly?rows.filter(r=>r.file===true):rows;
 const seasons=[...new Set(listedRows.filter(r=>r.kind==='episode').map(r=>r.season))].sort((a,b)=>a===0?1:b===0?-1:(b??-1)-(a??-1));
 const visible=listedRows.filter(r=>(filter==='removed'?r.removed:!r.removed)&&(season==='all'||String(r.season)===season)).sort((a,b)=>a.series.localeCompare(b.series)||(b.season===0?-1:b.season??-2)-(a.season===0?-1:a.season??-2)||(a.number??0)-(b.number??0));
 const back=()=>{if(busy)return;setSettings(null);setConfirm(null);setRename(null);setReleases(null);setHistory(null);};
 return <Stack component="section" gap="md" aria-label="Media management">
  {busy&&notice?<Text size="sm" role="status">{notice}</Text>:<NotificationMessage kind="success" message={notice}/>}{error&&<NotificationMessage kind="error" message={error}/>}{!loaded&&!error&&!settings&&!confirm&&!rename&&!releases&&!history&&<Text size="sm" role="status">Loading media management…</Text>}
  {settings&&<Pane back={back} title={settings==='series'?'Series settings':selected?.kind==='movie'?'Movie settings':selected?.kind==='episode'?'Episode settings':'Video settings'} subtitle={settings==='series'?item?.title:selected?.title}>
   {settings==='series'&&seriesSettings&&<>
    <Group gap="sm" align="center"><NativeSelect aria-label="Series monitoring" disabled={busy} value={seriesSettings.monitoring} onChange={e=>void saveSeries('monitoring',e.target.value)}><option value="inherit">Existing monitoring policy</option><option value="all">Monitor all seasons</option><option value="off">Monitoring off</option></NativeSelect><NativeSelect aria-label="Series preferred quality" disabled={busy} value={seriesSettings.quality} onChange={e=>void saveSeries('quality',e.target.value)}>{['720','1080','2160'].map(q=><option key={q} value={q}>{q}p</option>)}</NativeSelect></Group>
    <Toggle title="Allow lower quality" value={seriesSettings.fallback} disabled={busy} onChange={value=>void saveSeries('fallback',value)}/>
    <Text size="sm">Defaults apply to episodes without overrides. Monitoring off pauses automatic acquisition for the whole series. Specials are monitored individually.</Text>
    <Group gap="sm" align="center"><NativeSelect aria-label="Season to monitor" value={season} onChange={e=>setSeason(e.target.value)}><option value="all">Choose a season</option>{seasons.map(n=><option key={String(n)} value={String(n)}>{n===0?'Specials':n===null?'Unnumbered':'Season '+n}</option>)}</NativeSelect><Button variant="secondary" disabled={busy||season==='all'} onClick={()=>void monitorSeason(true)}>Monitor season</Button><Button variant="secondary" disabled={busy||season==='all'} onClick={()=>void monitorSeason(false)}>Unmonitor season</Button></Group>
   </>}
   {selected&&<>
    <Group gap="sm" align="center"><NativeSelect aria-label={'Preferred quality for '+selected.title} disabled={busy||selected.active} value={selected.quality} onChange={e=>void act('quality',selected,e.target.value)}>{['720','1080','2160'].map(q=><option key={q} value={q}>{q}p</option>)}</NativeSelect></Group>
    <Toggle title="Monitor" value={selected.monitored} disabled={busy||!selected.automatic||selected.hold} onChange={value=>void act('monitor',selected,value)}/>
    <Toggle title="Allow lower quality" value={selected.fallback} disabled={busy||selected.active} onChange={value=>void act('fallback',selected,value)}/>
    {selected.kind==='episode'&&<Group gap="sm" align="center"><Button variant="secondary" disabled={busy||selected.active} onClick={()=>void act('inherit',selected)}>Use series settings</Button></Group>}
   </>}
  </Pane>}
  {confirm&&<Pane back={back} title={confirm.action==='delete'?'Delete saved video?':'Remove from management?'} subtitle={confirm.row.title}>
   <ConfirmInline confirm={confirm.action==='delete'?'Delete video file':'Remove entry'} busy={busy}
    onConfirm={()=>void act(confirm.action,confirm.row,confirm.action==='delete'?{folder:!!confirm.folder}:undefined,true)} onCancel={()=>{if(!busy)setConfirm(null);}}>
    <Text size="sm">{confirm.action==='delete'?`Permanently delete ${confirm.row.filename} (${size(confirm.row.bytes)}); it cannot be restored. ${confirm.folder?'The folder, its artwork and metadata are removed too.':'Artwork and metadata stay.'} Automatic acquisition will be paused.`:'Keep the saved file, stop monitoring and hide this entry. You can restore it from Removed entries.'}</Text>
    {confirm.action==='delete'&&confirm.row.kind==='movie'&&<Toggle title="Also remove the folder if it only has metadata and artwork left" detail="Removing the folder is only available for movie folders; episode and video folders are shared and stay in place." value={!!confirm.folder} disabled={busy} onChange={value=>setConfirm({...confirm,folder:value})}/>}
   </ConfirmInline>
  </Pane>}
  {rename&&<Pane back={back} title="Correct title" subtitle={rename.row.title}
   footer={<><Button variant="secondary" disabled={busy} onClick={()=>setRename(null)}>Keep it</Button><Button disabled={busy||!rename.value.trim()} onClick={()=>void act('rename',rename.row,rename.value.trim())}>{busy?'Saving…':'Save title'}</Button></>}>
   <Field label={rename.row.kind==='movie'?'Movie title':'Episode title'}><TextInput required maxLength={300} value={rename.value} aria-label={'New title for '+rename.row.title} onChange={e=>setRename({...rename,value:e.target.value})}/></Field>
   {rename.row.kind==='episode'?<Text size="sm">Renaming only changes this episode’s saved file and its metadata; the shared show folder keeps its name.</Text>:<Text size="sm">Renames the saved file and its movie folder in place; metadata and artwork move with it.</Text>}
  </Pane>}
  {releases&&<Pane back={back} title={releases.replace?'Find better quality':'Compare releases'} subtitle={<>{releases.row.title} · {releases.replace?'File quality: '+(releases.row.fileQuality||'Not verified'):releases.row.quality+'p'+(releases.row.fallback?' or lower':'')}</>}>
   {releases.message&&<Text size="sm">{releases.message}</Text>}{releases.replace&&!releases.items.length&&!releases.message&&<Text size="sm">No higher-quality releases found.</Text>}{releases.items.map(r=><Card component="article" data-media-surface="tile" className={[surfaces.surface,surfaces.tile].filter(Boolean).join(' ')} withBorder p="md" radius="md" key={r.id}><Stack gap="sm"><Title order={3} size="h4">{r.title}</Title><Text size="sm">{r.quality} · {size(r.size)} · {r.source}</Text><Text size="sm">{r.reason||'Eligible · file contents are checked before download'}</Text><Button disabled={busy||!!r.reason} onClick={()=>void act(releases.replace?'replace':'search',releases.row,r.id)}>{releases.replace?'Replace with this release':'Download this release'}</Button></Stack></Card>)}
  </Pane>}
  {history&&<Pane back={back} title="History" subtitle={history.title}>{history.items.length?history.items.map((h,i)=><Text size="sm" key={i}>{new Date(h.created*1000).toLocaleString()} · {h.message}</Text>):<Text size="sm">No recorded actions yet.</Text>}</Pane>}
  {!settings&&!confirm&&!rename&&!releases&&!history&&<>
   <Group gap="sm" align="center"><NativeSelect aria-label="Managed media filter" value={filter} onChange={e=>setFilter(e.target.value)}><option value="current">{filesOnly?'Downloaded files':'Managed media'}</option><option value="removed">{filesOnly?'Removed files':'Removed entries'}</option></NativeSelect>
    {!!seasons.length&&<NativeSelect aria-label="Season" value={season} onChange={e=>setSeason(e.target.value)}><option value="all">All seasons & specials</option>{seasons.map(n=><option key={String(n)} value={String(n)}>{n===0?'Specials':n===null?'Unnumbered':'Season '+n}</option>)}</NativeSelect>}
    {item?.kind==='show'&&seriesSettings&&<Button variant="secondary" onClick={()=>setSettings('series')}>Series settings</Button>}
   </Group>
   {loaded&&!visible.length&&<Text size="sm">{filesOnly?(item?.kind==='show'?'No downloaded episodes in your library.':'No downloaded files in your library.'):'No entries here yet. Open a title’s management controls to add it.'}</Text>}
   {visible.map((row,index)=><Card component="article" data-media-surface="tile" className={[surfaces.surface,surfaces.tile].filter(Boolean).join(' ')} withBorder p="md" radius="md" key={row.key}><Stack gap="md">
    {row.kind==='episode'&&(index===0||visible[index-1].season!==row.season||visible[index-1].series!==row.series)&&<Title order={3} size="h4">{row.season===0?'Specials':'Season '+(row.season??'unknown')}</Title>}
    <Group justify="space-between" align="flex-start"><Stack gap={4} style={{flex:1,minWidth:0}}>{row.kind==='episode'&&<Text size="xs" c="dimmed">{row.series} · {row.season===0?'Special':`S${String(row.season??'?').padStart(2,'0')}`} E{row.number??'?'}</Text>}<Title order={3} size="h4">{row.title}</Title>{row.channel&&<ChannelIdentity id={row.channelId} name={row.channel}/>}</Stack><Badge variant="light" leftSection={statusIcon(row)}>{row.status}</Badge></Group>
    <Text size="sm">{row.dateLabel}: {row.date||'To be announced'}{row.file?' · '+size(row.bytes)+' · File quality: '+(row.fileQuality||'Not verified'):''}</Text>
    {row.message&&<Text size="sm" role="status">{row.message}</Text>}
    {entryButtons(row,busy,runEntry(row))}
   </Stack></Card>)}
  </>}
 </Stack>;
}

// Read-only history surface for a DetailSheet "History" tab. It lists the
// managed entries that already exist for the title — it never enrols — and
// loads each entry's recorded actions on demand (automatically when there is
// only one entry, or when the sheet deep-links a specific entry).
export function MediaTitleHistory({csrf,item,focus}:{csrf:string;item:DiscoverItem;focus?:string}) {
 const url='/api/media-management?parent='+encodeURIComponent(item.id);
 const [rows,setRows]=useState<Entry[]>([]),[loaded,setLoaded]=useState(false),[error,setError]=useState('');
 const [history,setHistory]=useState<Record<string,{busy?:boolean;error?:string;items?:{message:string;created:number}[]}>>({});
 useEffect(()=>{const c=new AbortController();setLoaded(false);setError('');api<Data>(url,c.signal).then(data=>{if(!c.signal.aborted){setRows(data.items.filter(r=>!r.removed));setLoaded(true);}}).catch(e=>{if(!c.signal.aborted)setError(e instanceof Error?e.message:'Could not load media.');});return()=>c.abort();},[url]);
 async function load(row:Entry){setHistory(old=>({...old,[row.key]:{busy:true}}));try{const data=await api<{items:{message:string;created:number}[]}>(url,undefined,{action:'history',key:row.key},csrf);setHistory(old=>({...old,[row.key]:{items:data.items}}));}catch(e){setHistory(old=>({...old,[row.key]:{error:e instanceof Error?e.message:'Could not load history.'}}));}}
 useEffect(()=>{if(rows.length===1||focus)for(const row of rows)if(rows.length===1||row.key===focus)void load(row);},[rows,focus]);
 return <Stack gap="md" aria-label="Download history">
  {error&&<NotificationMessage kind="error" message={error}/>}{!loaded&&!error&&<Text size="sm" role="status">Loading history…</Text>}
  {loaded&&!rows.length&&<Text size="sm">No recorded actions yet. History appears here once this title is managed.</Text>}
  {rows.map(row=>{const entry=history[row.key];
   return <Card component="article" data-media-surface="tile" className={[surfaces.surface,surfaces.tile].filter(Boolean).join(' ')} withBorder p="md" radius="md" key={row.key}><Stack gap="md">
    <Group justify="space-between" align="flex-start"><Stack gap={4} style={{flex:1,minWidth:0}}>{row.kind==='episode'&&<Text size="xs" c="dimmed">{row.series} · {row.season===0?'Special':`S${String(row.season??'?').padStart(2,'0')}`} E{row.number??'?'}</Text>}<Title order={3} size="h4">{row.title}</Title></Stack>{!entry&&<Button variant="secondary" onClick={()=>void load(row)}>Load history</Button>}</Group>
    {entry?.busy&&<Text size="sm" role="status">Loading history…</Text>}
    {entry?.error&&<NotificationMessage kind="error" message={entry.error}/>}
    {entry?.items&&(entry.items.length?entry.items.map((h,i)=><Text size="sm" key={i}>{new Date(h.created*1000).toLocaleString()} · {h.message}</Text>):<Text size="sm">No recorded actions yet.</Text>)}
   </Stack></Card>;})}
 </Stack>;
}
