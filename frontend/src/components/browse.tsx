import LiveArtwork from '../LiveArtwork';
import type { ComponentProps, ReactNode } from 'react';
import { ActionIcon, AspectRatio, Badge, Box, Card, Center, Grid, Group, Image, Paper, SimpleGrid, Stack, Text, TextInput, type BadgeProps } from '@mantine/core';
import { SelectField } from './SelectField';
import { Film, Search, X } from 'lucide-react';
import surfaces from './MediaSurfaces.module.css';

export type FilterOption = { value: string; label: string; count?: number };
export function FilterBar({search,onSearch,searchLabel='Search',searchPlaceholder='Search…',genres,genre,onGenre,genreLabel='Genre',allGenresLabel='All genres',sorts,sort,onSort,sortLabel='Sort',extra,className}:{search?:string;onSearch?:(value:string)=>void;searchLabel?:string;searchPlaceholder?:string;genres?:FilterOption[];genre?:string;onGenre?:(value:string)=>void;genreLabel?:string;allGenresLabel?:string;sorts?:FilterOption[];sort?:string;onSort?:(value:string)=>void;sortLabel?:string;extra?:ReactNode;className?:string}) {
 if(!onSearch&&!genres&&sorts&&onSort)return <Group data-filter-controls justify="flex-end" align="flex-start" mih={44} gap="sm" className={className}>{extra}<Box data-filter-sort w={{base:'100%',sm:208,md:220}} maw="100%" mih={44}><SelectField aria-label={sortLabel} value={sort} onChange={event=>onSort(event.currentTarget.value)} data={sorts}/></Box></Group>;
 return <Paper withBorder p="md" radius="md" className={className}><Grid gap="sm" align="flex-start">
  {onSearch&&<Grid.Col span={{base:12,sm:genres?6:8}}><TextInput type="search" aria-label={searchLabel} placeholder={searchPlaceholder} value={search} onChange={e=>onSearch(e.currentTarget.value)} leftSection={<Search size={18}/>} rightSection={search?<ActionIcon variant="subtle" aria-label={'Clear '+searchLabel.toLowerCase()} onClick={()=>onSearch('')}><X size={16}/></ActionIcon>:undefined}/></Grid.Col>}
  {genres&&onGenre&&<Grid.Col span={{base:6,sm:3}}><SelectField aria-label={genreLabel} value={genre} onChange={e=>onGenre(e.currentTarget.value)} data={[{value:'',label:allGenresLabel},...genres.map(option=>({value:option.value,label:option.count==null?option.label:option.label+' ('+option.count+')'}))]}/></Grid.Col>}
  {sorts&&onSort&&<Grid.Col span={{base:genres?6:12,sm:onSearch?(genres?3:4):genres?6:12}}><SelectField aria-label={sortLabel} value={sort} onChange={e=>onSort(e.currentTarget.value)} data={sorts}/></Grid.Col>}
  {extra&&<Grid.Col span={12}><Group data-filter-controls align="flex-start" mih={44} gap="sm">{extra}</Group></Grid.Col>}
 </Grid></Paper>;
}
export function MediaGrid({children,label,className,...rest}:{children:ReactNode;label?:string}&ComponentProps<'ul'>){
 return <SimpleGrid cols={{base:2,md:3,xl:4}} spacing={{base:'sm',sm:'md'}} verticalSpacing="lg" className={className} aria-label={label} m={0} p={0} style={{listStyle:'none'}} renderRoot={props=><ul {...props} {...rest}/>}>{children}</SimpleGrid>;
}
export function MediaArtwork({src,alt='',kind='movie',badge,fit='cover'}:{src?:string;alt?:string;kind?:string;badge?:ReactNode;fit?:'cover'|'contain'}){
 return <AspectRatio ratio={2/3}><Box pos="relative" bg="var(--mantine-color-default)" c="dimmed" style={{overflow:'hidden',borderRadius:'var(--mantine-radius-sm)'}}><Box pos="absolute" inset={0}>{src?<LiveArtwork src={src} fit={fit} placeholder={<Film size={36} aria-hidden="true"/>}/>:<Center h="100%"><Film size={36} aria-hidden="true"/></Center>}</Box>{badge&&<Badge pos="absolute" top={8} left={8} variant="filled">{badge}</Badge>}{kind==='channel'&&<Badge data-media-kind-caption pos="absolute" bottom={8} left={8} maw="calc(100% - 16px)" size="sm" variant="filled" autoContrast>YouTube channel</Badge>}</Box></AspectRatio>;
}
export function MediaLengthBadge({length,...props}:Omit<BadgeProps,'children'>&{length:string}){
 return <Badge size="sm" variant="filled" autoContrast {...props} data-media-duration aria-label={length.startsWith('Length')?length:'Length '+length}>{length}</Badge>;
}
export function MediaCard({art,title,channel,meta,status,badge,length,actions,active,className,artClassName,titleClassName,children,layout="grid",...rest}:{art:ReactNode;title:ReactNode;channel?:ReactNode;meta?:ReactNode;status?:ReactNode;badge?:ReactNode;length?:string;actions?:ReactNode;active?:boolean;className?:string;artClassName?:string;titleClassName?:string;children?:ReactNode;layout?:"grid"|"list"}&Omit<ComponentProps<'li'>,'title'>){
 return <Card component="li" withBorder radius="md" p="sm" className={[surfaces.surface,surfaces.tile,className].filter(Boolean).join(' ')} {...rest} data-media-surface="tile" data-media-active={active||undefined} style={{display:'flex',minWidth:0,outline:active?'2px solid var(--mantine-primary-color-filled)':undefined,...rest.style}}>
  <Box style={{display:'flex',flexDirection:layout==='list'?'row':'column',gap:'var(--mantine-spacing-sm)',height:'100%',minWidth:0}}>
   <Box data-media-artwork pos="relative" className={artClassName} w={layout==='list'?100:'100%'} style={{flexShrink:0}}>{art}{badge&&<Box pos="absolute" top={8} left={8}>{badge}</Box>}{length&&<MediaLengthBadge length={length} pos="absolute" bottom={8} right={8} maw="calc(100% - 16px)"/>}</Box>
   <Stack gap="sm" style={{minWidth:0,flex:1}}><Box data-media-title className={titleClassName} fw={600} h={44} style={{overflowWrap:'anywhere',overflow:'hidden'}}>{title}</Box>
   {channel&&<Box data-media-channel style={{minWidth:0}}>{channel}</Box>}
   {meta&&<Text data-media-meta component="div" size="sm" c="dimmed" lineClamp={2} style={{minWidth:0,overflowWrap:'anywhere'}}>{meta}</Text>}
   {status&&<Box data-media-status fz="xs" c="dimmed" style={{minWidth:0,overflowWrap:'anywhere'}}>{status}</Box>}
   {children}
   {actions&&<Group data-media-actions gap="xs" mt="auto" pt="xs" justify="flex-end">{actions}</Group>}</Stack>
  </Box>
 </Card>;
}
