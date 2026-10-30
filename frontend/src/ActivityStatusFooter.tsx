import { ActionIcon, Anchor, AppShell, Box, Group, Paper, Text, getDefaultZIndex } from '@mantine/core';
import { useElementSize } from '@mantine/hooks';
import { X } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type MouseEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useDownloadFeed, type ActivityData } from './downloadFeed';
import { youtubeCookiesUrl } from './appNavigation';
import { emptyNotifications, notificationSnapshot, registerNotificationHost, subscribeNotifications } from './notifications-state';
import styles from './app/CookieWarning.module.css';

// Reuse earlier local collapsed IDs as closed warnings; server issues stay intact.
const closedKey='reeldock:youtube-cookie-warning-collapse';
function closedWarnings():string[]{try{const saved:unknown=JSON.parse(window.localStorage.getItem(closedKey)||'[]');return Array.isArray(saved)?saved.filter((id):id is string=>typeof id==='string').slice(-16):[];}catch{return [];}}

export function CookieWarningBar({warning,onCookies,onClose}:{warning:NonNullable<ActivityData['cookie_warning']>;onCookies:()=>void;onClose:()=>void}){
 function openCookies(event:MouseEvent<HTMLAnchorElement>){if(event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;event.preventDefault();onCookies();}
 return <Paper component="section" aria-label="YouTube cookies warning" radius={0} bg="yellow.3" c="black" px="xs" py={4} pb="calc(4px + env(safe-area-inset-bottom))" data-cookie-warning-id={warning.id}>
  <Group gap="xs" wrap="nowrap" mih={44}>
   <Box role="status" aria-label={warning.message} title={warning.message} style={{flex:1,minWidth:0}}><Text visibleFrom="sm" size="sm" lineClamp={1}>{warning.message}</Text><Text hiddenFrom="sm" size="sm" lineClamp={1}>YouTube cookies need updating</Text></Box>
   <Anchor href={youtubeCookiesUrl} c="black" underline="always" size="sm" fw={600} onClick={openCookies} style={{display:'inline-flex',alignItems:'center',minHeight:44,whiteSpace:'nowrap',flexShrink:0}}>Update cookies</Anchor>
   <ActionIcon variant="transparent" color="dark" c="black" size={44} style={{flexShrink:0}} title="Close YouTube cookie warning" aria-label="Close YouTube cookie warning" onClick={onClose}><X size={20}/></ActionIcon>
  </Group>
 </Paper>;
}

/** The existing shared feed drives this footer; it starts only after sign-in. */
export default function ActivityStatusFooter({mobile,onCookies,onHeightChange,children}:{mobile:boolean;onCookies:()=>void;onHeightChange:(height:number)=>void;children:ReactNode}){
 const{activity}=useDownloadFeed(),{ref,height}=useElementSize(),{ref:barRef,height:barHeight}=useElementSize(),[closedIds,setClosedIds]=useState(closedWarnings),rawWarning=activity?.cookie_warning;
 const warning=rawWarning&&!closedIds.includes(rawWarning.id)?rawWarning:null;
 const{host}=useSyncExternalStore(subscribeNotifications,notificationSnapshot,()=>emptyNotifications),bar=useRef<HTMLDivElement|null>(null),previousFocus=useRef<HTMLElement|null>(null);
 function closeWarning(){if(!warning)return;setClosedIds(ids=>{const next=[...ids.filter(id=>id!==warning.id),warning.id].slice(-16);try{window.localStorage.setItem(closedKey,JSON.stringify(next));}catch{/* Closing still works when browser storage is unavailable. */}return next;});if(bar.current?.contains(document.activeElement)&&previousFocus.current?.isConnected)previousFocus.current.focus({preventScroll:true});}
 const attach=useCallback((node:HTMLDivElement|null)=>{bar.current=node;barRef(node);},[barRef]);
 const visible=mobile||!!warning;
 useEffect(()=>{onHeightChange(visible?height:0);},[visible,height,onHeightChange]);
 useEffect(()=>()=>onHeightChange(0),[onHeightChange]);
 useLayoutEffect(()=>{document.documentElement.style.setProperty('--reeldock-cookie-bar-height',warning?barHeight+'px':'0px');return()=>{document.documentElement.style.removeProperty('--reeldock-cookie-bar-height');};},[!!warning,barHeight]);
 useLayoutEffect(()=>{const node=bar.current;if(warning&&node&&typeof node.showPopover==='function'&&!node.matches(':popover-open'))node.showPopover();},[host,warning?.id]);
 if(!visible)return null;
 return <><AppShell.Footer h="auto" data-activity-footer=""><Box ref={ref} style={{minWidth:0}}>
  {mobile&&<Box px="xs" h="calc(66px + env(safe-area-inset-bottom))" data-mobile-bottom-nav="">{children}</Box>}
  {warning&&<Box h={barHeight} aria-hidden="true" data-cookie-footer-space=""/>}
 </Box></AppShell.Footer>{warning&&createPortal(<Box ref={attach} popover="manual" className={styles.layer} style={{zIndex:getDefaultZIndex('modal')+1}} data-cookie-warning-layer="" onFocusCapture={event=>{if(event.relatedTarget instanceof HTMLElement&&!event.currentTarget.contains(event.relatedTarget))previousFocus.current=event.relatedTarget;}}><CookieWarningBar key={warning.id} warning={warning} onCookies={onCookies} onClose={closeWarning}/></Box>,host||document.body)}</>;
}

/** Keep the status bar and native keyboard navigation in the active drawer. */
export function ActivityOverlayScope({children}:{children:ReactNode}){
 const unregister=useRef<(()=>void)|undefined>(undefined);
 const attach=useCallback((node:HTMLDivElement|null)=>{unregister.current?.();unregister.current=node?registerNotificationHost(node):undefined;},[]);
 useEffect(()=>()=>unregister.current?.(),[]);
 return <Box ref={attach} style={{minWidth:0}}>{children}</Box>;
}
