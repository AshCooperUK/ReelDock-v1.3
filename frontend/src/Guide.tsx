import surfaces from './components/MediaSurfaces.module.css';
import { UnstyledButton as ControlTrigger } from '@mantine/core';
import { SelectField as NativeSelect } from './components/SelectField';
import { NotificationMessage } from './notifications';
import { TextInput } from './TextField';
import { setPageSearch, usePageSearch } from './pageSearch';
import { mediaDate } from './mediaDate';
import { mediaLength } from './mediaDuration';
import LiveArtwork from './LiveArtwork';
import ChannelAvatar from './ChannelAvatar';
import { openChannel } from './channelNavigation';
import { sortChannels, shiftDay } from './guideData';
import VideoDownloadControls from './VideoDownloadControls';
import DiscoveryTitle from './discovery/DiscoveryTitle';
import type { DiscoverItem } from './Discover';
import { useEffect, useRef, useState } from 'react';
import { CalendarDays, ChevronLeft, ChevronRight, Clock3, Film, Star } from 'lucide-react';
import { Button, Switch } from './components/ui';
import { SkeletonRow } from './components/states';
import LoadingStatus from './components/LoadingStatus';
import VideoSheet from './VideoSheet';
import TileActions from './collections/TileActions';
import styles from './GuideTimeline.module.css';
import { ActionIcon, Badge, Box, Grid, Group, NavLink, Paper, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { useMediaQuery } from '@mantine/hooks';

type Status = 'Air date' | 'Release date' | 'Premiere date' | 'Found in sources' | 'Uploaded' | 'Live now' | 'Scheduled' | 'Expected' | 'Usual slot passed';
export type Programme = { allDay?:boolean; id?: string; date?:string; thumbnail?: string; videoId?: string; title: string; start: number; duration?: number; durationSeconds?:number; metadata_pending?:boolean; status: Status; note: string; description: string; category: string; views?: string };
export type Channel = { show?:DiscoverItem; movie?:DiscoverItem; sortMetrics?: {latest:number|null;upcoming:number|null;activity:number|null}; subscriberCount?:number; id?: string; avatar?: string; name: string; initials: string; subscribers: string; topic: string; bio: string; favourite: boolean; programmes: Programme[] };

const time = (hour: number) => `${Math.floor(hour).toString().padStart(2, '0')}:${Math.round((hour % 1) * 60).toString().padStart(2, '0')}`;

const cardStep = 104;
// Notes that only restate the status already encoded by the card class (and
// the time shown in its top line) are dropped from the card face.
const statusNote = /^(Uploaded|Live now|Starts at |Estimated around |Date only|Air date)/;
const informativeNote = (programme: Programme) => programme.note && !statusNote.test(programme.note) ? programme.note : '';
const guideBatch = 40;
const programmeLength=(programme:Programme)=>mediaLength({durationSeconds:programme.durationSeconds,runtimeMinutes:programme.duration,metadata_pending:programme.metadata_pending});


export function guideNoticeType(notice:string){return /^(?:Updating this week:|YouTube updates are paused\.|Some channels could not update yet\. Showing saved videos; retries are queued\.)/.test(notice.trim())?'progress':'warning';}
export type LiveGuideData = { csrf: string; channels: Channel[]; date: string; today: string; now: number; loading: boolean; error: string; notice?:string; noticeKind?:'warning'; truncated: boolean; saving: boolean; changeDay: (offset: number) => void; retry: () => void; selectDate: (date:string) => void; favourite: (id: string, value: boolean) => void };
export default function Guide({ favouritesOnTop, onFavouritesOnTop, live }: { favouritesOnTop: boolean; onFavouritesOnTop: (checked: boolean) => void; live: LiveGuideData }) {
  const now=live.now;
  const days=Array.from({length:7},(_,i)=>shiftDay(live.date,i));
  const todayIndex=days.indexOf(live.today);
  const nowPosition=(todayIndex+now/24)/7*100;
  const guideChannels = live.channels;
  const hasShows=guideChannels.some(c=>c.show);
  const mobileControls=useMediaQuery('(max-width: 47.99em)',false,{getInitialValueInEffect:false});
  const query=usePageSearch('TV Guide');
  const [filter, setFilter] = useState('All videos');
  const [sort,setSort]=useState(()=>{try{return localStorage.getItem('reeldock-guide-sort')||'name-asc';}catch{return 'name-asc';}});
  const [selection, setSelection] = useState<{ channel: Channel; programme: Programme } | null>(null);
  const [mobileDay,setMobileDay]=useState('week');
  const [selectedTitle,setSelectedTitle]=useState<DiscoverItem|null>(null);
  function openGuideRow(channel:Channel){if(channel.movie||channel.show)setSelectedTitle((channel.movie||channel.show)!);else if(channel.id)openChannel({id:channel.id,name:channel.name,avatar:channel.avatar});}
  function selectProgramme(channel:Channel,programme:Programme){if(channel.movie||channel.show)setSelectedTitle((channel.movie||channel.show)!);else setSelection({channel,programme});}
  useEffect(()=>{if(mobileDay!=='week'&&!days.includes(mobileDay))setMobileDay(live.date);},[live.date]);
  useEffect(() => { setSelection(null); }, [live.date]);
  function programmes(channel:Channel){
    const lanes=new Map<number,number>();
    return channel.programmes.filter(matches).map(programme=>{
      const dayIndex=days.indexOf(programme.date||live.date),lane=lanes.get(dayIndex)||0;
      lanes.set(dayIndex,lane+1);return {...programme,dayIndex,lane};
    }).filter(programme=>programme.dayIndex>=0);
  }
  function trackHeight(channel:Channel){return Math.max(cardStep,...programmes(channel).map(p=>cardStep*(p.lane+1)));}
  const isToday=todayIndex>=0;
  const favouriteKeys=guideChannels.filter(c=>c.favourite).map(c=>c.id||c.name);
  const keyOf = (channel: Channel) => channel.id || channel.name;
  const programmeTitle=(channel:Channel,programme:Programme)=>channel.movie||channel.show?channel.name:programme.title;
  const dateLabel = mediaDate(live.date)+' – '+mediaDate(days[6]);
  const visibleChannels = sortChannels(guideChannels.filter(c => c.name.toLowerCase().includes(query.toLowerCase()) || c.programmes.some(p => p.title.toLowerCase().includes(query.toLowerCase()))),sort,favouritesOnTop,favouriteKeys);
  const [noticeDismissed,setNoticeDismissed]=useState(false);
  const noticeKind=live.noticeKind||guideNoticeType(live.notice||'');
  useEffect(()=>{setNoticeDismissed(false);},[noticeKind==='warning'?live.notice:'']);
  // Both views mount a bounded batch. The agenda pages programmes; the optional
  // timeline pages channels, while keeping every programme's original identity.
  const canVirtualize=typeof IntersectionObserver!=='undefined';
  const [renderCount,setRenderCount]=useState(guideBatch);
  const [display,setDisplay]=useState('agenda');
  const scrollRef=useRef<HTMLDivElement|null>(null),sentinelRef=useRef<HTMLDivElement|null>(null);
  const matches = (p: Programme) => filter === 'All videos' || (filter === 'Uploaded' && p.status === 'Uploaded') || (filter === 'Upcoming' && (['Scheduled', 'Expected'].includes(p.status)||(!!p.allDay&&(p.date||'')>=live.today))) || (filter === 'Live now' && p.status === 'Live now');
  const channelOrder=new Map(visibleChannels.map((channel,index)=>[channel,index]));
  const latestRank=(p:Programme)=>p.allDay||!Number.isFinite(p.start)?2:p.status==='Uploaded'||p.status==='Live now'?0:1;
  const agendaEntries=visibleChannels.flatMap(channel=>channel.programmes.filter(p=>days.includes(p.date||live.date)&&matches(p)).map(programme=>({channel,programme})));
  const actualDays=new Set(agendaEntries.filter(entry=>latestRank(entry.programme)===0).map(entry=>entry.programme.date||live.date));
  const compareDays=(a:string,b:string)=>{
    const day=days.indexOf(a)-days.indexOf(b);
    return sort==='latest'?(Number(actualDays.has(b))-Number(actualDays.has(a)))||-day:day;
  };
  const allAgenda=agendaEntries.sort((a,b)=>{
    const day=compareDays(a.programme.date||live.date,b.programme.date||live.date);
    if(day)return day;
    // Latest means actual uploads/live entries before forecasts, then date-only
    // events. Favourites stay within that day's truthful event category.
    if(sort==='latest'){const category=latestRank(a.programme)-latestRank(b.programme);if(category)return category;}
    const favourite=favouritesOnTop?Number(favouriteKeys.includes(keyOf(b.channel)))-Number(favouriteKeys.includes(keyOf(a.channel))):0;
    if(favourite)return favourite;
    const channel=channelOrder.get(a.channel)!-channelOrder.get(b.channel)!;
    const start=(Number.isFinite(a.programme.start)?a.programme.start:0)-(Number.isFinite(b.programme.start)?b.programme.start:0);
    return sort==='latest'?-start||channel:sort==='upcoming'?start||channel:channel||start;
  });
  const agendaDays=[...days].sort(compareDays);
  const agenda=mobileDay==='week'?allAgenda:allAgenda.filter(entry=>(entry.programme.date||live.date)===mobileDay);
  const renderedAgenda=canVirtualize?agenda.slice(0,renderCount):agenda;
  const renderedChannels=canVirtualize?visibleChannels.slice(0,renderCount):visibleChannels;
  const total=display==='timeline'?visibleChannels.length:agenda.length;
  const hasMore=display==='timeline'?renderedChannels.length<total:renderedAgenda.length<total;
  useEffect(()=>{setRenderCount(guideBatch);},[query,sort,filter,favouritesOnTop,live.date,mobileDay,display]);
  useEffect(()=>{const sentinel=sentinelRef.current;if(!hasMore||!sentinel)return;const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting))setRenderCount(count=>Math.min(count+guideBatch,total));},{root:display==='timeline'?scrollRef.current:null,rootMargin:'800px 0px'});observer.observe(sentinel);return()=>observer.disconnect();},[hasMore,renderCount,total,mobileControls,mobileDay,display]);
  function jumpToNow(){setMobileDay(live.today);setDisplay('agenda');live.changeDay(0);}
  function toggleFavourite(name:string){live.favourite(name,!favouriteKeys.includes(name));}
  const tone=(status:Status)=>status==='Live now'?'red':status==='Uploaded'?'green':status==='Expected'||status==='Usual slot passed'?'yellow':'blue';
  function agendaProgramme(channel:Channel,programme:Programme){
    const title=programmeTitle(channel,programme),note=informativeNote(programme);
    const context=Array.from(new Set([programme.description,note].filter(Boolean))).join(' · ');
    const metadataSource=!!(channel.movie||channel.show);
    const sourceLabel=(channel.movie?'Movie information: ':channel.show?'TV show information: ':'Channel information: ')+channel.name;
    const publishLabel=programme.status==='Uploaded'?'Published':programme.status;
    const uploadInfo=publishLabel+' · '+mediaDate(programme.date||live.date)+' · '+(programme.allDay?'Date only':time(programme.start));
    const source=<Stack data-guide-source gap="xs" style={{minWidth:0}}>
      <ControlTrigger type="button" data-guide-source-open aria-label={sourceLabel} disabled={!metadataSource&&!channel.id} mih={44} w="100%" style={{minWidth:0,textAlign:'left'}} onClick={()=>openGuideRow(channel)}>
        <Group gap="sm" wrap="nowrap" align="center">
          <Box w={mobileControls?48:64} h={mobileControls?48:64} style={{flexShrink:0,overflow:'hidden',borderRadius:'var(--mantine-radius-md)'}}>
            {metadataSource?<LiveArtwork src={programme.thumbnail||channel.avatar||''} fit="contain" placeholder={<Film size={24}/>}/>:<ChannelAvatar src={channel.avatar} detail/>}
          </Box>
          <Text size="sm" fw={600} lineClamp={2} title={channel.name} style={{minWidth:0,overflowWrap:'anywhere'}}>{channel.name}</Text>
        </Group>
      </ControlTrigger>
      <Text data-guide-upload size="xs" c="dimmed" style={{overflowWrap:'anywhere'}}>{uploadInfo}</Text>
    </Stack>;
    const video=<Stack data-guide-video gap="xs" style={{flex:1,minWidth:0}}>
      <Text component="h3" m={0} size="sm" fw={600} lineClamp={2} title={title} style={{overflowWrap:'anywhere'}}>{title}</Text>
      {context&&<Text data-guide-description size="sm" c="dimmed" lineClamp={2} title={context} style={{overflowWrap:'anywhere'}}>{context}</Text>}
      <Group gap="xs" wrap="wrap">
        {(programme.videoId||programme.duration||programme.durationSeconds)&&<Text size="xs" c="dimmed">{programmeLength(programme)}</Text>}
        <Badge size="xs" variant="light" color={tone(programme.status)}>{programme.status==='Uploaded'?'Published':programme.status}</Badge>
        {programme.category&&<Text size="xs" c="dimmed">{programme.category}</Text>}
      </Group>
    </Stack>;
    const actions=<Group data-guide-actions gap={4} wrap="nowrap" style={{flexShrink:0}}>
      {!metadataSource&&<ActionIcon size={44} variant="subtle" color="yellow" aria-label={`${favouriteKeys.includes(keyOf(channel))?'Unfavourite':'Favourite'} ${channel.name}`} aria-pressed={favouriteKeys.includes(keyOf(channel))} disabled={live.saving} onClick={()=>toggleFavourite(keyOf(channel))}><Star size={17} fill={favouriteKeys.includes(keyOf(channel))?'currentColor':'none'}/></ActionIcon>}
      <ActionIcon size={44} variant="default" aria-label={'Open '+title} title={'Open '+title} onClick={()=>selectProgramme(channel,programme)}><ChevronRight size={18}/></ActionIcon>
    </Group>;
    return <Paper component="article" key={programme.id||keyOf(channel)+programme.title+(programme.start||'')} data-guide-programme data-media-surface="tile" className={[surfaces.surface,surfaces.tile].filter(Boolean).join(' ')} withBorder radius="md" p="md" style={{minWidth:0}}>
      {mobileControls?<Stack gap="md"><Group gap="sm" wrap="nowrap" align="flex-start"><Box style={{flex:1,minWidth:0}}>{source}</Box>{actions}</Group>{video}</Stack>:<Group gap="md" wrap="nowrap" align="flex-start"><Box w={{sm:180,lg:220}} style={{flexShrink:0,minWidth:0}}>{source}</Box>{video}{actions}</Group>}
    </Paper>;
  }
  return <Stack component="section" gap="md" aria-label="TV Guide">
   <Paper withBorder radius="lg" p="md"><Stack gap="md">
    <Group justify="space-between" align="center"><Stack gap={3}><Text fw={600}>{dateLabel}</Text><Text size="xs" c="dimmed">Europe/London · {allAgenda.length} programmes across your sources</Text></Stack><Group gap="xs"><ActionIcon variant="default" size={44} aria-label="Previous week" onClick={()=>live.changeDay(-7)}><ChevronLeft size={18}/></ActionIcon><Button variant="secondary" onClick={jumpToNow}><Clock3 size={16}/>Today</Button><ActionIcon variant="default" size={44} aria-label="Next week" onClick={()=>live.changeDay(7)}><ChevronRight size={18}/></ActionIcon></Group></Group>
    <SimpleGrid cols={{base:2,md:3}} spacing="sm"><NativeSelect aria-label="Guide view" value={display} onChange={event=>setDisplay(event.currentTarget.value)} data={[{value:'agenda',label:'Agenda'},{value:'timeline',label:'Week timeline'}]}/><NativeSelect aria-label="Filter guide" value={filter} onChange={event=>setFilter(event.currentTarget.value)}>{['All videos','Uploaded','Upcoming','Live now'].map(value=><option key={value} value={value}>{value==='All videos'&&guideChannels.some(c=>c.movie||c.show)?'All media':value}</option>)}</NativeSelect><NativeSelect aria-label="Sort channels" value={sort} onChange={event=>{setSort(event.currentTarget.value);try{localStorage.setItem('reeldock-guide-sort',event.currentTarget.value);}catch{/* Current visit only. */}}}>{[['name-asc','Channel A–Z'],['name-desc','Channel Z–A'],['latest','Latest upload'],['upcoming','Next expected upload'],['activity','Most active · 30 days'],['subscribers-desc','Most subscribers'],['subscribers-asc','Fewest subscribers'],['favourites','Favourites only']].map(([value,label])=><option key={value} value={value}>{label}</option>)}</NativeSelect></SimpleGrid>
    <Switch showLabel label="Favourites first" checked={favouritesOnTop} onCheckedChange={onFavouritesOnTop}/>
   </Stack></Paper>
   <LoadingStatus active={live.loading&&!live.channels.length} label="Loading this week…"/>
   {live.notice&&noticeKind==='progress'&&<Text size="sm" c="dimmed" role="status" aria-live="polite" data-guide-progress>{live.notice}</Text>}
   {live.notice&&noticeKind==='warning'&&!noticeDismissed&&<NotificationMessage id={'guide-warning-'+live.date} kind="warning" message={live.notice} onDismiss={()=>setNoticeDismissed(true)} actions={<Button variant="secondary" onClick={live.retry}>Retry</Button>}/>}
   {live.error&&<NotificationMessage id={'guide-error-'+live.date} kind="error" message={live.error} actions={<Button variant="secondary" onClick={live.retry}>Try again</Button>}/>}
   {live.truncated&&<Text size="sm" c="dimmed" role="status">This week contains more than 5,000 videos. Only the first 5,000 are shown.</Text>}
   {display==='agenda'?<Grid gap="lg" align="flex-start">
    <Grid.Col span={{base:12,sm:3}}><Paper withBorder radius="lg" p="sm"><Stack gap="sm"><TextInput type="date" aria-label="Choose date" value={mobileDay==='week'?live.date:mobileDay} onChange={event=>{if(event.currentTarget.value){setMobileDay(event.currentTarget.value);live.selectDate(event.currentTarget.value);}}}/>{mobileControls?<NativeSelect aria-label="Guide day" value={mobileDay} onChange={event=>setMobileDay(event.currentTarget.value)} data={[{value:'week',label:'Whole week ('+allAgenda.length+')'},...days.map(day=>({value:day,label:mediaDate(day)+' ('+allAgenda.filter(entry=>(entry.programme.date||live.date)===day).length+')'}))]}/>:<Stack gap={4} aria-label="Select guide day"><NavLink component="button" type="button" label="Whole week" rightSection={<Badge variant="light" color="gray">{allAgenda.length}</Badge>} active={mobileDay==='week'} onClick={()=>setMobileDay('week')} mih={44}/>{days.map(day=><NavLink key={day} component="button" type="button" label={new Date(day+'T12:00:00Z').toLocaleDateString('en-GB',{weekday:'long',day:'numeric',month:'short',timeZone:'UTC'})} description={day===live.today?'Today':undefined} rightSection={<Badge variant="light" color="gray">{allAgenda.filter(entry=>(entry.programme.date||live.date)===day).length}</Badge>} active={mobileDay===day} aria-pressed={mobileDay===day} onClick={()=>setMobileDay(day)} mih={52}/> )}</Stack>}</Stack></Paper></Grid.Col>
    <Grid.Col span={{base:12,sm:9}}><Stack gap="lg" aria-label="Guide agenda">
     {agendaDays.filter(day=>mobileDay==='week'||mobileDay===day).map(day=>{const entries=renderedAgenda.filter(entry=>(entry.programme.date||live.date)===day);if(!entries.length&&mobileDay==='week')return null;return <Stack component="section" key={day} gap="sm" aria-label={'Programmes for '+day}><Group justify="space-between"><Stack gap={2}><Title order={2} size="h4">{day===live.today?'Today':new Date(day+'T12:00:00Z').toLocaleDateString('en-GB',{weekday:'long',timeZone:'UTC'})}</Title><Text size="xs" c="dimmed">{mediaDate(day)}</Text></Stack><Badge variant="light" color="gray">{allAgenda.filter(entry=>(entry.programme.date||live.date)===day).length} programmes</Badge></Group>{entries.map(({channel,programme})=>agendaProgramme(channel,programme))}{!entries.length&&!live.loading&&<Text size="sm" c="dimmed">No programmes for this day.</Text>}</Stack>;})}
     {!live.loading&&!live.error&&!agenda.length&&<Paper withBorder radius="md" p="xl"><Stack align="center"><CalendarDays size={28}/><Text fw={600}>No programmes in this view</Text><Text size="sm" c="dimmed">Choose another day, adjust the filters or use the shared search.</Text></Stack></Paper>}
     {hasMore&&<Box ref={sentinelRef} role="status" aria-label="Loading more programmes"><SkeletonRow/><Button variant="secondary" onClick={()=>setRenderCount(count=>Math.min(count+guideBatch,total))}>Load more programmes</Button></Box>}
    </Stack></Grid.Col>
   </Grid>:<Paper withBorder radius="lg" className={styles.viewport} role="region" aria-label="Seven-day channel guide" ref={scrollRef}>
    <Box className={styles.table} role="table" aria-label="Programmes by channel and publication time">
     <Box className={styles.header} role="row"><Text fw={600} p="sm" role="columnheader">Media · {visibleChannels.length}</Text><Box className={styles.days} role="columnheader" aria-label="Days of the week">{days.map(day=><Text key={day} ta="center" p="sm" fw={day===live.today?700:500} c={day===live.today?'var(--mantine-primary-color-light-color)':undefined}>{mediaDate(day)}</Text>)}</Box></Box>
     {renderedChannels.map(channel=><Box className={styles.row} role="row" key={keyOf(channel)} style={{minHeight:trackHeight(channel)+1}}><Stack className={styles.identity} role="rowheader" gap="xs" p="sm"><ControlTrigger className={styles.title} title={channel.name} aria-label={(channel.movie?'Movie information: ':channel.show?'TV show information: ':'Channel information: ')+channel.name} onClick={()=>openGuideRow(channel)}><Group wrap="nowrap" gap="sm"><Box w={36} h={36} style={{flexShrink:0}}>{channel.movie||channel.show?<LiveArtwork src={channel.avatar||''} placeholder={<Film size={18}/>}/>:<ChannelAvatar src={channel.avatar}/>}</Box><Text size="sm" fw={600} lineClamp={2}>{channel.name}</Text></Group></ControlTrigger>{!(channel.movie||channel.show)&&<ActionIcon size={44} variant={favouriteKeys.includes(keyOf(channel))?'light':'subtle'} color="yellow" aria-label={`${favouriteKeys.includes(keyOf(channel))?'Unfavourite':'Favourite'} ${channel.name}`} aria-pressed={favouriteKeys.includes(keyOf(channel))} disabled={live.saving} onClick={()=>toggleFavourite(keyOf(channel))}><Star size={17} fill={favouriteKeys.includes(keyOf(channel))?'currentColor':'none'}/></ActionIcon>}</Stack><Box className={styles.track} style={{height:trackHeight(channel)}} role="cell">{programmes(channel).map(programme=><Paper component="button" type="button" withBorder radius="md" p="xs" key={programme.id||programme.title+(programme.date||'')+(programme.start||'')} data-media-surface="tile" className={[styles.programme,surfaces.surface,surfaces.tile].filter(Boolean).join(' ')} style={{top:6+programme.lane*cardStep,left:`calc(${programme.dayIndex*100/7}% + 4px)`,width:`calc(${100/7}% - 8px)`,borderStyle:programme.status==='Expected'||programme.status==='Usual slot passed'?'dashed':'solid'}} onClick={()=>selectProgramme(channel,programme)} aria-label={`${programme.title}, ${channel.name}, ${programme.status}, ${programme.allDay?'date only':time(programme.start)}${informativeNote(programme)?', '+informativeNote(programme):''}`} title={informativeNote(programme)||undefined}><Stack gap={5}><Group gap={4} justify="space-between"><Text size="xs" fw={600}>{programme.allDay?'Date only':time(programme.start)}</Text>{(programme.videoId||programme.duration)&&<Text size="xs" c="dimmed">{programmeLength(programme)}</Text>}</Group><Text size="sm" fw={600} lineClamp={2}>{programme.title}</Text><Text size="xs" c={tone(programme.status)} lineClamp={1}>{programme.status}</Text></Stack></Paper>)}</Box></Box>)}
     {hasMore&&<Box ref={sentinelRef} p="md" aria-hidden="true"><SkeletonRow/><SkeletonRow/></Box>}
     {isToday&&<Box aria-hidden="true" className={styles.now} style={{left:`calc(200px + (100% - 200px) * ${nowPosition/100})`}}/>}
    </Box>
   </Paper>}
   {!live.loading&&!live.error&&!visibleChannels.length&&<Paper withBorder radius="lg" p="xl"><Stack align="center"><CalendarDays size={28}/><Title order={3} size="h5">{!guideChannels.length?'No channels in your guide yet':'No matching channels'}</Title><Text size="sm" c="dimmed">{!guideChannels.length?'Imported channels that are shown in the guide will appear here.':'Try another channel or video title.'}</Text><Button variant="secondary" onClick={()=>{live.changeDay(0);setPageSearch('TV Guide','');}}>Reset view</Button></Stack></Paper>}
   <Group gap="sm">{(['Uploaded','Scheduled','Expected','Live now','Usual slot passed'] as Status[]).map(status=><Badge key={status} variant="dot" color={tone(status)}>{status}</Badge>)}{guideChannels.some(c=>c.movie||c.show)&&<Badge variant="dot">{hasShows?'Movie & TV dates':'Movie dates'}</Badge>}</Group>
   <Text size="xs" c="dimmed">Times: Europe/London. Expected uploads are estimates. Movie and TV dates have no announced time and open the title details.</Text>
   {selectedTitle&&<DiscoveryTitle item={selectedTitle} csrf={live.csrf} onClose={()=>setSelectedTitle(null)}/>}
   {selection&&<VideoSheet csrf={live.csrf} open onOpenChange={open=>{if(!open)setSelection(null);}} canManage={!!selection.programme.videoId} video={{id:selection.programme.videoId||'',title:selection.programme.title,image:selection.programme.thumbnail,channel:selection.channel.name,channelId:selection.channel.id,avatar:selection.channel.avatar,durationSeconds:selection.programme.durationSeconds,runtimeMinutes:selection.programme.duration,metadata_pending:selection.programme.metadata_pending}} actions={selection.programme.videoId&&<TileActions item={{kind:'video',id:'video:'+selection.programme.videoId,videoId:selection.programme.videoId,title:selection.programme.title,image:selection.programme.thumbnail,channel:selection.channel.name,channelId:selection.channel.id}}/>} downloads={selection.programme.videoId?<VideoDownloadControls key={selection.programme.videoId} id={selection.programme.videoId} title={selection.programme.title} poster={selection.programme.thumbnail} csrf={live.csrf} canDownload={selection.programme.status==='Uploaded'} showCollections={false}/>:<Paper withBorder p="xl" radius="md"><Stack align="center"><CalendarDays size={36}/><Text>No announced video yet</Text></Stack></Paper>} content={<Stack gap="md"><Badge variant="light" color={tone(selection.programme.status)}>{selection.programme.status}</Badge>{informativeNote(selection.programme)&&informativeNote(selection.programme)!==selection.programme.description&&<Text size="sm" c="dimmed">{informativeNote(selection.programme)}</Text>}<Text>{selection.programme.description}</Text><SimpleGrid cols={{base:1,sm:2}}><Stack gap={3}><Text size="xs" c="dimmed">{selection.programme.status==='Uploaded'?'Published':'Starts'}</Text><Text>{mediaDate(selection.programme.date||live.date)} · {time(selection.programme.start)}</Text></Stack><Stack gap={3}><Text size="xs" c="dimmed">Duration</Text><Text>{selection.programme.videoId||selection.programme.duration?programmeLength(selection.programme):'Not announced'}</Text></Stack><Stack gap={3}><Text size="xs" c="dimmed">Category</Text><Text>{selection.programme.category}</Text></Stack>{selection.programme.views&&<Stack gap={3}><Text size="xs" c="dimmed">Views</Text><Text>{selection.programme.views}</Text></Stack>}</SimpleGrid></Stack>}/>}
  </Stack>;
}
