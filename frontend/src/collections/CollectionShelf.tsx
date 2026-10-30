import { UnstyledButton as ControlTrigger } from '@mantine/core';
import React,{useEffect,useRef,useState} from 'react';
import { Badge, Box, Center, Group, Paper, Stack, Text, Title } from '@mantine/core';
import { SelectField as NativeSelect } from '../components/SelectField';
import {Bookmark,BookmarkCheck,Film,Heart,Star,Tv,Youtube} from 'lucide-react';
import {Button} from '../components/ui';
import {MediaArtwork,MediaCard,MediaGrid} from '../components/browse';
import ChannelIdentity from '../ChannelIdentity';
import VideoTitle from '../VideoTitle';
import type {DiscoverItem} from '../Discover';
import type {EpisodeEntry} from '../discovery/DiscoveryTitle';
import TileActions from './TileActions';
import { mediaCardLength } from '../mediaDuration';
import { mediaDate } from '../mediaDate';
import HoverCard,{useCardHover,titleHover} from '../HoverCard';
export type CollectionItem=DiscoverItem&{publishedApproximate?:boolean;watchlistKey?:string;watchlisted?:boolean;watchStatus?:string;watchDate?:string;episodeKey?:string;episodeLabel?:string;episode?:EpisodeEntry;show?:DiscoverItem;genres?:string[];avatar?:string;banner?:string;date?:string;indexedVideos?:number;latestUpload?:string;collectionKey?:string;automatic?:boolean;downloadFuture?:boolean;downloadAll?:boolean;automaticSupported?:boolean;favourite?:boolean};
export type CollectionSort='az'|'za';
const alphabet=['0-9',...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'];
const compare=new Intl.Collator('en-GB',{numeric:true,sensitivity:'base',ignorePunctuation:true});
export function letter(title:string){const first=title.normalize('NFKD').replace(/[\u0300-\u036f]/g,'').replace(/^[^a-z0-9]+/i,'')[0]?.toUpperCase()||'0';return /[0-9]/.test(first)?'0-9':/[A-Z]/.test(first)?first:'0-9';}
export function ordered(items:CollectionItem[]){return [...items].sort((a,b)=>alphabet.indexOf(letter(a.title))-alphabet.indexOf(letter(b.title))||compare.compare(a.title,b.title)||a.id.localeCompare(b.id));}
export function itemGenres(item:CollectionItem){const names=new Map<string,string>();for(const value of [...(item.genres||[]),...(item.genre||'').split(',')]){const name=value.trim().replace(/\s+/g,' ');if(name)names.set(name.toLocaleLowerCase('en-GB'),name);}return [...names].map(([value,label])=>({value,label}));}
export function genreOptions(items:CollectionItem[]){const counts=new Map<string,{value:string;label:string;count:number}>();let unknown=0;for(const item of items){const genres=itemGenres(item);if(!genres.length)unknown++;for(const genre of genres){const prior=counts.get(genre.value);counts.set(genre.value,{...genre,count:(prior?.count||0)+1});}}const options=[...counts.values()].sort((a,b)=>compare.compare(a.label,b.label));if(unknown)options.push({value:'__uncategorised',label:'Uncategorised',count:unknown});return options;}
export function filterGenre(items:CollectionItem[],value:string){return !value?items:items.filter(item=>{const genres=itemGenres(item);return value==='__uncategorised'?genres.length===0:genres.some(genre=>genre.value===value);});}
// Page-level FilterBar search: client-side title (and channel) match, every
// word must appear so multi-word searches narrow instead of failing.
export function matchesQuery(item:CollectionItem,query:string){const fold=(value:string)=>value.normalize('NFKD').replace(/[\u0300-\u036f]/g,'').toLocaleLowerCase('en-GB');const hay=fold([item.title,item.channel].filter(Boolean).join(' '));return fold(query).split(/\s+/).filter(Boolean).every(word=>hay.includes(word));}
function stamp(value?:string){if(!value)return '';const date=new Date(value+'T12:00:00Z');return Number.isFinite(date.getTime())?new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',year:'numeric'}).format(date):'';}
const rating=(value?:string)=>value?Number(value).toLocaleString('en-GB',{maximumFractionDigits:1}):'';
function ShelfCard({item,kind,open,onRemove,removing,onFavourite,favouriting,active,csrf,collection}:{item:CollectionItem;kind:string;open:(item:CollectionItem)=>void;onRemove?:(item:CollectionItem)=>void;removing:string[];onFavourite?:(item:CollectionItem)=>void;favouriting:string[];active:boolean;csrf?:string;collection:'favourites'|'watchlist'}){
 const hover=useCardHover<HTMLLIElement>();
 const info={title:item.title,image:item.image||item.banner,backdrop:item.backdrop||item.banner,kind:item.episodeLabel?'Episode':kind==='channel'?'Channel':kind==='video'?'YouTube':kind==='show'?'TV show':'Movie',year:item.year,rating:rating(item.rating),date:(item.published||item.date)?.slice(0,10),channel:item.channel,channelId:item.channelId||(kind==='channel'?item.id:''),avatar:item.avatar,description:item.description||item.genre};
 const meta=kind==='video'?(mediaDate(item.published||item.date)||'Date unavailable'):kind==='channel'?(item.indexedVideos||0).toLocaleString()+' indexed videos':collection==='watchlist'&&item.watchStatus?item.watchStatus+(item.watchDate?' · '+stamp(item.watchDate):''):item.episodeKey?'Episode · '+(stamp(item.episode?.date)||'Date unavailable'):[item.year,kind==='show'?'TV Show':'Movie'].filter(Boolean).join(' · ');
 const status=kind==='video'?(collection==='watchlist'&&item.watchStatus?item.watchStatus+(item.watchDate?' · '+stamp(item.watchDate):''):undefined):kind==='channel'?(collection==='watchlist'&&item.watchStatus?item.watchStatus:item.latestUpload?'Latest upload · '+stamp(item.latestUpload):undefined):item.episodeKey?(item.episode?.file?'Downloaded with ReelDock':item.episode?.requested?item.episode.status:item.episode?.date&&item.episode.date>new Date().toLocaleDateString('en-CA',{timeZone:'Europe/London'})?'Scheduled air date':'Not downloaded'):kind==='show'?(item.downloadFuture?'Future downloads on':collection==='watchlist'&&item.downloadAll?'Download All Seasons on':collection==='watchlist'&&item.automatic?'Automatic downloads on':'Future downloads off'):(item.automatic?'Automatic download on':'Automatic download off');
 return <MediaCard length={mediaCardLength({...item,kind})} ref={hover.anchor} active={active} data-initial={letter(item.title)} {...titleHover(hover)}
  art={<ControlTrigger w="100%" style={{display:"block"}} aria-label={(kind==='channel'?'Channel information: ':'Open ')+item.title} onClick={()=>open(item)}><CollectionArtwork item={item} mark={false}/>{item.episodeLabel&&<Badge pos="absolute" bottom={36} left={8} maw="calc(100% - 16px)" size="sm">{item.episodeLabel}</Badge>}</ControlTrigger>}

  titleClassName="hover-card-title"
  title={kind==='channel'?<ChannelIdentity id={item.id} name={item.title} avatar={item.avatar}/>:kind==='video'?<VideoTitle length={item} id={item.videoId||item.id} title={item.title} channel={item.channelId} channelName={item.channel} avatar={item.avatar} poster={item.image} csrf={csrf}/>:<ControlTrigger w="100%" mih={44} style={{textAlign:"left",fontWeight:600}} onClick={()=>open(item)}>{item.title}</ControlTrigger>}
  channel={kind==='video'?<ChannelIdentity id={item.channelId} name={item.channel||'Unknown channel'} avatar={item.avatar}/>:undefined}
  meta={meta}
  status={status}
  actions={(onRemove||onFavourite)?<Group gap="xs">{onFavourite&&<Button iconOnly variant="secondary" aria-label={(item.favourite?'Remove from favourites':'Add to favourites')+' · '+item.title} title={item.favourite?'Remove from favourites':'Add to favourites'} aria-pressed={!!item.favourite} disabled={favouriting.includes(item.watchlistKey||'')} onClick={()=>onFavourite(item)}><Heart size={18} fill={item.favourite?'currentColor':'none'}/></Button>}{onRemove&&<Button iconOnly variant="secondary" aria-label={'Remove from Watchlist · '+item.title} title="Remove from Watchlist and turn off future automatic downloads." disabled={removing.includes(item.watchlistKey||'')} onClick={()=>onRemove(item)}><BookmarkCheck size={18}/></Button>}</Group>:<TileActions item={item}/>}>
  <HoverCard hover={hover} info={info}/>
 </MediaCard>;
}
export function CollectionArtwork({item,mark=true}:{item:CollectionItem;mark?:boolean}){
 return <MediaArtwork src={item.kind==='channel'?(item.avatar||item.image||item.banner):(item.image||item.backdrop||item.banner)} kind={item.kind} badge={mark?<Heart size={14} fill="currentColor"/>:item.rating?'★ '+rating(item.rating):undefined}/>;
}

export default function CollectionShelf({id,title,items,kind,open,extra,genreFilter=false,collection='favourites',onRemove,removing=[],onFavourite,favouriting=[],csrf,query='',sort='az',emptyNoun}:{id:string;title:string;items:CollectionItem[];kind:CollectionItem['kind'];open:(item:CollectionItem)=>void;extra?:React.ReactNode;genreFilter?:boolean;collection?:'favourites'|'watchlist';onRemove?:(item:CollectionItem)=>void;removing?:string[];onFavourite?:(item:CollectionItem)=>void;favouriting?:string[];csrf?:string;query?:string;sort?:CollectionSort;emptyNoun?:string}){
 const row=useRef<HTMLUListElement>(null);const [active,setActive]=useState('');const [genre,setGenre]=useState('');
 const options=genreOptions(items),selectedGenre=genreFilter&&options.some(option=>option.value===genre)?genre:'';
 const filtered=filterGenre(items,selectedGenre).filter(item=>matchesQuery(item,query));
 const sorted=ordered(filtered);if(sort==='za')sorted.reverse();
 const keys=new Set(sorted.map(item=>letter(item.title)));
 const noun=emptyNoun||(kind==='show'?'TV shows':kind==='video'?'videos':kind==='channel'?'channels':'movies');
 useEffect(()=>{if(genre&&!options.some(option=>option.value===genre)){setGenre('');setActive('');}},[items,genre]);
 useEffect(()=>{setActive('');},[query,sort]);
 function jump(value:string){const el=row.current;const index=sorted.findIndex(item=>letter(item.title)===value);if(!el||index<0)return;setActive(value);el.children[index]?.scrollIntoView({block:'nearest',inline:'nearest',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});}
 return <Stack component="section" gap="md" aria-labelledby={id+'-title'}>
  <Group component="header" justify="space-between" align="center" gap="sm">
   <Group gap="sm"><Title order={2} size="h3" id={id+'-title'}>{title}</Title><Badge variant="light">{selectedGenre||query.trim()?sorted.length.toLocaleString()+' / '+items.length.toLocaleString():sorted.length.toLocaleString()}</Badge></Group>
   <Group gap="sm" style={{flexWrap:'wrap'}}>
    {extra}
    {genreFilter&&<NativeSelect aria-label={title+' genre'} title={kind==='channel'||kind==='video'?'Automatic genres from saved channel information':'Filter by genre'} value={selectedGenre} disabled={!items.length} onChange={e=>{setGenre(e.target.value);setActive('');}} data={[{value:'',label:'All genres'},...options.map(option=>({value:option.value,label:option.label+' ('+option.count+')'}))]} style={{flex:'1 1 150px'}}/>}
    <NativeSelect aria-label={title+' alphabet'} value={active} onChange={e=>jump(e.target.value)} disabled={!sorted.length} data={[{value:'',label:'Jump to letter'},...alphabet.map(value=>({value,label:value,disabled:!keys.has(value)}))]} style={{flex:'1 1 145px'}}/>
   </Group>
  </Group>
  <MediaGrid ref={row} label={title+' '+collection}>
   {sorted.length?sorted.map(item=><ShelfCard key={item.id} item={item} kind={kind} open={open} onRemove={onRemove} removing={removing} onFavourite={onFavourite} favouriting={favouriting} active={active===letter(item.title)} csrf={csrf} collection={collection}/>):<Paper component="li" withBorder p="lg" radius="md" style={{gridColumn:"1 / -1"}}><Group align="flex-start">{collection==='watchlist'?<Bookmark size={22}/>:<Heart size={22}/>}<div><strong>{query.trim()?'No '+noun+' match this search.':collection==='watchlist'?'No '+noun+' on your Watchlist yet':<>No favourite {noun} yet</>}</strong>{!query.trim()&&<Text size="sm">{collection==='watchlist'?'Use Add to Watchlist in a media popup.':kind==='video'?'Choose individual videos with Add videos.':'Mark a favourite in ReelDock to see it here.'}</Text>}</div></Group></Paper>}
  </MediaGrid>
 </Stack>;
}
