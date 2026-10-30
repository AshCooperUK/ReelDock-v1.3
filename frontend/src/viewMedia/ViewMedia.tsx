import surfaces from '../components/MediaSurfaces.module.css';
import { retargetLocalCard, type LibraryChange } from './localRename';
import { NotificationMessage } from '../notifications';
import { ActionIcon, AspectRatio, Badge, Box, Card, Center, Grid, Group, Image, Paper, SimpleGrid, Stack, Switch, Text, Title, UnstyledButton, VisuallyHidden } from '@mantine/core';
import { SelectField as NativeSelect } from '../components/SelectField';
import { memo, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ChevronLeft, ChevronRight, Film, Grid2X2, LayoutGrid, List, Tv, X, Youtube } from 'lucide-react';
import { api } from '../api';
import { filterMediaCards } from './filtering';
import { mediaSearchQuery } from '../appNavigation';
import { Button, Dialog, DialogContent, PopupShell } from '../components/ui';
import { DetailSheet } from '../components/DetailSheet';
import MediaManagement from '../MediaManagement';
import VideoSheet from '../VideoSheet';
import DiscoveryTitle from '../discovery/DiscoveryTitle';
import VideoDownloadControls from '../VideoDownloadControls';
import { M3uDetails } from '../M3uLibrary';
import ChannelIdentity from '../ChannelIdentity';
import ChannelAvatar from '../ChannelAvatar';
import { openChannel, channelEvent } from '../channelNavigation';

import type { MediaCard, MediaPage, MediaSource } from './types';
import SearchYouTubeDownload from './SearchYouTubeDownload';
import { createParentOpener, type LocalParentTarget } from './localParent';
import MediaWarnings from './MediaWarnings';
import TileActions,{mediaTileTarget} from '../collections/TileActions';
import HoverCard,{useCardHover,titleHover,type HoverInfo} from '../HoverCard';
import { MediaArtwork, MediaCard as BrowseCard } from '../components/browse';
import styles from './MediaBrowse.module.css';
import tileStyles from './ArtworkTiles.module.css';
import { mediaCardLength } from '../mediaDuration';
import { mediaDate } from '../mediaDate';
import { attachScrollPaging, observeAutoPaging, trackPagingMomentum } from './scrollPaging';
import { createPagePreloader } from './pagePreloader';
import { sortGroups, orderCards, searchSections, searchPage, searchPageSize } from './ordering';
import { refreshDelay, refreshedPage, mergeCards, pendingPage } from './refresh';
import { movieSections, movieSorts, defaultMovieSection, defaultMovieSort } from './movieOptions';
import { showSections, showSorts, defaultShowSection, defaultShowSort } from './showOptions';
const labels:Record<string,string>={all:'All media',movies:'Movies',shows:'TV Shows',youtube:'YouTube',trending:'Trending',new:'Recently added',top:'Top rated',title:'Title A–Z'};
export type MediaLayout='tiles'|'grid'|'list';
export const MediaTile = memo(function MediaTile({item,layout,upcoming,label,searched,onOpen}:{item:MediaCard;layout:string;upcoming:boolean;label:string;searched:boolean;onOpen:(item:MediaCard)=>void}) {
  const hover=useCardHover();
  if(layout==='tiles')return <Paper component="li" data-media-surface="tile" data-media-layout="tiles" className={[surfaces.surface,tileStyles.tile].filter(Boolean).join(' ')} radius="sm" p={0}>
    <UnstyledButton className={tileStyles.button} w="100%" aria-label={'Open '+item.title} onClick={()=>onOpen(item)}>
      <MediaArtwork src={item.image||item.backdrop} fit="cover"/>
      <Text component="span" data-artwork-title aria-hidden="true" className={tileStyles.title} size="sm" fw={600}>{item.title}</Text>
    </UnstyledButton>
  </Paper>;
  const kindLabel=item.kind==='channel'?'YouTube channel':['show','shows','episode'].includes(item.kind)?'TV show':['youtube','video'].includes(item.kind)?'YouTube':'Movie';
  const KindIcon=kindLabel==='TV show'?Tv:kindLabel.startsWith('YouTube')?Youtube:Film;
  const searchVideo=searched&&['youtube','video'].includes(item.kind);
  const grouped=item.action?.type==='local-group';
  const countLabel=item.count?item.count+' '+(item.count===1?{show:'episode',shows:'episode',youtube:'video'}[item.kind]||'file':{show:'episodes',shows:'episodes',youtube:'videos'}[item.kind]||'files'):'';
  const subtitle=searchVideo?(mediaDate(item.date)||'Upload date unavailable'):grouped?(item.year?item.year+' · '+countLabel:countLabel):item.year||mediaDate(item.date)||(searched?'':({movie:'Movie',show:'TV show',video:'YouTube'}[item.kind]||label));
  const origins=(item.origins||[item.origin]).filter(Boolean),hasStatus=searched||!!item.count||origins.length>0||layout==='list'&&!!item.description;
  const hoverInfo:HoverInfo={title:item.title,image:item.image,backdrop:item.backdrop,kind:item.kind==='channel'?'Channel':kindLabel,year:item.year,rating:item.rating,date:item.date?.slice(0,10)||item.releaseDate?.slice(0,10),channel:item.channel,channelId:item.channelId,description:item.description};
  const titleButton=<UnstyledButton w="100%" mih={44} aria-label={'Open '+item.title+(searched?' · '+kindLabel:'')} onClick={()=>onOpen(item)} style={{textAlign:'left'}}><Text fw={600} lineClamp={2} className="hover-card-title" title={searchVideo?item.title:undefined}>{item.title}</Text></UnstyledButton>;
  return <BrowseCard length={mediaCardLength(item)} layout={layout==='list'?'list':'grid'} ref={hover.anchor as never} {...titleHover(hover)}
   art={<UnstyledButton w="100%" style={{display:'block'}} onClick={()=>onOpen(item)} aria-label={'Open '+item.title}><MediaArtwork src={item.image||item.backdrop} kind={item.kind} badge={upcoming?'Upcoming':item.rating?'★ '+item.rating:undefined}/></UnstyledButton>}
   title={item.kind==='channel'?<ChannelIdentity id={item.channelId} name={item.title} avatar={item.image}/>:titleButton}
   channel={item.channel&&item.kind!=='channel'?<ChannelIdentity id={item.channelId} name={item.channel}/>:undefined}
   meta={subtitle?<Text component="span" size="sm" title={searchVideo?(item.dateApproximate?"Approximate upload date supplied by public YouTube search":"Upload date"):undefined}>{subtitle}</Text>:undefined}
   status={hasStatus?<Stack gap={4}>{(searched||!!item.count)&&<Group gap={6}>{searched&&<Badge variant="light" size="sm" leftSection={<KindIcon size={12}/>}>{kindLabel}</Badge>}{!!item.count&&<Badge variant="light" size="sm">{countLabel}</Badge>}</Group>}{origins.length>0&&<Text size="xs" c="dimmed">{origins.join(' · ')}</Text>}{layout==='list'&&item.description&&<Text size="sm" lineClamp={3}>{item.description}</Text>}</Stack>:undefined}
   actions={!grouped&&<TileActions {...mediaTileTarget(item)} inline/>}>
   <HoverCard info={hoverInfo} hover={hover}/>
  </BrowseCard>;
});
export function AutoMoreResults({enabled,batch,onMore}:{enabled:boolean;batch:string;onMore:()=>void}) {
  const edge=useRef<HTMLDivElement>(null),latest=useRef(onMore);latest.current=onMore;
  useEffect(()=>{if(!enabled||!edge.current)return;return observeAutoPaging(edge.current,()=>latest.current());},[enabled,batch]);
  return <Box ref={edge} h={1} data-auto-results-edge aria-hidden="true"/>;
}

export function MediaResults({items,layout,searched,source,today,onOpen,limits={},onMore,loading=false}:{items:MediaCard[];layout:MediaLayout;searched:boolean;source:string;today:string;onOpen:(item:MediaCard)=>void;limits?:Record<string,number>;onMore?:(group:string)=>void;loading?:boolean}) {
  const cards=(rows:MediaCard[])=><SimpleGrid component="ul" m={0} p={0} style={{listStyle:'none'}} cols={layout==='list'?1:layout==='tiles'?{base:2,sm:3,md:4,lg:5,xl:6}:{base:2,md:3,xl:4}} spacing={layout==='tiles'?'xs':'md'}>{rows.map(item=><MediaTile key={item.id} item={item} layout={layout} searched={searched} upcoming={!!((searched||source==='discover')&&item.action.type==='discover'&&item.releaseDate&&item.releaseDate>today)} label={labels[item.kind]||''} onOpen={onOpen}/>)}</SimpleGrid>;
  return searched?<Stack gap="xl">{searchSections(items).map(section=>{
    const page=onMore?searchPage(section.items,limits[section.label]):{items:section.items,hasMore:false};
    return <Stack component="section" gap="md" aria-label={section.label} aria-busy={loading} key={section.label}><Title order={3}>{section.label}</Title>{cards(page.items)}<VisuallyHidden role="status" aria-live="polite" aria-atomic="true">{page.items.length+' of '+section.items.length+' '+section.label+' results shown'}</VisuallyHidden>{page.hasMore&&<AutoMoreResults enabled={!loading} batch={section.label+':'+page.items.length+':'+section.items.length} onMore={()=>onMore?.(section.label)}/>}</Stack>;
  })}</Stack>:cards(items);
}

export default function ViewMedia({csrf,initial,initialSource='discover',initialMedia='movies',sourceLocked=false}:{csrf:string;initial?:MediaPage;initialSource?:string;initialMedia?:string;sourceLocked?:boolean}) {
  const tileViewAvailable=initialSource==='discover';
  const startAll=initialSource==='discover'&&!sourceLocked;
  const [sources,setSources]=useState<MediaSource[]>([]),[source,setSource]=useState(startAll?'all':initialSource),[media,setMedia]=useState(startAll?'all':initialMedia),[sort,setSort]=useState(startAll||initialSource==='local'?'new':initialSource==='discover'?(initialMedia==='shows'?defaultShowSort:defaultMovieSort):'source');
  const [category,setCategory]=useState('all');
  const [searchSource,setSearchSource]=useState('all'),[searchKind,setSearchKind]=useState('all');
  const [collection,setCollection]=useState(!startAll&&initialSource==='discover'?(initialMedia==='shows'?defaultShowSection:defaultMovieSection):''),[query,setQuery]=useState(()=>mediaSearchQuery(location.search)||'');
  const [discoveryOptions,setDiscoveryOptions]=useState<Pick<MediaPage,'feeds'|'categories'>>({});
  const [page,setPage]=useState<MediaPage|null>(startAll?null:initial || null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[revision,setRevision]=useState(0);
  const [layout,setLayout]=useState<MediaLayout>(tileViewAvailable?'tiles':'grid'),[hero,setHero]=useState(0),[selected,setSelected]=useState<MediaCard|null>(null),[childSelected,setChildSelected]=useState<MediaCard|null>(null);
  const [heroHidden,setHeroHidden]=useState(()=>{try{return localStorage.getItem('reeldock-hero-hidden')==='1';}catch{return false;}});
  const [searchLimits,setSearchLimits]=useState<Record<string,number>>({});
  const [showShorts,setShowShorts]=useState(false);
  const sentinel=useRef<HTMLDivElement>(null),paging=useRef(false);
  const pageCache=useRef(new Map<string,MediaPage>());
  useEffect(() => {
    const syncSearch = () => { const q = mediaSearchQuery(location.search) || ''; setQuery(q); setPage(null); setRevision(i => i + 1); };
    window.addEventListener('reeldock-media-search', syncSearch);
    window.addEventListener('popstate', syncSearch);
    return () => { window.removeEventListener('reeldock-media-search', syncSearch); window.removeEventListener('popstate', syncSearch); };
  }, []);
  const nextPage=useRef(createPagePreloader((url,signal)=>api<MediaPage>(url,signal)));
  const youtubeBrowse=source==='subscriptions'&&media==='youtube'&&!query;
  const shortsMode=youtubeBrowse?(showShorts?'1':'0'):undefined;
  const shortsQuery:Record<string,string>=shortsMode===undefined?{}:{show_shorts:shortsMode};
  const nextUrl=page?.nextOffset==null?'':(query?'/api/view-media/search?':'/api/view-media?')+new URLSearchParams({source,media,sort,collection,category,q:query,...shortsQuery,...(source==='discover'?{engine:'1'}:{}),offset:String(page.nextOffset),...(page.nextCursor?{cursor:page.nextCursor}:{})});
  const scopeKey=JSON.stringify(query?['global',query,sort]:[source,media,sort,collection,category,query,...(shortsMode===undefined?[]:[shortsMode])]);
  const controller=useRef<AbortController|null>(null),generation=useRef(0),readyScope=useRef('');
  useEffect(()=>{const c=new AbortController();api<{items:MediaSource[]}>('/api/view-media/sources',c.signal).then(r=>setSources(r.items)).catch(e=>{if(!c.signal.aborted)setError(e.message);});return()=>c.abort();},[]);
  const current=sources.find(s=>s.id===source);
  const globalSearch=!!query;
  const movieMode=source==='discover'&&media==='movies',showMode=source==='discover'&&media==='shows';
  const groups=sortGroups.filter(g=>current?.sorts.includes(g.values[0])&&(!g.values[0].startsWith('episode_')||(media==='shows'&&!query))&&(source!=='discover'||media!=='youtube'||!!query||['source','title'].includes(g.values[0])));
  const sortGroup=groups.find(g=>g.values.includes(sort));
  const requestSort=sort;
  const requestMedia=media;
  function changeSource(id:string){const next=sources.find(s=>s.id===id);if(!next)return;setSource(id);setMedia(next.media[0]);setSort(id==='discover'?defaultMovieSort:next.sorts[0]);setCollection(id==='discover'?defaultMovieSection:'');setCategory('all');setQuery('');setSelected(null);}
  function changeShowShorts(checked:boolean){
    if(checked===showShorts)return;
    controller.current?.abort();generation.current++;readyScope.current='';paging.current=false;
    nextPage.current.clear();pageCache.current.delete(scopeKey);
    setPage(null);setSelected(null);setChildSelected(null);setHero(0);setError('');setBusy(true);setShowShorts(checked);
  }
  useEffect(()=>{
    if(!current)return;
    const c=new AbortController();controller.current=c;const version=++generation.current;let timer:ReturnType<typeof setTimeout>;let refreshLocal=source==='local',freshSearch=true;
    readyScope.current='';paging.current=false;setSearchLimits({});setPage(pageCache.current.get(scopeKey)||(source==='discover'&&media==='movies'&&collection===defaultMovieSection&&category==='all'&&!query?initial||null:null));setHero(0);setError('');setBusy(true);
    const load=async()=>{if(paging.current){timer=setTimeout(load,2000);return;}try{const result=await api<MediaPage>((globalSearch?'/api/view-media/search?':'/api/view-media?')+new URLSearchParams({...(globalSearch?{grouped:'1',...(freshSearch?{fresh:'1'}:{})}:{}),source,media:requestMedia,sort:requestSort,collection,category,q:query,...shortsQuery,...(source==='discover'?{engine:'1'}:{}),...(refreshLocal?{refresh:'1'}:{})}),c.signal);refreshLocal=false;freshSearch=false;if(!c.signal.aborted&&version===generation.current){readyScope.current=scopeKey;setError('');setPage(old=>{if(c.signal.aborted||version!==generation.current)return old;if(source==='all'&&old?.observationRevision!==result.observationRevision)nextPage.current.clear();if(source==='all'&&old?.observationSignature&&old.observationSignature!==result.observationSignature)pageCache.current.clear();const next=result.loading&&old?.items.length&&(source!=='all'||old.observationSignature===result.observationSignature)?{...old,refreshing:true}:(source==='discover'||source==='all'||globalSearch)?refreshedPage(old,{...result,items:mergeCards(result.items)}):result;pageCache.current.set(scopeKey,next);return next;});if(result.feeds)setDiscoveryOptions({feeds:result.feeds,categories:result.categories});const delay=refreshDelay(result,globalSearch?'global':source);if(delay!==null)timer=setTimeout(load,delay);}}catch(e){if(!c.signal.aborted){setError(e instanceof Error?e.message:'Could not load media.');if(source==='discover')timer=setTimeout(load,60000);}}finally{if(!c.signal.aborted)setBusy(false);}};
    void load();return()=>{c.abort();nextPage.current.clear();clearTimeout(timer);};
  },[source,requestMedia,requestSort,collection,category,query,revision,current?.id,shortsMode]);
  async function more(){
    if(globalSearch||busy||paging.current||page?.preparing||!nextUrl)return;
    paging.current=true;const version=generation.current,previous=page!;
    setBusy(true);setError('');
    try{
      const result=await nextPage.current.load(nextUrl,controller.current!.signal,incoming=>{
        if(version!==generation.current||controller.current?.signal.aborted)return;
        const partial=pendingPage(previous,incoming);setPage(partial);pageCache.current.set(scopeKey,partial);
      });
      if(version!==generation.current||controller.current?.signal.aborted)return;
      if(source==='all'&&previous.observationSignature&&previous.observationSignature!==result.observationSignature){pageCache.current.clear();nextPage.current.clear();setPage(null);setRevision(value=>value+1);return;}
      if(version===generation.current&&!controller.current?.signal.aborted)setPage(old=>{
        if(version!==generation.current||controller.current?.signal.aborted)return old;
        const next=source==='all'?refreshedPage(old,result):{...result,items:mergeCards([...(old?.items||[]),...result.items])};
        pageCache.current.set(scopeKey,next);return next;
      });
    }catch(e){if(e instanceof Error&&e.name==='AbortError')return;if(version===generation.current&&!controller.current?.signal.aborted)setError(e instanceof Error?e.message:'Could not load more.');}
    finally{if(version===generation.current){paging.current=false;setBusy(false);}}
  }
  // Warm one ordinary browse batch while the current cards are being read.
  // Do not advance its cursor or append cards until the scroll gate consumes it.
  useEffect(()=>{
    if(page?.completeSet||globalSearch||busy||error||selected||page?.preparing||page?.loading||!nextUrl||!controller.current||readyScope.current!==scopeKey)return;
    void nextPage.current.load(nextUrl,controller.current.signal).catch(()=>{});
  },[globalSearch,busy,error,selected,page?.preparing,page?.loading,nextUrl]);
  useEffect(()=>{if(page?.completeSet||globalSearch||busy||error||selected||page?.nextOffset==null)return;return attachScrollPaging(window,()=>sentinel.current?.getBoundingClientRect().top??Infinity,()=>void more());},[globalSearch,busy,error,page,selected]);
  useEffect(()=>{if(busy&&!globalSearch)return trackPagingMomentum(window);},[busy,globalSearch]);
  // Renames/deletes/enrolments elsewhere in the app must refresh the listing.
  useEffect(()=>{const onChange=(event:Event)=>{const changes=(event as CustomEvent<LibraryChange>).detail?.localChanges;if(Array.isArray(changes)&&changes.length){setSelected(card=>retargetLocalCard(card,changes));setChildSelected(card=>retargetLocalCard(card,changes));setPage(old=>old?{...old,items:old.items.map(card=>retargetLocalCard(card,changes)!)}:old);for(const [key,cached] of pageCache.current)pageCache.current.set(key,{...cached,items:cached.items.map(card=>retargetLocalCard(card,changes)!)});nextPage.current.clear();}setRevision(i=>i+1);};window.addEventListener('reeldock-library-changed',onChange);return()=>window.removeEventListener('reeldock-library-changed',onChange);},[]);
  // A channel identity inside the group list opens the global channel sheet;
  // close the group overlay so the two never stack (same rule as DetailSheet).
  useEffect(()=>{const close=()=>{setSelected(null);setChildSelected(null);};window.addEventListener(channelEvent,close);return()=>window.removeEventListener(channelEvent,close);},[]);
  const cards=useMemo(()=>globalSearch?filterMediaCards(page?.items||[],searchSource,searchKind):source==='discover'?orderCards(mergeCards(page?.items||[]),showMode&&sort==='source'?'new':sort):page?.items||[],[source,showMode,globalSearch,page?.items,sort,searchSource,searchKind]);
  const today=new Date().toLocaleDateString('en-CA',{timeZone:'Europe/London'});
  const featured=page?.items.slice(0,5)||[],active=featured[hero%Math.max(1,featured.length)];
  const parentOpener=useRef<ReturnType<typeof createParentOpener>|null>(null);
  if(!parentOpener.current)parentOpener.current=createParentOpener({
    request:(id,signal)=>api<{target:LocalParentTarget}>('/api/view-media/local/'+encodeURIComponent(id)+'/parent',signal),
    discover:card=>{setChildSelected(null);setSelected(card);},
    group:card=>setSelected(card),
  });
  useEffect(()=>()=>parentOpener.current?.cancel(),[]);
  useEffect(()=>{parentOpener.current?.cancel();},[scopeKey]);
  const openItem=useCallback((item:MediaCard)=>{if(item.kind==='channel'&&item.channelId){parentOpener.current?.cancel();openChannel({id:item.channelId,name:item.title,avatar:item.image});}else parentOpener.current?.open(item);},[]);

  // One renderer for every item overlay: discover titles open the shared
  // DetailSheet; YouTube videos, local files and M3U/VOD items use the same
  // sheet anatomy with Overview/Manage/Download settings tabs. A child opened
  // from a local group swaps the group overlay for its own sheet so only one
  // overlay is ever visible.
  function itemDialog(item:MediaCard,subtitle:string|undefined,close:()=>void){
   if(item.action.type==='discover')return <DiscoveryTitle globalSearch={globalSearch} key={item.id} item={item.action.item} csrf={csrf} onClose={close}/>;
   if(item.action.type==='vod')return <M3uDetails source={item.action.source} item={item.action.item} onClose={close}/>;
   if(item.action.type==='youtube')return <VideoSheet csrf={csrf} open onOpenChange={open=>{if(!open)close();}}
     video={{...item,id:item.action.id,title:item.title,image:item.image,backdrop:item.backdrop,channel:item.channel,channelId:item.channelId}}
     subtitle={subtitle}
     actions={<TileActions {...mediaTileTarget(item)}/>}
     downloads={globalSearch?<SearchYouTubeDownload key={item.action.id} item={item} csrf={csrf}/>:<VideoDownloadControls id={item.action.id} title={item.title} poster={item.image} csrf={csrf} canDownload showCollections={false}/>}
     description={item.description}/>;
   const overview=<Text c="dimmed" style={{whiteSpace:"pre-line"}}>{item.description||"Saved in your library. Use Manage to rename, repair or remove it."}</Text>;
   return <DetailSheet open onOpenChange={open=>{if(!open)close();}}
     banner={item.backdrop||item.image}
     title={item.title}
     subtitle={subtitle}
     channel={item.channel?<ChannelIdentity id={item.channelId} name={item.channel}/>:undefined}
     media={(item.image||item.backdrop)?<Image src={item.image||item.backdrop} alt="" radius="sm" fit="cover"/>:undefined}
     actions={item.action.type==='local'&&<TileActions {...mediaTileTarget(item)}/>}
     tabs={item.action.type==='local'?[
      {id:'overview',label:'Overview',content:overview},
      {id:'manage',label:'Manage',content:<MediaManagement csrf={csrf} localId={item.action.id}/>},
     ]:undefined}
    >{overview}</DetailSheet>;
  }
  return <Stack component="section" gap="lg">
    <Paper withBorder p={{base:'sm',sm:'md'}} radius="md"><Stack gap="sm">

      <Box className={styles.fields}>
        {globalSearch?<>
          <NativeSelect aria-label="Search source" value={searchSource} onChange={e=>setSearchSource(e.target.value)} data={[{value:'all',label:'All sources'},...sources.filter(entry=>entry.id!=='all').map(entry=>({value:entry.id,label:entry.label}))]} />
          <NativeSelect aria-label="Search media type" value={searchKind} onChange={e=>setSearchKind(e.target.value)} data={[{value:'all',label:'All media'},{value:'movies',label:'Movies'},{value:'shows',label:'TV Shows'},{value:'youtube',label:'YouTube'}]} />
        </>:<>
          {!!sources.length&&!sourceLocked&&<NativeSelect aria-label="Media source" value={source} onChange={e=>changeSource(e.target.value)} data={sources.map(entry=>({value:entry.id,label:entry.label}))} />}
          <NativeSelect aria-label="Media type" value={media} onChange={e=>{const kind=e.target.value;setPage(null);setMedia(kind);setCollection(source==='discover'?(kind==='movies'?defaultMovieSection:kind==='shows'?defaultShowSection:''):'');if(source==='discover')setSort(kind==='movies'?defaultMovieSort:kind==='shows'?defaultShowSort:'source');else if(sort.startsWith('episode_')&&kind!=='shows')setSort(current?.sorts[0]||'source');setCategory('all');setQuery('');setRevision(i=>i+1);}} data={(current?.media||[]).map(kind=>({value:kind,label:labels[kind]||kind}))} />
          {source==='discover'&&media==='youtube'&&<NativeSelect aria-label="YouTube category" value={category} onChange={e=>{setCategory(e.target.value);setCollection('related');}} data={(discoveryOptions.categories||[{id:'all',name:'All topics'}]).map(c=>({value:c.id,label:c.name}))} />}
        </>}
        {!!page?.collections?.length&&<NativeSelect aria-label="VOD playlist" value={collection||page.collection} onChange={e=>setCollection(e.target.value)} data={page.collections.map(c=>({value:c.id,label:c.name}))}/>}
        {movieMode||showMode||globalSearch?<>
          {!globalSearch&&<NativeSelect aria-label={showMode?'TV section':'Movie section'} value={collection||(showMode?defaultShowSection:defaultMovieSection)} onChange={e=>{setCollection(e.target.value);setQuery('');setSelected(null);}} data={(showMode?showSections:movieSections).map(section=>({value:section.value,label:section.label}))}/>}
          <NativeSelect aria-label={showMode&&!globalSearch?'TV sort order':'Movie sort order'} value={sort} onChange={e=>setSort(e.target.value)} data={(globalSearch?[{value:'new',label:'Recently saved'},{value:'oldest',label:'Oldest saved'},...movieSorts.map(option=>option.value==='source'?{...option,label:'Source order'}:option)]:showMode?showSorts:movieSorts).map(option=>({value:option.value,label:option.label}))}/>
        </>:<>
          <NativeSelect aria-label="Sort by" title={source==='discover'?'Sort the results loaded so far':'Sort catalogue'} value={sortGroup?.values[0]||sort} onChange={e=>setSort(e.target.value)} data={groups.map(g=>({value:g.values[0],label:g.label==='Date added'&&source==='subscriptions'?'Upload date':g.label==='Date added'&&source==='discover'?'NZB upload date':g.label==='Last episode added'?'Episode added':g.label}))}/>
          {sortGroup&&sortGroup.values.length>1&&<NativeSelect aria-label="Sort direction" value={sort} onChange={e=>setSort(e.target.value)} data={sortGroup.values.map((value,i)=>({value,label:sortGroup.directions[i]}))}/>}
        </>}
      </Box>
      {youtubeBrowse&&<Group align="center" wrap="wrap" mih={44}><Switch label="Show Shorts" checked={showShorts} onChange={event=>changeShowShorts(event.currentTarget.checked)} styles={{root:{minHeight:44,display:'flex',alignItems:'center'},body:{minHeight:44,alignItems:'center'},label:{minHeight:44,display:'flex',alignItems:'center',cursor:'pointer'}}}/></Group>}
    </Stack></Paper>
    {page?.freshness&&<Text size="xs" c="dimmed" mb="md" role="status">{page.freshness.source}{page.freshness.updatedAt?' · Chart: '+page.freshness.updatedAt:''}{page.freshness.fetchedAt?' · Checked: '+new Date(page.freshness.fetchedAt).toLocaleString():''}{page.freshness.stale?' · Saved snapshot (stale)':''}. {page.freshness.scope}</Text>}
    {globalSearch&&page?.providerChecksPending&&<Text size="sm" c="dimmed" mb="md" role="status">Showing saved matches while source checks finish…</Text>}
    {globalSearch&&page?.searchUpdatesAvailable&&<Group mb="md"><Text size="sm" role="status">New source results available.</Text><Button variant="secondary" onClick={()=>setRevision(i=>i+1)}>Update results</Button></Group>}
    {layout!=='tiles'&&!query&&!heroHidden&&active&&active.kind!=='channel'&&<Card data-media-surface="tile" className={[surfaces.surface,surfaces.tile].filter(Boolean).join(' ')} withBorder radius="md" mb="md" component="section" aria-label="Featured media">
      <Group align="flex-start" wrap="nowrap" gap="md">
        <Box data-featured-artwork visibleFrom="sm" w={72} style={{flexShrink:0}}><MediaArtwork src={active.image||active.backdrop} kind={active.kind}/></Box>
        <Stack gap="sm" style={{flex:1,minWidth:0}}><Group justify="space-between" align="flex-start" wrap="nowrap"><Title order={3}>{active.title}</Title><ActionIcon variant="subtle" aria-label="Hide featured media" title="Hide featured media" onClick={()=>{setHeroHidden(true);try{localStorage.setItem('reeldock-hero-hidden','1');}catch{}}}><X size={18}/></ActionIcon></Group><Text c="dimmed" size="sm">{[active.year,active.rating?'★ '+active.rating:'',labels[media]].filter(Boolean).join(' · ')}</Text><Text lineClamp={2} size="sm">{active.description||'Explore this title from '+(current?.label||'NZB')+'.'}</Text><Group><Button onClick={()=>openItem(active)}>View details</Button>{active.action.type!=='local-group'&&<TileActions {...mediaTileTarget(active)} inline/>}</Group></Stack>
      </Group>
      {featured.length>1&&<Group mt="md" justify="space-between"><ActionIcon variant="default" aria-label="Previous featured title" onClick={()=>setHero(i=>(i+featured.length-1)%featured.length)}><ChevronLeft size={20}/></ActionIcon><Text size="sm" c="dimmed">{hero%featured.length+1} of {featured.length}</Text><ActionIcon variant="default" aria-label="Next featured title" onClick={()=>setHero(i=>(i+1)%featured.length)}><ChevronRight size={20}/></ActionIcon></Group>}
    </Card>}
    <Group justify="space-between" align="center" gap="sm" mb="md">
      <Title order={2} size="h3">{query?'Results for “'+query+'”':labels[media]||page?.label||'Media'}</Title>
      <Group gap="sm" style={{flexWrap:'wrap'}}>
        {tileViewAvailable&&<ActionIcon variant={layout==='tiles'?'light':'default'} aria-label="Tile only" aria-pressed={layout==='tiles'} onClick={()=>setLayout('tiles')}><LayoutGrid size={18}/></ActionIcon>}
        <ActionIcon variant={layout==='grid'?'light':'default'} aria-label="Poster grid" aria-pressed={layout==='grid'} onClick={()=>setLayout('grid')}><Grid2X2 size={18}/></ActionIcon><ActionIcon variant={layout==='list'?'light':'default'} aria-label="Media list" aria-pressed={layout==='list'} onClick={()=>setLayout('list')}><List size={18}/></ActionIcon>
        {layout!=='tiles'&&!query&&heroHidden&&<Button variant="secondary" onClick={()=>{setHeroHidden(false);try{localStorage.removeItem('reeldock-hero-hidden');}catch{}}}>Show featured</Button>}
      </Group>
    </Group>
    {error&&<NotificationMessage kind="error" message={error} actions={<> <Button variant="secondary" onClick={()=>setRevision(i=>i+1)}>Try again</Button> </>}/>}
    <MediaWarnings key={scopeKey+':'+revision} messages={page?.warnings}/>
    {page?.scanning&&<Text size="sm" role="status">Reading your local media folders…</Text>}
    <MediaResults items={cards} layout={layout} searched={globalSearch} source={source} today={today} onOpen={openItem} limits={searchLimits} loading={busy||page?.loading||page?.refreshing||!!selected||!!childSelected} onMore={globalSearch?group=>setSearchLimits(old=>({...old,[group]:(old[group]||searchPageSize)+searchPageSize})):undefined}/>
    {globalSearch&&!busy&&!!page?.items.length&&!cards.length&&<Text c="dimmed" my="xl">No matches in these filters. Choose All sources or All media to see the other results.</Text>}
    {(busy||page?.loading)&&<Text size="sm" role="status">{globalSearch?'Searching sources…':'Loading media…'}</Text>}{page?.error&&<NotificationMessage kind="error" message={page.error} actions={<> <Button variant="secondary" onClick={()=>setRevision(n=>n+1)}>Try again</Button> </>}/>}{!busy&&!error&&!page?.scanning&&!page?.loading&&!page?.incomplete&&page&&!page.items.length&&<Paper withBorder p="xl" radius="md"><Stack align="center" ta="center"><Film size={32}/><Title order={3}>{query?'No matching media':source==='all'&&page.observationState==='unindexed'?'No observed titles yet':'No media to show yet'}</Title><Text c="dimmed">{query?(page?.completeSet?'No saved matches were found. Try a shorter title or check any source alerts.':'Try another title or search term.'):source==='all'?'Choose a source to browse its catalogue, or search all sources. Already saved results appear here automatically; background catalogue saving is optional.':source==='vod'?'Choose a synced M3U or Xtream playlist.':source==='subscriptions'?'Add YouTube channels and sync their uploads in ReelDock Settings.':'Media from the selected source will appear here.'}</Text></Stack></Paper>}
    <Box ref={sentinel} h={1} aria-hidden="true"/>
    {selected?.action.type==='local-group'
     ?<DetailSheet open={!childSelected} onOpenChange={open=>{if(!open){setSelected(null);setChildSelected(null);}}} title={selected.title} media={<ChannelAvatar src={selected.image}/>} channel={selected.kind==='youtube'&&selected.channelId?<ChannelIdentity id={selected.channelId} name={selected.channel||selected.title}/>:undefined} subtitle={[selected.count?selected.count+' '+(selected.count===1?{show:'episode',shows:'episode',youtube:'video'}[selected.kind]||'file':{show:'episodes',shows:'episodes',youtube:'videos'}[selected.kind]||'files'):'',globalSearch?'Search result':current?.label].filter(Boolean).join(' · ')}><SimpleGrid cols={{base:2,md:3}}>{(selected.children||[]).map(child=><MediaTile key={child.id} item={child} layout="grid" searched={false} upcoming={false} label={labels[child.kind]||''} onOpen={child=>child.action.type==='local-group'?setSelected(child):setChildSelected(child)}/>)}</SimpleGrid></DetailSheet>
     :selected&&itemDialog(selected,globalSearch?'Search result':current?.label,()=>{setSelected(null);setChildSelected(null);})}
    {childSelected&&itemDialog(childSelected,selected?.title||current?.label,()=>setChildSelected(null))}
  </Stack>;
}
