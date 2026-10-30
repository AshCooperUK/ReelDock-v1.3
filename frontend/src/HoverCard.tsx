import surfaces from './components/MediaSurfaces.module.css';
import { useEffect, useLayoutEffect, useRef, useState, type FocusEvent, type MouseEvent, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { Film } from 'lucide-react';
import ChannelIdentity from './ChannelIdentity';
import { AspectRatio, Box, Center, Image, Paper, Stack, Text } from '@mantine/core';

export type HoverInfo = {
 title: string; image?: string; backdrop?: string; kind?: string; year?: string;
 rating?: string | number; date?: string; channel?: string; channelId?: string; avatar?: string; description?: string;
};

export type CardHover<T extends HTMLElement = HTMLElement> = {
 anchor: RefObject<T | null>; titleEl: RefObject<HTMLElement | null>; open: boolean;
 show: () => void; hide: () => void;
 popup?: RefObject<HTMLDivElement | null>; keepOpen?: () => void;
 failed: string[]; failImage: (src: string) => void;
};
function contains(host:Node|null|undefined,target:EventTarget|null){return !!target&&typeof (target as Node).nodeType==='number'&&!!host?.contains(target as Node);}

export function useCardHover<T extends HTMLElement = HTMLElement>(): CardHover<T> {
 const anchor = useRef<T | null>(null);
 const titleEl = useRef<HTMLElement | null>(null);
 const popup = useRef<HTMLDivElement | null>(null), mounted = useRef(true), visible = useRef(false);
 const [open, setOpen] = useState(false);
 const [failed, setFailed] = useState<string[]>([]);
 const showT = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
 const hideT = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
 const listeners=useRef<(()=>void)|null>(null);
 const clearTimers = () => { clearTimeout(showT.current); clearTimeout(hideT.current); };
 const stopListening=()=>{listeners.current?.();listeners.current=null;};
 const close = () => { clearTimers();stopListening();visible.current=false; if(mounted.current)setOpen(false); };
 function startListening(){
  if(listeners.current)return;
  const inside=(target:EventTarget|null)=>contains(anchor.current,target)||contains(popup.current,target);
  const outside=(event:Event)=>{if(!inside(event.target))close();};
  const scroll=(event:Event)=>{if(!contains(popup.current,event.target))close();};
  const key=(event:KeyboardEvent)=>{if(event.key==='Escape')close();};
  window.addEventListener('scroll',scroll,{capture:true});window.addEventListener('resize',close);
  window.addEventListener('pointerdown',outside,{capture:true});window.addEventListener('keydown',key);window.addEventListener('reeldock-overlay-open',close);
  listeners.current=()=>{window.removeEventListener('scroll',scroll,{capture:true});window.removeEventListener('resize',close);window.removeEventListener('pointerdown',outside,{capture:true});window.removeEventListener('keydown',key);window.removeEventListener('reeldock-overlay-open',close);};
 }
 const keepOpen = () => { clearTimers(); if(mounted.current){startListening();visible.current=true;setOpen(true);} };
 const show = () => { clearTimers();if(!mounted.current)return;startListening();if(visible.current)return;showT.current = setTimeout(keepOpen,320); };
 const hide = () => { clearTimers();if(visible.current)hideT.current=setTimeout(close,180);else stopListening(); };
 const failImage = (src: string) => setFailed(list => list.includes(src) ? list : [...list, src]);
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;clearTimers();stopListening();};},[]);
 return { anchor, titleEl, popup, open, show, hide, keepOpen, failed, failImage };
}

function withinPreview(hover:CardHover,target:EventTarget|null){return contains(hover.titleEl.current,target)||contains(hover.popup?.current,target);}
function keepPreview(hover:CardHover){if(hover.keepOpen)hover.keepOpen();else hover.show();}

/** Delegated trigger props for a card wrapper: the hover card only appears
 *  while the pointer is over the title text (marked .hover-card-title). */
export function titleHover<T extends HTMLElement>(hover: CardHover) {
 return {
  onMouseOver: (event: MouseEvent<T>) => {
   if(contains(hover.popup?.current,event.target)){keepPreview(hover);return;}
   const title = (event.target as HTMLElement).closest?.('.hover-card-title');
   if (title) { hover.titleEl.current = title as HTMLElement; hover.show(); } else hover.hide();
  },
  onMouseLeave: (event:MouseEvent<T>) => {if(withinPreview(hover,event.relatedTarget))keepPreview(hover);else hover.hide();},
  onFocus: (event: FocusEvent<T>) => {
   if(contains(hover.popup?.current,event.target)){keepPreview(hover);return;}
   const title = (event.target as HTMLElement).closest?.('.hover-card-title');
   if (title) { hover.titleEl.current = title as HTMLElement; hover.show(); }
  },
  onBlur: (event:FocusEvent<T>) => {if(withinPreview(hover,event.relatedTarget))keepPreview(hover);else hover.hide();},
 };
}

const CARD_WIDTH = 296, GAP = 8;
export function hoverPosition(title:{top:number;bottom:number;left:number},height:number,viewport:{width:number;height:number}){
 const width=Math.max(0,Math.min(CARD_WIDTH,viewport.width-GAP*2)),bounded=Math.min(Math.max(0,height),Math.max(0,viewport.height-GAP*2));
 const preferred=title.bottom+bounded+GAP<=viewport.height?title.bottom+GAP:title.top-bounded-GAP;
 return {width,left:Math.max(GAP,Math.min(title.left,viewport.width-width-GAP)),top:Math.max(GAP,Math.min(preferred,viewport.height-bounded-GAP))};
}

export default function HoverCard({ info, hover }: { info: HoverInfo; hover: CardHover }) {
 const anchor = hover.anchor.current;
 const fallbackPopup=useRef<HTMLDivElement|null>(null),popup=hover.popup||fallbackPopup;
 const [position,setPosition]=useState<ReturnType<typeof hoverPosition>|null>(null);
 useLayoutEffect(()=>{
  if(!hover.open||!anchor){setPosition(null);return;}
  const measure=()=>{if(!popup.current)return;const box=hover.titleEl.current?.getBoundingClientRect()||anchor.getBoundingClientRect();const next=hoverPosition(box,popup.current.getBoundingClientRect().height,{width:window.innerWidth,height:window.innerHeight});setPosition(old=>old&&old.width===next.width&&old.left===next.left&&old.top===next.top?old:next);};
  measure();const observer=typeof ResizeObserver==='undefined'?null:new ResizeObserver(measure);if(popup.current)observer?.observe(popup.current);return()=>observer?.disconnect();
 },[hover.open,anchor,info.title,info.description,info.channel,info.date]);
 if (!hover.open || !anchor) return null;
 const width=Math.max(0,Math.min(CARD_WIDTH,window.innerWidth-GAP*2));
 const image = [info.backdrop, info.image].find(src => src && !hover.failed.includes(src));
 const meta = [info.kind, info.year, info.rating ? '★ ' + info.rating : '', info.date].filter(Boolean).join(' · ');
 return createPortal(
  <Paper ref={popup} data-media-surface="preview" className={[surfaces.surface,surfaces.preview].filter(Boolean).join(' ')} withBorder shadow="md" radius="md" p="sm" fz="sm" role="dialog" aria-label={'Preview '+info.title} pos="fixed" style={{top:position?.top??GAP,left:position?.left??GAP,width,zIndex:250,pointerEvents:'auto',visibility:position?'visible':'hidden',maxHeight:'calc(100dvh - 16px)',overflowY:'auto'}} onMouseEnter={()=>keepPreview(hover)} onMouseLeave={event=>{if(withinPreview(hover,event.relatedTarget))keepPreview(hover);else hover.hide();}} onFocusCapture={()=>keepPreview(hover)} onBlur={event=>{if(withinPreview(hover,event.relatedTarget))keepPreview(hover);else hover.hide();}}>
   <Stack gap="sm"><AspectRatio ratio={16/9}>{image?<Image src={image} alt="" loading="lazy" referrerPolicy="no-referrer" onError={()=>hover.failImage(image)}/>:<Center bg="var(--mantine-color-default)" c="dimmed"><Film size={30}/></Center>}</AspectRatio>
   <Text size="sm" fw={600} lineClamp={2}>{info.title}</Text>{meta&&<Text size="xs" c="dimmed">{meta}</Text>}{info.channel&&<ChannelIdentity id={info.channelId} name={info.channel} avatar={info.avatar}/>} {info.description&&<Text size="sm" c="dimmed" lineClamp={3}>{info.description}</Text>}</Stack>
  </Paper>,
  document.body,
 );
}
