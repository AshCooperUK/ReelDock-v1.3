import { useEffect, useMemo, useState } from 'react';
import { Badge, Group, Paper, Stack, Text, Title } from '@mantine/core';
import { api } from '../api';
import { Button } from '../components/ui';
import { DetailSheet } from '../components/DetailSheet';
import { MediaArtwork, MediaLengthBadge } from '../components/browse';
import { mediaLength, type MediaDuration } from '../mediaDuration';
import { SelectField } from '../components/SelectField';
import { NotificationMessage } from '../notifications';
import { ServerDownload } from '../M3uDownloads';
import { M3uDetails } from '../M3uLibrary';
import type { MediaCard } from './types';

type Entry=MediaDuration&{source:string;path:string;title:string;kind:string;season:number|null;number:number|null;day?:string;quality?:string;sourceName:string};
type Sources={sources:{id:string;label:string;type:string;entries:Omit<Entry,'sourceName'>[]}[]};
const entryKey=(entry:Entry)=>JSON.stringify([entry.source,entry.path]);
const episodeKey=(entry:Entry)=>entry.season!==null&&entry.number!==null?`${entry.season}:${entry.number}`:entry.day?`date:${entry.day}`:entryKey(entry);
const seasonKey=(entry:Entry)=>entry.season===null?'other':String(entry.season);

/** One work sheet; only the chosen exact playlist file reaches the download API. */
export default function VodGroupDetails({item,csrf,onClose}:{item:MediaCard;csrf:string;onClose:()=>void}){
 const [entries,setEntries]=useState<Entry[]>([]),[busy,setBusy]=useState(true),[error,setError]=useState(''),[revision,setRevision]=useState(0);
 const [tab,setTab]=useState('overview'),[season,setSeason]=useState(''),[episode,setEpisode]=useState(''),[version,setVersion]=useState(''),[file,setFile]=useState<Entry|null>(null);
 const show=item.kind==='show';
 const raw=item.action.type==='vod-group'?item.action.item:null;
 useEffect(()=>{if(!raw)return;const c=new AbortController();setBusy(true);setError('');
  api<Sources>('/api/view-media/download-sources?'+new URLSearchParams({id:raw.id,kind:raw.kind,title:raw.title,year:raw.year||'',externalId:raw.externalId||'',imdbId:raw.imdbId||''}),c.signal)
   .then(data=>{if(!c.signal.aborted)setEntries(data.sources.filter(source=>source.type==='vod').flatMap(source=>source.entries.map(entry=>({...entry,sourceName:source.label}))));})
   .catch(e=>{if(!c.signal.aborted)setError(e instanceof Error?e.message:'Available files could not be checked.');})
   .finally(()=>{if(!c.signal.aborted)setBusy(false);});return()=>c.abort();
 },[raw?.id,revision]);
 const seasons=useMemo(()=>Array.from(new Set(entries.map(seasonKey))).sort((a,b)=>a==='other'?1:b==='other'?-1:Number(a)-Number(b)),[entries]);
 const currentSeason=seasons.includes(season)?season:seasons[0]||'';
 const episodes=useMemo(()=>{const found=new Map<string,Entry>();for(const entry of entries.filter(entry=>seasonKey(entry)===currentSeason))if(!found.has(episodeKey(entry)))found.set(episodeKey(entry),entry);return [...found].sort(([,a],[,b])=>(a.number??Infinity)-(b.number??Infinity)||a.title.localeCompare(b.title));},[entries,currentSeason]);
 const currentEpisode=episodes.some(([key])=>key===episode)?episode:episodes[0]?.[0]||'';
 const versions=entries.filter(entry=>!show||seasonKey(entry)===currentSeason&&episodeKey(entry)===currentEpisode);
 const selected=versions.find(entry=>entryKey(entry)===version)||versions[0];
 if(file)return <M3uDetails source={file.source} item={file} onClose={()=>setFile(null)}/>;
 const choices=<Stack gap="md">{busy?<Text role="status">Checking available files…</Text>:error?<NotificationMessage kind="error" message={error} actions={<Button variant="secondary" onClick={()=>setRevision(n=>n+1)}>Try again</Button>}/>:!entries.length?<Text>No files remain available for this title in the enabled playlists.</Text>:<>
  {show&&<><SelectField aria-label="VOD season" label="Season" value={currentSeason} onChange={event=>{setSeason(event.target.value);setEpisode('');setVersion('');}}>{seasons.map(value=><option key={value} value={value}>{value==='other'?'Dated or unnumbered episodes':value==='0'?'Specials':'Season '+value}</option>)}</SelectField>
   <SelectField aria-label="VOD episode" label="Episode" value={currentEpisode} onChange={event=>{setEpisode(event.target.value);setVersion('');}}>{episodes.map(([key,entry])=><option key={key} value={key}>{entry.number!==null?'Episode '+entry.number+' · ':entry.day?entry.day+' · ':''}{entry.title}</option>)}</SelectField></>}
  <SelectField aria-label="VOD version" label="Version / source" value={selected?entryKey(selected):''} onChange={event=>setVersion(event.target.value)}>{versions.map(entry=><option key={entryKey(entry)} value={entryKey(entry)}>{[entry.sourceName,entry.quality||'Quality not supplied',entry.title,entry.path].filter(Boolean).join(' · ')}</option>)}</SelectField>
  {selected&&<Paper withBorder p="md" radius="md"><Stack gap="sm"><Text fw={600} style={{overflowWrap:'anywhere'}}>{selected.title}</Text><MediaLengthBadge length={mediaLength(selected)}/><Text size="xs" c="dimmed" style={{overflowWrap:'anywhere'}}>{selected.sourceName} · {selected.path}</Text><ServerDownload key={entryKey(selected)} csrf={csrf} source={selected.source} path={selected.path}/><Button variant="secondary" onClick={()=>setFile(selected)}>File details &amp; settings</Button></Stack></Paper>}
 </>}</Stack>;
 return <DetailSheet open onOpenChange={open=>{if(!open)onClose();}} title={item.title} subtitle={[item.year,show?'VOD series':'VOD movie'].filter(Boolean).join(' · ')} media={<MediaArtwork src={item.image} kind={item.kind} fit="contain"/>} banner={item.backdrop||item.image} tab={tab} onTab={setTab}
  actions={<Button variant="secondary" onClick={()=>setTab('downloads')}>Choose {show?'episode & version':'version to download'}</Button>}
  tabs={[{id:'overview',label:'Overview',content:<Stack gap="md"><Group><Badge variant="light">{show?'TV series':'Movie'}</Badge><Badge variant="light">{busy?'Checking files…':`${entries.length} available ${entries.length===1?'file':'files'}`}</Badge></Group><Text style={{whiteSpace:'pre-line'}}>{item.description||'Choose Downloads to see every available file and version for this title.'}</Text>{show&&<Title order={3} size="h4">Seasons and episodes are available in Downloads.</Title>}</Stack>},{id:'downloads',label:'Downloads',content:choices}]}/>
}
