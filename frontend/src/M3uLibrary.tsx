import { Button as ReelDockButton } from './components/ui';
import { UnstyledButton as ControlTrigger } from '@mantine/core';
import { SelectField as NativeSelect } from './components/SelectField';
import { NotificationMessage } from './notifications';
import { TextInput } from './TextField';
import FixMatch from './FixMatch';
import VodOptions from './collections/VodOptions';
import TileActions from './collections/TileActions';
import QuickDownload from './QuickDownload';
import HoverCard, { useCardHover, titleHover, type HoverInfo } from './HoverCard';
import { mediaDate } from './mediaDate';
import { ServerDownload, useJobs as useM3uDownloads } from './M3uDownloads';
import LiveArtwork from './LiveArtwork';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Film, Download, Search, UserRound } from 'lucide-react';
import { api } from './api';
import { onVisible, pageHidden } from './visibility';
import { Button } from './components/ui';
import { DetailSheet, type DetailSheetTab } from './components/DetailSheet';
import { ActionIcon, Anchor, Avatar, Badge, Box, Button as LinkButton, Grid, Group, Image, Paper, ScrollArea, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { MediaArtwork, MediaCard, MediaGrid, MediaLengthBadge } from './components/browse';
import { mediaCardLength, mediaLength, type MediaDuration } from './mediaDuration';

type Source = { id: string; name: string };
type Entry = MediaDuration & { title: string; path: string; kind: string; year?: number; group_name?: string; source?:string; artwork?:string; release_date?:string; rating?:number; description?:string; tmdb_url?:string };
type Person = { id: number; name: string; role: string; image?: string };
type Details = MediaDuration & { title: string; description: string; artwork?: string; facts: Record<string, string | number>; people?: Person[]; tmdb_url?: string; message?: string };
type DownloadInfo = { csrf: string; local: boolean; download: string; downloadAvailable: boolean; downloadReason: string };
const kinds = [['', 'All titles'], ['movies', 'Movies'], ['series', 'Episodes'], ['tv', 'Dated TV'], ['unsorted', 'Unsorted'], ['playlist', 'Live TV playlists']];
const sorts = [['added','Date Added'],['release','Release Date'],['random','Random'],['rating','Rating (TMDB)'],['title', 'Title A–Z'], ['title-desc', 'Title Z–A'], ['year-desc', 'Newest year'], ['year', 'Oldest year'], ['group', 'Playlist group'], ['type', 'Media type'], ['file', 'File name A–Z'], ['file-desc', 'File name Z–A']];
const label = (kind: string) => kinds.find(k => k[0] === kind)?.[1] || kind;
const message = (e: unknown) => e instanceof Error ? e.message : 'Please try again.';
function PlaylistCard({item,source,onOpen}:{item:Entry;source:string;onOpen:()=>void}){
 const hover=useCardHover<HTMLLIElement>();
 return <MediaCard length={mediaCardLength(item)} ref={hover.anchor} {...titleHover(hover)} art={<ControlTrigger w="100%" style={{display:'block'}} onClick={onOpen} aria-label={'Open '+item.title}><MediaArtwork src={item.artwork||fileUrl(source,item.path,'artwork')} kind={item.kind} badge={item.rating?'★ '+item.rating.toFixed(1):undefined}/></ControlTrigger>} title={<ControlTrigger w="100%" mih={44} onClick={onOpen} style={{textAlign:'left'}}><Text fw={600} lineClamp={2} className="hover-card-title">{item.title}</Text></ControlTrigger>} meta={[item.year,mediaDate(item.release_date),label(item.kind)].filter(Boolean).join(' · ')} status={item.group_name} actions={<TileActions resolve={{source,path:item.path}} inline/>}><HoverCard hover={hover} info={{title:item.title,image:item.artwork||fileUrl(source,item.path,'artwork'),kind:label(item.kind),year:item.year?String(item.year):'',rating:item.rating?item.rating.toFixed(1):'',date:mediaDate(item.release_date)||'',description:item.description||item.group_name}}/></MediaCard>;
}
function Facts({facts}:{facts:Record<string,string|number>}){return <SimpleGrid component="dl" cols={{base:1,sm:2}} spacing="sm" m={0}>{Object.entries(facts).map(([name,value])=><Paper key={name} withBorder p="sm" radius="md"><Text component="dt" size="xs" c="dimmed">{name}</Text><Text component="dd" size="sm" m={0} mt={4}>{['Released','Air date','Release / air date','Born','Died'].includes(name)?mediaDate(String(value))||'Not available':value}</Text></Paper>)}</SimpleGrid>;}
function fileUrl(source: string, path: string, endpoint = 'file') { return `/api/m3u/${encodeURIComponent(source)}/${endpoint}?` + new URLSearchParams({ path }); }
function Failure({ error, retry }: { error: string; retry?: () => void }) { return <NotificationMessage kind="error" message={error} actions={<> {retry && <Button variant="secondary" onClick={retry}>Try again</Button>} </>}/>; }

export default function M3uLibrary() {
  const [sources, setSources] = useState<Source[]>([]), [source, setSource] = useState('');
  const [busy, setBusy] = useState(true), [error, setError] = useState(''), [revision, setRevision] = useState(0);
  const [search, setSearch] = useState(''), [query, setQuery] = useState(''), [kind, setKind] = useState(''), [sort, setSort] = useState('title');
  useEffect(() => { const controller = new AbortController(); setBusy(true); setError(''); api<{ items: Source[] }>('/api/m3u', controller.signal).then(data => { if (controller.signal.aborted) return; setSources(data.items); setSource(old => data.items.some(s => s.id === old) ? old : data.items[0]?.id || ''); }).catch(e => { if (!controller.signal.aborted) setError(message(e)); }).finally(() => { if (!controller.signal.aborted) setBusy(false); }); return () => controller.abort(); }, [revision]);
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 300); return () => clearTimeout(timer); }, [search]);
  return <Stack gap="lg">{busy?<Text role="status">Loading playlists…</Text>:error?<Failure error={error} retry={()=>setRevision(v=>v+1)}/>:!sources.length?<Paper withBorder p="xl" radius="md"><Stack align="center" ta="center"><Film size={30}/><Title order={3}>No playlists yet</Title><Text c="dimmed">Your imported media will appear here. Add a playlist in ReelDock Settings → M3U Settings.</Text></Stack></Paper>:<><Paper withBorder p="md" radius="md"><Grid gap="sm"><Grid.Col span={12}><TextInput aria-label="Find an M3U title" placeholder="Find a title…" value={search} onChange={e=>setSearch(e.target.value)} leftSection={<Search size={18}/>}/></Grid.Col><Grid.Col span={{base:12,sm:4}}><NativeSelect aria-label="VOD subscription" value={source} onChange={e=>setSource(e.target.value)}>{sources.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</NativeSelect></Grid.Col><Grid.Col span={{base:6,sm:4}}><NativeSelect aria-label="Media type" value={kind} onChange={e=>setKind(e.target.value)}>{kinds.map(([value,text])=><option key={value} value={value}>{text}</option>)}</NativeSelect></Grid.Col><Grid.Col span={{base:6,sm:4}}><NativeSelect aria-label="Sort titles" value={sort} onChange={e=>setSort(e.target.value)}>{sorts.map(([value,text])=><option key={value} value={value}>{text}</option>)}</NativeSelect></Grid.Col></Grid></Paper><Entries key={JSON.stringify([source,query,kind,sort])} source={source} query={query} kind={kind} sort={sort}/></>}</Stack>;
}
function Entries({ source, query, kind, sort }: { source: string; query: string; kind: string; sort: string }) {
  const [items, setItems] = useState<Entry[]>([]), [more, setMore] = useState(true), [busy, setBusy] = useState(true), [error, setError] = useState(''), [selected, setSelected] = useState<Entry | null>(null);
  const seed=useRef(String(Math.random()));
  const offset = useRef(0), loading = useRef(false), controller = useRef(new AbortController()), sentinel = useRef<HTMLDivElement>(null);
  const load = useCallback(async () => {
    if (loading.current) return; loading.current = true; setBusy(true); setError(''); const signal = controller.current.signal;
    try { const result = await api<{ items: Entry[]; more: boolean }>(`/api/m3u/${encodeURIComponent(source)}/entries?` + new URLSearchParams({ q: query, kind, sort, seed:seed.current, offset: String(offset.current) }), signal); if (signal.aborted) return; offset.current += result.items.length; setItems(old => [...old, ...result.items.filter(item => !old.some(prior => prior.path === item.path))]); setMore(result.more); }
    catch (e) { if (!signal.aborted) setError(message(e)); }
    finally { if (!signal.aborted) { loading.current = false; setBusy(false); } }
  }, [source, query, kind, sort]);
  useEffect(() => { controller.current = new AbortController(); loading.current = false; void load(); return () => controller.current.abort(); }, [load]);
  useEffect(() => { if (busy || error || !more || !sentinel.current) return; const observer = new IntersectionObserver(entries => { if (entries.some(e => e.isIntersecting)) void load(); }, { rootMargin: '200px' }); observer.observe(sentinel.current); return () => observer.disconnect(); }, [busy, error, more, load]);
  return <Stack gap="md"><MediaGrid label="Playlist media">{items.map(item=><PlaylistCard key={item.path} item={item} source={source} onOpen={()=>setSelected(item)}/>)}</MediaGrid>{!busy&&!error&&!items.length&&<Paper withBorder p="xl" radius="md"><Stack align="center" ta="center"><Search size={28}/><Title order={3}>{query||kind?'No matching titles':'No imported titles yet'}</Title><Text c="dimmed">{query||kind?'Try another title or change the type filter.':'Media will appear after this playlist is imported.'}</Text></Stack></Paper>}{error&&<Failure error={error} retry={load}/>}<Text ref={sentinel} role="status" size="sm" c="dimmed">{busy?'Loading titles…':items.length?`${items.length.toLocaleString()} titles shown${more?' · Scroll for more':''}`:''}</Text>{selected&&<M3uDetails key={source+selected.path} source={source} item={selected} onClose={()=>setSelected(null)}/>}</Stack>;
}
export function M3uDetails({ source, item, onClose }: { source: string; item: Entry; onClose?: () => void }) {
  const [tab,setTab]=useState('overview');
  const [details, setDetails] = useState<Details | null>(null), [downloadInfo, setDownloadInfo] = useState<DownloadInfo | null>(null), [error, setError] = useState(''), [metadataError, setMetadataError] = useState('');
  const [person, setPerson] = useState<Person | null>(null), [fixing, setFixing] = useState(false), [revision, setRevision] = useState(0);
  useEffect(() => {
    if (item.kind === 'playlist') return;
    const controller = new AbortController();
    void api<Details>(fileUrl(source, item.path, 'details'), controller.signal).then(data => { if (!controller.signal.aborted) setDetails(data); }).catch(e => { if (!controller.signal.aborted) setMetadataError(message(e)); });
    void api<DownloadInfo>(fileUrl(source, item.path, 'download-info'), controller.signal).then(data => { if (!controller.signal.aborted) setDownloadInfo(data); }).catch(e => { if (!controller.signal.aborted) setError(message(e)); });
    return () => controller.abort();
  }, [source, item.path, item.kind, revision]);
  const canOptions=['movies','series','tv'].includes(item.kind);
  // The M3U detail sheet follows the shared contract: download settings are a
  // tab, and Fix match / cast biographies are in-sheet views with a back link.
  const downloadChoices=<Stack gap="lg"><Stack gap={4}><Title order={3} size="h4">Download this media</Title><Text size="sm" c="dimmed">{item.kind==='playlist'?'Export the playlist for your media app.':downloadInfo?.local?'This media is already saved locally.':'Save the video in ReelDock, or export its stream link for your media app.'}</Text></Stack>{error&&<Failure error={error}/>} {downloadInfo?.downloadAvailable&&!downloadInfo.local&&<ServerDownload csrf={downloadInfo.csrf} source={source} path={item.path}/>}<Paper withBorder p="md" radius="md"><Stack gap="sm"><Text fw={600} size="sm">Export or download to this device</Text><Group gap="sm"><LinkButton component="a" variant="default" href={fileUrl(source,item.path)}>Export {item.kind==='playlist'?'playlist':'.strm link'}</LinkButton>{item.kind!=='playlist'&&(downloadInfo?.downloadAvailable?<LinkButton component="a" variant="default" href={downloadInfo.download} download aria-label="Download video" leftSection={<Download size={18}/>}>Download video</LinkButton>:<Button variant="secondary" disabled title={downloadInfo?downloadInfo.downloadReason:'Loading download choices…'}><Download size={18}/>Download video</Button>)}</Group>{item.kind!=='playlist'&&!downloadInfo?.downloadAvailable&&<Text size="xs" c="dimmed">{downloadInfo?.downloadReason||'Checking download choices…'}</Text>}</Stack></Paper></Stack>;
  const overview=<Stack gap="lg"><Group><Badge variant="light">{item.kind==='playlist'?'M3U playlist':downloadInfo?.local?'Saved locally':'VOD media'}</Badge>{mediaCardLength(item)!==undefined&&<MediaLengthBadge length={mediaLength({...item,...details})}/>}</Group>{item.kind==='playlist'?<Text>Export this playlist from Downloads to open its channels in your media app.</Text>:<><Text style={{whiteSpace:'pre-line'}}>{details?.description||(metadataError?'Title information is currently unavailable.':details?'No description available.':'Loading title information…')}</Text>{metadataError&&<Failure error={metadataError}/>}<Facts facts={details?.facts||{}}/>{!!details?.people?.length&&<Stack gap="sm"><Title order={3}>Cast &amp; crew</Title><ScrollArea type="auto" scrollbars="x"><Group wrap="nowrap" align="flex-start" gap="md" pb="sm">{details.people.map(p=><ControlTrigger key={p.id} onClick={()=>setPerson(p)} aria-label={'Biography: '+p.name} w={100} style={{flexShrink:0}}><Stack gap={4}><Avatar src={p.image} size={100} radius="md"><UserRound size={32}/></Avatar><Text fw={600} size="sm" lineClamp={2}>{p.name}</Text><Text size="xs" c="dimmed" lineClamp={2}>{p.role}</Text></Stack></ControlTrigger>)}</Group></ScrollArea></Stack>}{details?.tmdb_url?.startsWith('https://www.themoviedb.org/')&&<Text size="xs" c="dimmed">Metadata and artwork: <Anchor href={details.tmdb_url} target="_blank" rel="noopener noreferrer">TMDB</Anchor></Text>}{details?.message&&<Text size="sm" c="dimmed">{details.message}</Text>}</>}</Stack>;
  const manage=<Stack gap="lg">{canOptions?<VodOptions embedded showFavourite={false} source={source} path={item.path}/>:<Text c="dimmed">Playlist connection settings are available in Settings → VOD connections.</Text>}{canOptions&&downloadInfo?.csrf&&<Paper withBorder p="md" radius="md"><Stack gap="sm"><Title order={3} size="h4">Title information</Title><Text size="sm" c="dimmed">Correct the match, artwork or your own title details.</Text><Group><Button variant="secondary" onClick={()=>setFixing(true)}>Fix match</Button></Group></Stack></Paper>}</Stack>;
  const tabs:DetailSheetTab[]=[{id:'overview',label:'Overview',content:overview},{id:'downloads',label:'Downloads',content:downloadChoices},{id:'manage',label:'Manage',content:manage},{id:'history',label:'History',content:<VodDownloadHistory source={source} path={item.path}/>}];
  const view=fixing&&downloadInfo?.csrf?<FixMatch embedded source={source} path={item.path} csrf={downloadInfo.csrf} onChanged={()=>setRevision(v=>v+1)}/>:person?<Biography key={person.id} person={person}/>:null;
  return <DetailSheet open onOpenChange={open=>{if(!open)onClose?.();}}
    banner={!view?details?.artwork||item.artwork||fileUrl(source,item.path,"artwork"):undefined}
    bannerFallback={!view?item.artwork:undefined}
    media={!view?<MediaArtwork src={details?.artwork||item.artwork||fileUrl(source,item.path,"artwork")} kind={item.kind} fit="contain"/>:undefined}
    title={person?person.name:fixing?'Fix match':details?.title||item.title}
    subtitle={person?person.role:view?details?.title||item.title:'M3U library'}
    actions={!view&&canOptions&&<QuickDownload actions={<TileActions resolve={{source,path:item.path}} label={item.title}/>} item={{kind:'vod',source,path:item.path}} csrf={downloadInfo?.csrf||''} disabled={!downloadInfo||downloadInfo.local||!downloadInfo.downloadAvailable} disabledReason={!downloadInfo?'Loading download choices…':downloadInfo.local?'Already saved locally':!downloadInfo.downloadAvailable?downloadInfo.downloadReason:undefined} onChoices={()=>setTab('downloads')}/>}
    tabs={view?undefined:tabs}
    tab={tab}
    onTab={setTab}
    back={view?{label:details?.title||item.title,onBack:()=>{setFixing(false);setPerson(null);}}:undefined}
  >{view}</DetailSheet>;
}
function VodDownloadHistory({source,path}:{source:string;path:string}){
 const jobs=useM3uDownloads();
 const entries=jobs.data.filter(job=>job.source===source&&job.path===path).sort((a,b)=>b.updated-a.updated);
 return <Stack gap="md">{jobs.error&&<Failure error={jobs.error}/>} {!entries.length&&!jobs.error&&<Text c="dimmed">No recorded downloads for this media.</Text>}{entries.map(job=><Paper key={job.id} withBorder p="md" radius="md"><Stack gap={4}><Group justify="space-between"><Text fw={600} size="sm">{job.title}</Text><Badge variant="light">{job.state}</Badge></Group><Text size="xs" c="dimmed">{job.updated?new Date(job.updated*1000).toLocaleString('en-GB'):''}</Text>{job.message&&<Text size="sm">{job.message}</Text>}{job.destination&&<Text size="xs" c="dimmed" style={{overflowWrap:'anywhere'}}>{job.destination}</Text>}</Stack></Paper>)}</Stack>;
}
function Biography({ person }: { person: Person }) {
  const [data, setData] = useState<{ biography: string; facts: Record<string, string>; tmdb_url?: string } | null>(null), [error, setError] = useState('');
  useEffect(() => { const controller = new AbortController(); api<{ biography: string; facts: Record<string, string>; tmdb_url?: string }>('/api/m3u-person/' + person.id, controller.signal).then(setData).catch(e => { if (!controller.signal.aborted) setError(message(e)); }); return () => controller.abort(); }, [person.id]);
  return <Stack gap="md">{person.image&&<Image src={person.image} alt="" w={120} radius="md"/>}{error?<Failure error={error}/>:!data?<Text role="status">Loading biography…</Text>:<><Text style={{whiteSpace:'pre-line'}}>{data.biography}</Text><Facts facts={data.facts}/><Text size="xs" c="dimmed">Biography: <Anchor href={'https://www.themoviedb.org/person/'+person.id} target="_blank" rel="noopener noreferrer">TMDB</Anchor></Text></>}</Stack>;
}
