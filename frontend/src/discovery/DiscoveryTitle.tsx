import { SelectField as NativeSelect } from '../components/SelectField';
import { NotificationMessage, notify } from '../notifications';
import { useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, Check, Clock, Heart, Search, X, Youtube } from 'lucide-react';
import { api } from '../api';
import type { DiscoverItem } from '../Discover';
import { Button } from '../components/ui';
import { DetailSheet, SheetBack, type DetailSheetTab } from '../components/DetailSheet';
import MediaManagement, { MediaTitleHistory } from '../MediaManagement';
import VideoDownloadControls from '../VideoDownloadControls';
import ChannelIdentity from '../ChannelIdentity';
import { TextInput } from '../TextField';
import MovieReleaseSelect from './MovieReleaseSelect';
import { DownloadChoiceLayout, DownloadSourcePicker } from './ReleaseChoicePicker';
import EpisodeDownload from './EpisodeDownload';
import SeasonSelect from './SeasonSelect';
import { useMovieInformation, useShowInformation, MovieFacts, MovieCast } from './MovieInformation';
import { MediaArtwork, MediaLengthBadge } from '../components/browse';
import { mediaLength } from '../mediaDuration';
import { useChannelBanner } from '../useChannelBanner';
import { acquisitionItem } from '../discoverPayload';
import { mediaDate } from '../mediaDate';
import MovieOptions from './MovieOptions';
import ShowOptions from './ShowOptions';
import MovieDownloadStatus from './MovieDownloadStatus';
import TileActions from '../collections/TileActions';
import QuickDownload from '../QuickDownload';
import { Accordion, Grid, Group, Paper, Stack, Text, Title } from '@mantine/core';

export type EpisodeEntry={key:string;title:string;season:number|null;number:number|null;date:string;dateLabel:string;status:string;file:boolean;image:string;requested:boolean;requestId?:string;requestState:string;description:string;progress?:number;bytes?:number;estimatedBytes?:number;filename?:string;runtimeMinutes?:number;durationSeconds?:number};
type Entry=EpisodeEntry;
type Release={id:string;title:string;quality:string;size:number;source:string;reason:string;vod?:{source:string;path:string}};
type Data={item:DiscoverItem;items:Entry[];loading:boolean;error?:string;releaseFallback?:boolean;nextOffset:number|null;seasonCount:number;seasons?:number[];downloadAll:boolean;downloadFuture:boolean};
export default function DiscoveryTitle({item,csrf,onClose,globalSearch=false,initialEpisode}:{item:DiscoverItem;csrf:string;onClose:()=>void;globalSearch?:boolean;initialEpisode?:EpisodeEntry}) {
 const [data,setData]=useState<Data|null>(null),[error,setError]=useState(''),[sending,setSending]=useState<Record<string,boolean>>({});
 const [releases,setReleases]=useState<Release[]>([]),[source,setSource]=useState(''),[release,setRelease]=useState(''),[releaseBusy,setReleaseBusy]=useState(false),[releaseMessage,setReleaseMessage]=useState(''),[releaseError,setReleaseError]=useState('');
 const compactItem=acquisitionItem(item);
 const movieInformation=useMovieInformation(item),showInformation=useShowInformation(item);
 const video=item.kind==='video',movie=item.kind==='movie',show=item.kind==='show',catalogue=movie||show;
 const channelBanner=useChannelBanner(video?item.channelId:undefined);
 const information=movie?movieInformation:showInformation;
 const [season,setSeason]=useState('');
 const [historyFocus,setHistoryFocus]=useState(''),[optionsRevision,setOptionsRevision]=useState(0);
 const [episode,setEpisode]=useState<Entry|null>(initialEpisode||null),[term,setTerm]=useState(''),[query,setQuery]=useState(''),[paging,setPaging]=useState(false);
 const [episodeFavourites,setEpisodeFavourites]=useState<Record<string,boolean>|null>(null),[favouriteBusy,setFavouriteBusy]=useState<Record<string,boolean>>({}),[favouriteError,setFavouriteError]=useState(''),[favouriteRevision,setFavouriteRevision]=useState(0);
 // DetailSheet location state (appended after the states above — keep order).
 const [tab,setTab]=useState(initialEpisode?'downloads':'overview'),[expanded,setExpanded]=useState(false);
 useEffect(()=>{if(!show)return;const c=new AbortController();setEpisodeFavourites(null);setFavouriteError('');api<{episodes?:{episodeKey:string}[]}>('/api/favourites',c.signal).then(r=>{if(!c.signal.aborted)setEpisodeFavourites(Object.fromEntries((r.episodes||[]).map(entry=>[entry.episodeKey,true])));}).catch(e=>{if(!c.signal.aborted)setFavouriteError(e instanceof Error?e.message:'Could not load episode favourites.');});const changed=(event:Event)=>{const value=(event as CustomEvent<{key:string;favourite:boolean}>).detail;if(value?.key)setEpisodeFavourites(old=>old?{...old,[value.key]:value.favourite}:old);};window.addEventListener('reeldock-episode-favourite-changed',changed);return()=>{c.abort();window.removeEventListener('reeldock-episode-favourite-changed',changed);};},[item.id,show,favouriteRevision]);
 async function favourite(row:Entry){if(episodeFavourites===null||favouriteBusy[row.key])return;setFavouriteBusy(old=>({...old,[row.key]:true}));setFavouriteError('');try{const result=await api<{key:string;favourite:boolean}>('/api/favourites/episode',undefined,{key:row.key,favourite:!episodeFavourites[row.key]},csrf);setEpisodeFavourites(old=>({...old,[row.key]:result.favourite}));window.dispatchEvent(new CustomEvent('reeldock-episode-favourite-changed',{detail:result}));}catch(e){setFavouriteError(e instanceof Error?e.message:'Could not save episode favourite.');}finally{setFavouriteBusy(old=>({...old,[row.key]:false}));}}
 function favouriteAction(row:Entry){const selected=!!episodeFavourites?.[row.key],label=(selected?'Remove episode from favourites':'Favourite episode')+' · '+(row.season!==null?'S'+String(row.season).padStart(2,'0')+'E'+String(row.number??'?').padStart(2,'0')+' · ':'')+row.title;return <Button variant="secondary" iconOnly aria-label={label} title={label} aria-pressed={selected} disabled={episodeFavourites===null||favouriteBusy[row.key]} onClick={()=>void favourite(row)}><Heart size={18} fill={selected?'currentColor':'none'}/></Button>;}
 const episodeScroll=useRef(0);
 const body=useRef<HTMLDivElement>(null),loaded=useRef(30),generation=useRef(0),controller=useRef<AbortController|null>(null);
 const titleLoader=useRef<(()=>Promise<Data>)|null>(null);
 const episodeScope=useRef({id:item.id,query,season});
 const renderScope=useRef({id:item.id,query,season});if(renderScope.current.id!==item.id||renderScope.current.query!==query||renderScope.current.season!==season)renderScope.current={id:item.id,query,season};const scope=renderScope.current;
 function titleOwner(){const loader=titleLoader.current,version=generation.current;return()=>!!loader&&titleLoader.current===loader&&generation.current===version&&renderScope.current===scope&&!controller.current?.signal.aborted;}
 async function refresh(){const owns=titleOwner();if(!owns())throw new Error('This title is no longer open.');const next=await titleLoader.current!();if(!owns())throw new Error('This title is no longer open.');return next;}
 const [videoReady,setVideoReady]=useState(false);
 useEffect(()=>{if(!video)return;const c=new AbortController();api('/api/discover/youtube/register',c.signal,{item:compactItem},csrf).then(()=>{if(!c.signal.aborted)setVideoReady(true);}).catch(e=>{if(!c.signal.aborted)setError(e.message);});return()=>c.abort();},[item.id,csrf,video]);
 useEffect(()=>{
  if(video)return;
  const c=new AbortController();controller.current=c;const version=++generation.current;loaded.current=30;
  const current=()=>!c.signal.aborted&&generation.current===version&&renderScope.current===scope;
  const previous=episodeScope.current;if(previous.id!==item.id||previous.query!==query||previous.season!==season)setEpisode(old=>current()?(previous.id!==item.id?initialEpisode||null:null):old);episodeScope.current={id:item.id,query,season};
  setData(old=>current()&&old?.item?.id===item.id?{...old,items:[],loading:true,nextOffset:null}:current()?null:old);setError('');setSending({});setPaging(false);
  let timer:ReturnType<typeof setTimeout>|undefined,inFlight:Promise<Data>|null=null,settled:Data|null=null,refreshFlight:Promise<Data>|null=null,writeVersion=0;
  function load():Promise<Data>{
   if(inFlight)return inFlight;
   if(!current())return Promise.reject(new Error('This title is no longer open.'));
   clearTimeout(timer);setPaging(true);setError('');
   // A single owner reads every authoritative cursor. Refreshes retain the
   // displayed tail while updating it, and never exceed the API's 2,000 limit.
   inFlight=(async()=>{
    let offset=0,frame:Data|null=null;const limit=Math.min(2000,Math.max(30,loaded.current)),rows=new Map<string,Entry>();
    try{
     while(current()){
      const next=await api<Data>('/api/discovery-engine/title',c.signal,{item:compactItem,query,season:season===''?null:Number(season),offset,limit},csrf);
      if(!current())throw new Error('This title is no longer open.');
      for(const row of next.items)rows.set(row.key,row);
      const nextOffset=next.nextOffset??null;
      const invalid=nextOffset!==null&&(!Number.isSafeInteger(nextOffset)||nextOffset<=offset||nextOffset>250000||!next.items.length);
      frame={...next,items:[...rows.values()],nextOffset};
      const visible=nextOffset!==null&&settled?{...frame,items:[...frame.items,...settled.items.filter(row=>!rows.has(row.key))]}:frame;
      setData(old=>current()?visible:old);setError(old=>current()?next.error||'':old);
      if(invalid)throw new Error('Episode paging stopped because the next page was invalid. Try again.');
      if(next.error)break;
      if(nextOffset===null){settled=frame;loaded.current=Math.max(30,rows.size);timer=setTimeout(()=>{void load().catch(()=>{});},next.loading?1000:3000);break;}
      offset=nextOffset;
     }
     if(!frame)throw new Error('Could not load this title.');
     return frame;
    }catch(e){if(current())setError(e instanceof Error?e.message:'Could not load this title.');throw e;}
    finally{if(current())setPaging(false);inFlight=null;}
   })();
   return inFlight;
  }
  function afterWrite():Promise<Data>{
   writeVersion++;
   if(refreshFlight)return refreshFlight;
   // An acquisition ACK must refresh a head read after that ACK, rather than
   // settling against an older head whose remaining pages were still loading.
   refreshFlight=(async()=>{
    if(inFlight)await inFlight;
    let next:Data,observed:number;
    do{observed=writeVersion;next=await load();}while(current()&&observed!==writeVersion);
    return next;
   })().finally(()=>{refreshFlight=null;});
   return refreshFlight;
  }
  titleLoader.current=afterWrite;void load().catch(()=>{});
  return()=>{c.abort();clearTimeout(timer);if(titleLoader.current===afterWrite)titleLoader.current=null;};
 },[item.id,csrf,video,query,season]);
 const standalone=!!data?.releaseFallback;
 const standaloneRow=standalone?data?.items[0]:undefined;
 const movieRow=movie?data?.items[0]:undefined;
 const futureMovie=!!movieRow?.date&&movieRow.date>new Date().toLocaleDateString('en-CA',{timeZone:'Europe/London'});
 useEffect(()=>{
  if(!movieRow||movieRow.file||movieRow.requested||futureMovie)return;
  const c=new AbortController();setReleaseBusy(true);setReleases([]);setSource('');setRelease('');setReleaseMessage('');setReleaseError('');
  type VodSource={id:string;label:string;type:string;entries:{source:string;path:string;title:string}[]};
  const nzb=item.externalId?api<{items:Release[];message:string}>('/api/media-management',c.signal,{action:'releases',key:movieRow.key},csrf):Promise.resolve({items:[],message:''});
  const vod=globalSearch?api<{sources:VodSource[]}>('/api/view-media/download-sources?'+new URLSearchParams({id:item.id,kind:item.kind,externalId:item.externalId||'',imdbId:item.imdbId||'',title:item.title,year:item.year||''}),c.signal):Promise.resolve({sources:[]});
  Promise.allSettled([nzb,vod] as const).then(([nzbResult,vodResult])=>{
   if(c.signal.aborted)return;
   const options:Release[]=nzbResult.status==='fulfilled'?nzbResult.value.items:[];
   if(vodResult.status==='fulfilled')for(const provider of vodResult.value.sources.filter(s=>s.type==='vod'))for(const [index,entry] of provider.entries.entries())options.push({id:'vod:'+provider.id+':'+index,title:entry.title,source:provider.label,quality:'',size:0,reason:'',vod:{source:entry.source,path:entry.path}});
   const messages=[nzbResult.status==='rejected'?nzbResult.reason.message:'',vodResult.status==='rejected'?vodResult.reason.message:''];
   setReleaseError(messages.filter(Boolean).join(' '));
   setReleases(options);setReleaseMessage(!options.length&&nzbResult.status==='fulfilled'?nzbResult.value.message:'');
   const first=options.find(r=>!r.reason)||options[0];setSource(first?.source||'');setRelease(first&&!first.reason?first.id:'');
  }).finally(()=>{if(!c.signal.aborted)setReleaseBusy(false);});
  return()=>c.abort();
 },[movieRow?.key,movieRow?.file,movieRow?.requested,futureMovie,csrf,globalSearch,optionsRevision]);
 const releaseSources=Array.from(new Set(releases.map(r=>r.source)));
 const sourceReleases=releases.filter(r=>r.source===source);
 const choice=releases.find(r=>r.id===release&&r.source===source&&!r.reason);
 function downloadAction(row:Entry,expanded=false){
  const future=!!row.date&&row.date>new Date().toLocaleDateString('en-CA',{timeZone:'Europe/London'});
  const cancelling=row.requestState==='cancelling';
  const label=row.file?'Downloaded':cancelling?'Cancelling…':sending[row.key]?'Saving request':row.requested?(['downloading','verifying','importing','paused'].includes(row.requestState)?'Cancel download':'Cancel request'):future?'Download when released':'Download';
  return <Button iconOnly={!expanded} variant="secondary" aria-label={label+' · '+row.title} title={label} disabled={row.file||cancelling||sending[row.key]||!!(movie&&!row.requested&&!futureMovie&&(releaseBusy||!choice))} onClick={()=>void download(row)}>
   {row.file?<Check size={18}/>:row.requested&&future?<Clock size={18}/>:row.requested?<X size={18}/>:<ArrowDownToLine size={18}/>}
   {expanded&&label}
  </Button>;
 }
 const sourcePicker=movie&&movieRow&&!futureMovie&&!movieRow.file&&!movieRow.requested?<Paper withBorder radius="md" p="md"><Stack gap="sm">
  <Title order={3} size="h4">Choose a download file</Title>
  {releaseBusy?<Group><Text size="sm" role="status">Finding available files…</Text>{downloadAction(movieRow,true)}</Group>:releases.length?<DownloadChoiceLayout
   source={<DownloadSourcePicker sources={releaseSources} value={source} onChange={value=>{setSource(value);setRelease(releases.find(r=>r.source===value&&!r.reason)?.id||'');}}/>}
   file={<MovieReleaseSelect releases={sourceReleases} value={release} onChange={setRelease} label="Download file"/>}
   action={downloadAction(movieRow,true)}/>:<Group>{downloadAction(movieRow,true)}</Group>}
  {!releaseBusy&&releaseMessage&&<Text size="sm" role="status">{releaseMessage}</Text>}<NotificationMessage kind="error" message={releaseError}/>
 </Stack></Paper>:null;
 async function download(row:Entry){
  const owns=titleOwner();if(sending[row.key]||!owns())return;
  setSending(old=>({...old,[row.key]:true}));setError('');
  try{
   if(row.requested&&row.requestId)await api('/api/discover/requests/cancel',undefined,{id:row.requestId},csrf);
   else if(movie&&choice?.vod){await api('/api/m3u-server-downloads',undefined,{action:'download',...choice.vod},csrf);notify({kind:'success',message:'Queued for server download.'});}
   else if(movie&&choice&&!futureMovie)await api('/api/media-management',undefined,{action:'search',key:row.key,value:choice.id},csrf);
   else if(movie&&!futureMovie)throw new Error('Select an available NZB release first.');
   else await api('/api/discovery-engine/download',undefined,{key:row.key},csrf);
   if(!owns())return;const next=await refresh();setEpisode(old=>owns()?(old?next.items.find(r=>r.key===old.key)||old:null):old);
   window.dispatchEvent(new Event('reeldock-library-changed'));
  }catch(e){if(owns())setError(e instanceof Error?e.message:'Could not request this download.');}
  finally{if(owns())setSending(old=>({...old,[row.key]:false}));}
 }
 const title={...item,...data?.item,...(standaloneRow?{title:standaloneRow.title}:{}),...(show&&!standalone&&showInformation.info?.title?{title:showInformation.info.title,year:showInformation.info.year||data?.item.year||item.year,description:showInformation.info.description||data?.item.description||item.description}:{})};
 const movieStatus=movieRow?(movieRow.file?'Downloaded with ReelDock':movieRow.requested&&futureMovie?'Waiting for release':movieRow.requested?movieRow.status:movieRow.status==='Needs attention'?'Needs attention':futureMovie?'Not released yet':'Not downloaded'):'';
 const details=episode?(data?.items.find(row=>row.key===episode.key)||episode):null;
 const videoTarget={kind:'video' as const,id:'video:'+(item.videoId||item.id.split(':').pop()),videoId:item.videoId||item.id.split(':').pop(),title:item.title,image:item.image,channel:item.channel,channelId:item.channelId};
 function openManageTab(id:string,row?:{key:string}){if(row?.key)setHistoryFocus(row.key);setTab(id);}
 const policyChange=()=>setOptionsRevision(n=>n+1);
 // Sorted, season-grouped episode rows (§2.2): newest season first, episode
 // numbers ordered inside each season, specials and unnumbered last.
 const episodeRows=(data?.items||[]).slice().sort((a,b)=>(b.season===0?-1:b.season??-2)-(a.season===0?-1:a.season??-2)||(b.number??0)-(a.number??0));
 function chooseEpisode(row:Entry){episodeScroll.current=body.current?.scrollTop||0;setEpisode(row);body.current?.scrollTo({top:0});}
 function backToEpisodes(){setEpisode(null);const restore=()=>body.current?.scrollTo({top:episodeScroll.current});if(typeof window.requestAnimationFrame==='function')window.requestAnimationFrame(restore);else restore();}
 const overview=<>
  {title.description&&<Text lineClamp={expanded?undefined:4} style={{whiteSpace:"pre-line"}} title={catalogue&&!expanded?title.description:undefined}>{title.description}</Text>}
  {catalogue&&title.description&&title.description.length>240&&<Button type="button" variant="ghost" onClick={()=>setExpanded(value=>!value)}>{expanded?'Show less':'More'}</Button>}
  {catalogue&&<MovieFacts info={information.info}/>}
  {error&&<NotificationMessage kind="error" message={error}/>}
  {favouriteError&&<NotificationMessage kind="error" message={favouriteError} actions={<> <Button variant="secondary" onClick={()=>setFavouriteRevision(n=>n+1)}>Try again</Button> </>}/>}
  {catalogue&&!error&&(!data||data.loading)&&!data?.items.length&&<Text size="sm" role="status">Loading {item.kind==='show'?'episodes':'movie'}…</Text>}
  {show&&!standalone&&<MovieCast {...showInformation} titleKind="show"/>}
  {movie&&<MovieCast {...movieInformation}/>}
 </>;
 const episodeChoices=<Stack gap="md">{!standalone&&<Paper withBorder p="md" radius="md"><Stack gap="md"><Stack gap={4}><Title order={3} size="h4">Choose what to download</Title><Text size="sm" c="dimmed">Select a season and episode, then choose its source and file.</Text></Stack><Grid gap="sm">{!!data?.seasons?.length&&<Grid.Col span={{base:12,sm:4}}><SeasonSelect seasons={data.seasons} value={season} onChange={setSeason}/></Grid.Col>}<Grid.Col span={{base:12,sm:data?.seasons?.length?8:12}}><NativeSelect aria-label="Episode" value="" disabled={!episodeRows.length} onChange={event=>{const selected=episodeRows.find(row=>row.key===event.currentTarget.value);if(selected)chooseEpisode(selected);}} data={[{value:'',label:'Choose an episode'},...episodeRows.map(row=>({value:row.key,label:(row.season!==null?'S'+String(row.season).padStart(2,'0')+'E'+String(row.number??'?').padStart(2,'0')+' · ':'')+row.title}))]}/></Grid.Col></Grid><Text size="xs" c="dimmed" role="status" aria-live="polite">{episodeRows.length} episodes loaded{paging&&data?.nextOffset!=null?' · Loading more episodes…':!data&&paging?' · Loading episodes…':data?.loading?' · Checking episode information…':''}</Text>{error&&<NotificationMessage kind="error" message={error} actions={<Button variant="secondary" disabled={paging} onClick={()=>void refresh().catch(()=>{})}>Try again</Button>}/>}<Accordion variant="separated" radius="md"><Accordion.Item value="find"><Accordion.Control>Find a specific episode</Accordion.Control><Accordion.Panel><Group component="form" wrap="nowrap" gap="sm" onSubmit={event=>{event.preventDefault();setQuery(term.trim());}}><TextInput leftSection={<Search size={18}/>} aria-label="Search seasons and episodes" placeholder="Episode title or number…" value={term} onChange={event=>setTerm(event.target.value)} onClear={()=>{setTerm('');setQuery('');}} style={{flex:1,minWidth:0}}/><Button type="submit">Find</Button></Group></Accordion.Panel></Accordion.Item></Accordion></Stack></Paper>}
  {standaloneRow&&<><Text size="sm" c="dimmed">Episode information is unavailable. Choose a matching download file.</Text>{!standaloneRow.file&&!standaloneRow.requested?<EpisodeDownload key={optionsRevision} item={data!.item} episode={standaloneRow} csrf={csrf} globalSearch={false} standalone onQueued={refresh}/>:<MovieDownloadStatus row={standaloneRow} action={downloadAction(standaloneRow,true)}/>}</>}
  {!standalone&&data&&!data.loading&&!data.items.length&&<Text size="sm">No matching episodes. Change the season or clear the episode search.</Text>}{!data&&<Text role="status">Loading episodes…</Text>}
 </Stack>;
 const today=new Date().toLocaleDateString('en-CA',{timeZone:'Europe/London'});
 const selectedEpisodeDownload=details?<Stack gap="md">
  <SheetBack label="episodes" onBack={backToEpisodes}/>
  <Paper withBorder p="md" radius="md"><Stack gap="sm"><Group justify="space-between" align="flex-start"><Stack gap={4} style={{minWidth:0,flex:1}}><Text size="xs" c="dimmed">{details.season!==null?'Season '+details.season+' · Episode '+(details.number??'?'):'Unnumbered episode'}</Text><Title order={3} size="h4">{details.title}</Title><MediaLengthBadge length={mediaLength(details)}/></Stack>{favouriteAction(details)}</Group><Text size="sm" c="dimmed">{mediaDate(details.date)||'Release date to be announced'} · {details.file?'Downloaded with ReelDock':details.requested&&details.date>today?'Waiting for release':details.requested?details.status:details.date>today?'Scheduled air date':details.status==='Needs attention'?'Needs attention':'Not downloaded'}</Text>{details.description&&<Text size="sm" style={{whiteSpace:'pre-line'}}>{details.description}</Text>}</Stack></Paper>
  {!details.file&&!details.requested?<EpisodeDownload key={optionsRevision} item={title} episode={details} csrf={csrf} globalSearch={globalSearch} fallbackAction={details.date>today?downloadAction(details,true):undefined} onQueued={async()=>{const owns=titleOwner();const next=await refresh();setEpisode(old=>owns()?next.items.find(r=>r.key===details.key)||details:old);}}/>:<MovieDownloadStatus row={details} action={downloadAction(details,true)}/>}
  <Group><Button variant="secondary" onClick={()=>setTab('manage')}>Manage this episode</Button></Group>
  {error&&<NotificationMessage kind="error" message={error}/>} {favouriteError&&<NotificationMessage kind="error" message={favouriteError}/>}
 </Stack>:null;
 const movieDownloads=<Stack gap="md">{movieRow&&(movieRow.file||movieRow.requested)&&<MovieDownloadStatus row={movieRow} action={downloadAction(movieRow,true)}/>} {sourcePicker}{movieRow&&!movieRow.file&&!movieRow.requested&&futureMovie&&<Paper withBorder p="md" radius="md"><Text size="sm" c="dimmed">This movie has not been released yet. Add it to your Watchlist to follow its release and automatic download.</Text></Paper>}{!movieRow&&!error&&<Text role="status">Preparing download choices…</Text>}{error&&<NotificationMessage kind="error" message={error}/>}</Stack>;
 const videoDownloads=videoReady?<VideoDownloadControls id={item.videoId||item.id.split(':').pop()||''} title={item.title} poster={item.image} csrf={csrf} canDownload settingsOnlyDownload showCollections={false}/>:error?<NotificationMessage kind="error" message={error}/>:<Text role="status">Preparing download choices…</Text>;
 const manage=video?<MediaManagement csrf={csrf} manageKey={videoTarget.id} item={videoTarget}/>:<Stack gap="lg">{movie?<MovieOptions embedded showFavourite={false} item={title} csrf={csrf} onPolicyChange={policyChange}/>:<ShowOptions embedded showFavourite={false} item={item} catalogueReady={!!data&&!data.loading} displayItem={{...title,backdrop:information.info?.backdrop||title.backdrop}} csrf={csrf} onPolicyChange={policyChange}/>}<Stack gap="md"><Group justify="space-between"><Title order={3} size="h4">{details?'Manage selected episode':show?'Downloaded files':'Files & requests'}</Title>{details&&<Button variant="ghost" onClick={()=>setEpisode(null)}>All episodes</Button>}</Group><MediaManagement csrf={csrf} item={title} filesOnly={show&&!details} manageKey={details?.key} episode={details&&typeof details.season==='number'&&typeof details.number==='number'?{season:details.season,number:details.number}:undefined} onOpenTab={openManageTab}/></Stack></Stack>;
 const tabs:DetailSheetTab[]=[
  {id:'overview',label:'Overview',content:overview},
  {id:'downloads',label:'Downloads',content:video?videoDownloads:movie?movieDownloads:details?selectedEpisodeDownload:episodeChoices},
  {id:'manage',label:'Manage',content:manage},
  {id:'history',label:'History',content:<MediaTitleHistory csrf={csrf} item={video?videoTarget:title} focus={historyFocus||details?.key}/>},
 ];
 return <DetailSheet open onOpenChange={open=>{if(!open){controller.current?.abort();titleLoader.current=null;onClose();}}} scrollBottom={show&&tab==='downloads'&&!details} compactHeader={tab==='downloads'}
  banner={video?channelBanner||item.backdrop||item.image:information.info?.backdrop||title.backdrop||title.image}
  bannerFallback={video?item.image:information.info?.image||title.image}
  media={<MediaArtwork src={catalogue?information.info?.image||title.image:item.image} kind={item.kind} fit="contain"/>}
  title={title.title}
  subtitle={video?'YouTube':<>{title.year||'Discover'}{movieStatus&&' · '+movieStatus}</>}
  channel={video&&item.channel?<ChannelIdentity id={item.channelId} name={item.channel}/>:undefined}
  actions={<QuickDownload metadata={(video||movie)&&<MediaLengthBadge length={mediaLength(title)}/>} actions={<TileActions item={video?videoTarget:title}/>} item={acquisitionItem(video?videoTarget:title)} csrf={csrf} disabled={(show&&(!data||data.loading))||(video&&!videoReady)||(movie&&(!!movieRow?.file||futureMovie))} disabledReason={show&&(!data||data.loading)?'Loading series episodes…':movieRow?.file?'Already saved in your library':movie&&futureMovie?'This movie has not been released yet':video&&!videoReady?'Preparing download choices…':undefined} onChoices={()=>setTab('downloads')}/>}
  tabs={tabs} tab={tab} onTab={setTab} bodyRef={body}/>
}
