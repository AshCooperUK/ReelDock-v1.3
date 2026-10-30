import { UnstyledButton as ControlTrigger } from '@mantine/core';
import { NotificationMessage } from './notifications';
import { TextInput } from './TextField';
import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';
import { Button, Switch } from './components/ui';
import { Badge, Box, Grid, Group as MantineGroup, Image, Paper, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { MediaArtwork, MediaCard, MediaGrid } from './components/browse';
import { mediaCardLength, type MediaDuration } from './mediaDuration';
import TileActions from './collections/TileActions';
import HoverCard, { useCardHover, titleHover } from './HoverCard';

type Kind = 'movie' | 'show' | 'video' | 'channel';
export type DiscoverItem = MediaDuration & {
  id: string; kind: Kind; title: string; image?: string; backdrop?:string; releaseDate?:string; year?: string; provider?: string;
  externalId?: string; imdbId?: string; videoId?: string; channelId?: string; channel?: string; description?: string; genre?: string; rating?: string;
  url?: string; public?: boolean; live?: boolean; rank?: number; views?: number; dailyViews?: number;
  subscribers?: number; growth?: number; growthDays?: number; growthChart?: boolean;
  country?: string; published?: string; duration?: string; releases?: Release[];
};
type Release = { id: string; title: string; quality: string; size: number; published?: string; provider?: string };
type Group = { preloaded?: boolean; partial?: boolean; title: string; subtitle?: string; items: DiscoverItem[]; error?: string; warning?: string; preview?: boolean; scope?: string; source?: string; sourceUrl?: string; updatedAt?: string | null; fetchedAt?: string };
type Prefs = { layout: 'grid' | 'shelves' | 'list'; density: 'comfortable' | 'compact'; quality: string; fallback: boolean; source: string };
type Episode = { id: number | string; name: string; season: number; number: number | null; airdate?: string; runtime?: number; summary?: string; image?: string; type?: string };

export type DiscoverPreview = { charts: Record<string, Group>; feeds: Record<string, Group>; preferences: Prefs; viewMedia?: import('./viewMedia/types').MediaPage | null };

const compact = (value: number) => new Intl.NumberFormat('en-GB', { notation: 'compact', maximumFractionDigits: 1 }).format(value);

function metric(item: DiscoverItem) {
  if (item.growthChart) return item.growth && item.growth > 0 ? `+${item.growth} rank places · ${item.growthDays} days` : `No rank change · ${item.growthDays} days`;
  if (item.dailyViews != null) return compact(item.dailyViews) + ' views / day';
  if (item.subscribers != null) return compact(item.subscribers) + ' subscribers';
  if (item.views != null) return compact(item.views) + ' views';
  if (item.published) return new Date(item.published).toLocaleDateString('en-GB');
  return 'Public YouTube video';
}

/** Date-only release metadata follows the app's UK calendar, not UTC midnight. */
const releaseCalendar = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit' });
let calendarMinute = -1, calendarDay = '';
export function isUnreleased(value?: string, year?: string, now = new Date()): boolean {
  if (!value && !year) return false;
  const minute = Math.floor(now.getTime() / 60000);
  if (minute !== calendarMinute) {
    const parts = releaseCalendar.formatToParts(now);
    const part = (name: string) => parts.find(entry => entry.type === name)?.value || '';
    calendarDay = `${part('year')}-${part('month')}-${part('day')}`; calendarMinute = minute;
  }
  const today = calendarDay;
  const day = value?.slice(0, 10) || '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(day)) {
    const parsed = new Date(day + 'T00:00:00Z');
    if (!Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === day) return day > today;
  }
  // A catalogue year alone cannot establish a release day within this year.
  return /^\d{4}$/.test(year || '') && Number(year) > Number(today.slice(0,4));
}

function Poster({item,detail=false}:{item:DiscoverItem;detail?:boolean}){return <MediaArtwork src={item.image||item.backdrop} alt={detail?item.title+' artwork':''} kind={item.kind} badge={isUnreleased(item.published,item.year)?'Not released yet':!detail?(item.public?(item.rank?'#'+item.rank:'YouTube'):item.kind==='show'?'TV show':item.kind==='movie'?'Movie':'YouTube'):undefined}/>;}
const DiscoverCard=memo(function DiscoverCard({item,unreleased,onSelect}:{item:DiscoverItem;unreleased:boolean;onSelect:(item:DiscoverItem)=>void}){
 const hover=useCardHover<HTMLLIElement>();return <MediaCard length={mediaCardLength(item)} ref={hover.anchor} {...titleHover(hover)} art={<ControlTrigger type="button" w="100%" style={{display:'block'}} aria-label={'View '+item.title} onClick={()=>onSelect(item)}><Poster item={item}/></ControlTrigger>} title={<ControlTrigger w="100%" mih={44} onClick={()=>onSelect(item)} style={{textAlign:'left'}}><Text fw={600} lineClamp={2} className="hover-card-title">{item.title}</Text></ControlTrigger>} meta={item.public?metric(item):item.channel||[item.year,item.rating?item.rating+' IMDb':'',item.genre].filter(Boolean).join(' · ')||item.provider||'Catalogue'} status={unreleased?'Not released yet':undefined} actions={<TileActions item={item} inline/>}><HoverCard hover={hover} info={{title:item.title,image:item.image,backdrop:item.backdrop,kind:item.kind==='show'?'TV show':item.kind==='movie'?'Movie':item.kind==='channel'?'Channel':'YouTube',year:item.year,rating:item.rating,date:item.published?.slice(0,10),channel:item.channel,channelId:item.channelId,description:item.description}}/></MediaCard>;
});
export function Collection({group,layout,overview,onSelect,onLoadMore,loadingMore=false}:{group:Group;layout:string;overview?:boolean;onSelect:(item:DiscoverItem)=>void;onLoadMore?:()=>void;loadingMore?:boolean}){
 const [limit,setLimit]=useState(overview?30:24);const sentinel=useRef<HTMLDivElement>(null);const shown=group.items.slice(0,limit),hasMore=limit<group.items.length||!!onLoadMore;
 useEffect(()=>{if(!hasMore||loadingMore||!sentinel.current)return;const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting)){if(limit<group.items.length)setLimit(current=>Math.min(current+24,group.items.length));else onLoadMore?.();}},{rootMargin:'300px 0px'});observer.observe(sentinel.current);return()=>observer.disconnect();},[hasMore,group.items.length,limit,onLoadMore,loadingMore]);
 return <Stack component="section" gap="md"><MantineGroup justify="space-between"><Stack gap={4}><Title order={2} size="h3">{group.title}</Title>{group.subtitle&&<Text size="sm" c="dimmed">{group.subtitle}</Text>}</Stack><Badge variant="light">{group.items.length} titles</Badge></MantineGroup>{group.error&&<NotificationMessage kind="error" message={group.error}/>}<MediaGrid label={group.title}>{shown.map(item=><DiscoverCard key={item.id} item={item} unreleased={isUnreleased(item.published,item.year)} onSelect={onSelect}/>)}</MediaGrid>{hasMore&&<Box ref={sentinel} h={1} aria-hidden="true"/>}</Stack>;
}

export function selectableEpisode(episode: Episode) {
  return Number.isInteger(episode.season) && episode.season >= 0 && Number.isInteger(episode.number) && (episode.number ?? 0) > 0;
}

export function episodeGroup(episode: Episode) { return episode.number == null || episode.type?.includes('special') ? 0 : episode.season; }

export function SeasonBlock({ manual=false, number, episodes, count, selected, onSeason, onToggle, onSpecial }: { manual?:boolean; number: number; episodes: Episode[]; count: number; selected: Set<number | string>; onSeason: (checked: boolean) => void; onToggle: (episode: Episode) => void; onSpecial?: (episode: Episode) => void }) {
  const [expanded, setExpanded] = useState(false);
  const eligible = manual?0:episodes.filter(selectableEpisode).length;
  return <Paper component="section" withBorder p="md" radius="md"><Stack gap="md"><MantineGroup justify="space-between"><MantineGroup gap="sm">{!manual&&<Switch label={number===0?'Select all specials':'Select season '+number} disabled={!eligible} checked={eligible>0&&count===eligible} onCheckedChange={onSeason}/>}<Title order={3}>{number===0?'Specials':'Season '+number}</Title><Badge variant="light">{manual?episodes.length+' episodes':count+' / '+episodes.length+' selected'}</Badge></MantineGroup><Button variant="secondary" aria-expanded={expanded} onClick={()=>setExpanded(value=>!value)}>{expanded?'Hide episodes':'Show episodes'}</Button></MantineGroup>{!episodes.length&&<Text size="sm">No episodes have been listed for this season yet.</Text>}
   {expanded&&episodes.map(episode=><Paper withBorder p="sm" radius="md" key={episode.id}><Stack gap="sm"><MantineGroup align="flex-start" wrap="nowrap">{!manual&&<Switch label={episode.name} disabled={manual||!selectableEpisode(episode)} checked={selected.has(episode.id)} onCheckedChange={()=>onToggle(episode)}/>} {episode.image&&<Image src={episode.image} alt="" loading="lazy" w={64} h={48} radius="sm"/>}<Stack gap={4} style={{flex:1,minWidth:0}}><Text fw={600} size="sm">{episode.number==null?'Special':String(episode.number).padStart(2,'0')} · {episode.name}</Text><Text size="xs" c="dimmed">{[episode.airdate||'Air date to be announced',episode.runtime?episode.runtime+' min':'',episode.type?.includes('special')?'Special':''].filter(Boolean).join(' · ')}</Text>{!selectableEpisode(episode)&&<Text size="xs" c="yellow">Unnumbered special · choose a release manually</Text>}{isUnreleased(episode.airdate)&&<Badge variant="light" color="yellow">Not released yet</Badge>}{episode.summary&&<Text size="sm" c="dimmed" lineClamp={3}>{episode.summary}</Text>}</Stack></MantineGroup>{(manual||!selectableEpisode(episode))&&onSpecial&&<MantineGroup justify="flex-end"><Button variant="secondary" onClick={()=>onSpecial(episode)}>Search releases</Button></MantineGroup>}</Stack></Paper>)}
  </Stack></Paper>;
}

export function youtubeMediaQuery(item: DiscoverItem, episode?: Episode): string {
  const suffix = episode ? ` S${String(episode.season).padStart(2, '0')}E${String(episode.number ?? '').padStart(2, '0')} ${episode.name}` : item.year ? ` ${item.year} full movie` : ' full movie';
  return (item.title.slice(0, Math.max(2, 160 - suffix.length)) + suffix).slice(0, 160).trim();
}

export function ProviderSearch({ section, layout, onSelect, initialQuery = '' }: { section: string; layout: string; onSelect: (item: DiscoverItem) => void; initialQuery?: string }) {
  const provider = section === 'Films' ? 'movies' : section === 'TV shows' ? 'shows' : 'youtube';
  const [query, setQuery] = useState(initialQuery);
  const [submitted, setSubmitted] = useState('');
  const [result, setResult] = useState<{ items: DiscoverItem[]; total: number | null; nextOffset: number | null; scope: string; releaseCount?: number | null; warnings?: string[]; unidentified?: { id: string; title: string; provider: string }[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  const busy = useRef(false);
  const load = useCallback(async (term: string, offset = 0) => {
    if (offset && busy.current) return;
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    busy.current = true; setLoading(true); setError('');
    if (!offset) { setResult(null); setSubmitted(term); }
    try {
      const data = await api<NonNullable<typeof result>>('/api/discover/search?' + new URLSearchParams({ provider, q: term, offset: String(offset) }), request.signal);
      if (request.signal.aborted) return;
      setResult(previous => {
        if (!offset || !previous) return data;
        const merged = new Map(previous.items.map(item => [item.id, item]));
        data.items.forEach(item => {
          const old = merged.get(item.id);
          merged.set(item.id, old ? { ...old, releases: [...new Map([...(old.releases || []), ...(item.releases || [])].map(release => [release.id, release])).values()] } : item);
        });
        return { ...data, items: [...merged.values()], unidentified: [...new Map([...(previous.unidentified || []), ...(data.unidentified || [])].map(row => [row.id, row])).values()], warnings: [...new Set([...(previous.warnings || []), ...(data.warnings || [])])], releaseCount: (previous.releaseCount || 0) + (data.releaseCount || 0) };
      });
    } catch (exc) {
      if (!request.signal.aborted) setError(exc instanceof Error ? exc.message : 'Search failed.');
    } finally {
      if (!request.signal.aborted) { busy.current = false; setLoading(false); }
    }
  }, [provider]);
  useEffect(() => { if (initialQuery) void load(initialQuery); return () => controller.current?.abort(); }, [initialQuery, load]);
  return <Stack component="section" gap="lg"><Paper withBorder p="md" radius="md"><MantineGroup component="form" wrap="nowrap" onSubmit={event=>{event.preventDefault();void load(query.trim());}}><TextInput style={{flex:1,minWidth:0}} onClear={()=>{controller.current?.abort();busy.current=false;setLoading(false);setQuery('');setSubmitted('');setResult(null);setError('');}} aria-label="Search provider" type="search" minLength={2} maxLength={160} required value={query} onChange={event=>setQuery(event.target.value)} placeholder={provider==='youtube'?'Search YouTube videos…':'Search the NZB catalogue for '+section.toLowerCase()+'…'}/><Button type="submit" disabled={query.trim().length<2}>Search</Button></MantineGroup></Paper>
   {!submitted&&<Paper withBorder p="lg" radius="md"><Stack gap="sm"><Title order={2}>Search beyond the charts</Title><Text c="dimmed">{provider==='youtube'?'Find a public video, then download that video only.':'Search all enabled indexers. Uncertain matches are listed separately for review.'}</Text></Stack></Paper>}
   {result&&<><Text size="sm" c="dimmed">{result.scope}{result.releaseCount!=null?' · '+result.releaseCount+' releases loaded':''}</Text><Collection group={{title:'Results for “'+submitted+'”',subtitle:result.total==null?'':result.total+' matching releases',items:result.items}} layout={layout} onSelect={onSelect} loadingMore={loading} onLoadMore={!error&&result.nextOffset!=null?()=>void load(submitted,result.nextOffset!):undefined}/>{!!result.unidentified?.length&&<Stack gap="sm"><Title order={3}>Needs identification</Title><Text size="sm" c="dimmed">These releases were found, but could not be confidently matched to a title. They have not been queued.</Text>{result.unidentified.map(row=><Paper key={row.id} withBorder p="md" radius="md"><Text fw={600}>{row.title}</Text><Text size="sm" c="dimmed">{row.provider}</Text></Paper>)}</Stack>}{result.warnings?.map(warning=><NotificationMessage kind="warning" message={warning} key={warning}/>)}{!result.items.length&&!result.unidentified?.length&&<Text c="dimmed">No releases found. Try another title.</Text>}</>}
   {error&&<NotificationMessage kind="error" message={error} actions={<Button variant="secondary" onClick={()=>void load(submitted,result?.nextOffset||0)}>Retry search</Button>}/>} {loading&&<Text role="status">{result?'Loading more results…':'Searching…'}</Text>}
  </Stack>;
}
