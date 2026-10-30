import * as React from 'react';
import { Alert, Box, Group, Image, Paper, Stack, Tabs } from '@mantine/core';
import { SelectField as NativeSelect } from './SelectField';
import { useMediaQuery } from '@mantine/hooks';
import { ArrowLeft } from 'lucide-react';
import { Button, Dialog, DialogContent, DialogTitle, DialogDescription } from './ui';
import { channelEvent } from '../channelNavigation';
import surfaces from './MediaSurfaces.module.css';

// One Mantine surface for details. Related items replace this surface; manage
// panels and destructive confirmations remain inside it.
export type DetailSheetTab = { id: string; label: React.ReactNode; content: React.ReactNode };
function DetailHeader({title,subtitle,channel,banner,bannerFallback,media,actions,compact=false}:{title:React.ReactNode;subtitle?:React.ReactNode;channel?:React.ReactNode;banner?:string;bannerFallback?:string;media?:React.ReactNode;actions?:React.ReactNode;compact?:boolean}) {
 const [failed,setFailed]=React.useState<string[]>([]);
 const [attempt,setAttempt]=React.useState(0);
 // Cached artwork can initially return an empty 204 while its worker fetches
 // the bytes. Probe only that local cache, keeping the visible fallback stable
 // until an actual image loads; no hidden image element or empty banner strip.
 React.useEffect(()=>{
  const waiting=[banner,bannerFallback].find(value=>value&&/^\/artwork\/[a-f0-9]{64}(?:\?|$)/.test(value)&&failed.includes(value));
  if(!waiting||attempt>=12)return;
  let retired=false;let probe:HTMLImageElement|undefined;
  const timer=window.setTimeout(()=>{
   probe=new window.Image();
   probe.onload=()=>{if(!retired)setFailed(previous=>previous.filter(value=>value!==waiting));};
   probe.onerror=()=>{if(!retired)setAttempt(value=>value+1);};
   probe.src=waiting+(waiting.includes('?')?'&':'?')+'artwork_retry='+String(attempt+1);
  },5000);
  return()=>{retired=true;window.clearTimeout(timer);if(probe){probe.onload=null;probe.onerror=null;}};
 },[banner,bannerFallback,failed,attempt]);
 const source=[banner,bannerFallback].find(value=>value&&!failed.includes(value));
 return <Paper component="header" data-detail-header withBorder radius="md" p="md" pos="relative" style={{isolation:'isolate',overflow:'hidden',minWidth:0}}>
  {source&&<Image key={source} data-detail-banner src={source} alt="" pos="absolute" inset={0} w="100%" h="100%" fit="cover" style={{zIndex:-1}} onError={event=>{event.currentTarget.hidden=true;setFailed(previous=>[...previous,source]);}}/>}
  <Stack gap="md" pt={source?(compact?{base:24,sm:40}:{base:84,sm:132}):0} style={{minWidth:0}}>
   <Paper data-detail-header-information data-media-surface={source?'banner-information':undefined} className={source?[surfaces.surface,surfaces.bannerInformation].filter(Boolean).join(' '):undefined} withBorder={!!source} radius="md" p={source?'md':0} bg={source?undefined:'var(--mantine-color-body)'} style={{minWidth:0}}>
    <Group wrap="nowrap" gap="md" align="flex-start">{media&&<Box data-detail-poster w={{base:56,sm:compact?64:80}} style={{flexShrink:0,overflow:'hidden',borderRadius:'var(--mantine-radius-sm)'}}>{media}</Box>}<Stack gap="xs" style={{minWidth:0,flex:1}}><DialogTitle data-detail-title style={{overflowWrap:'anywhere',paddingRight:source?0:64}}>{title}</DialogTitle>{subtitle&&<DialogDescription data-detail-type style={{overflowWrap:'anywhere'}}>{subtitle}</DialogDescription>}{channel&&<Box data-detail-channel style={{minWidth:0}}>{channel}</Box>}</Stack></Group>
    {actions&&<Group gap="sm" mt="md">{actions}</Group>}
   </Paper>
  </Stack>
 </Paper>;
}
export function SheetBack({ label, onBack }: { label: React.ReactNode; onBack: () => void }) {
  return <Button type="button" variant="ghost" onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />Back to {label}</Button>;
}
export function ConfirmInline({ title, children, confirm = 'Remove', keep = 'Keep it', busy = false, onConfirm, onCancel }: {
  title?: React.ReactNode; children?: React.ReactNode; confirm?: React.ReactNode; keep?: React.ReactNode;
  busy?: boolean; onConfirm: () => void; onCancel: () => void;
}) {
  return <Alert color="red" variant="light" title={title} role="alertdialog" aria-label={typeof title === 'string' ? title : undefined}
    onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onCancel(); } }}>
    {children}
    <Group mt="md" gap="sm">
      <Button variant="secondary" disabled={busy} autoFocus onClick={onCancel}>{keep}</Button>
      <Button variant="danger" disabled={busy} onClick={onConfirm}>{busy ? 'Saving…' : confirm}</Button>
    </Group>
  </Alert>;
}
export function DetailSheet({ open, onOpenChange, title, subtitle, channel, banner, bannerFallback, media, actions, tabs, tab, onTab, back, footer, className, bodyClassName, bodyRef, scrollBottom = false, compactHeader = false, closeOnChannelNavigation = true, children }: {
  open: boolean; onOpenChange: (open: boolean) => void; title: React.ReactNode; subtitle?: React.ReactNode; channel?: React.ReactNode; banner?: string; bannerFallback?:string; media?: React.ReactNode;
  actions?: React.ReactNode; tabs?: DetailSheetTab[]; tab?: string; onTab?: (id: string) => void; back?: { label: React.ReactNode; onBack: () => void };
  footer?: React.ReactNode; className?: string; bodyClassName?: string; bodyRef?: React.Ref<HTMLDivElement>; scrollBottom?: boolean; compactHeader?:boolean; closeOnChannelNavigation?: boolean; children?: React.ReactNode;
}) {
  const [ownTab, setOwnTab] = React.useState<string>();
  const phone = useMediaQuery('(max-width: 47.99em)');
  const active = tab ?? ownTab ?? tabs?.[0]?.id;
  const current = tabs ? tabs.find(entry => entry.id === active) : undefined;
  const chooseTab = (id: string) => { setOwnTab(id); onTab?.(id); };
  const tabId = React.useId();
  React.useEffect(() => {
    if(!closeOnChannelNavigation)return;
    const close = () => onOpenChange(false);
    window.addEventListener(channelEvent, close);
    return () => window.removeEventListener(channelEvent, close);
  }, [onOpenChange,closeOnChannelNavigation]);
  React.useEffect(() => { if (!open) setOwnTab(undefined); }, [open]);
  const panel=<Stack gap="md">{back&&<SheetBack label={back.label} onBack={back.onBack}/>}<Paper withBorder radius="md" p={{base:'sm',sm:'lg'}} className={bodyClassName}><Stack gap="md">{tabs?current?.content:children}</Stack></Paper></Stack>;
  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent scrollBottom={scrollBottom} scrollRef={bodyRef} className={className}>
   <Stack gap="lg" style={{minWidth:0}}>
    <DetailHeader key={(banner||'')+'\0'+(bannerFallback||'')} title={title} subtitle={subtitle} channel={channel} banner={banner} bannerFallback={bannerFallback} media={media} actions={actions} compact={compactHeader}/>
    {tabs&&tabs.length>1&&!phone?<Tabs value={active} onChange={value=>{if(value)chooseTab(value);}} id={tabId} keepMounted={false}><Tabs.List aria-label="Details sections">{tabs.map(entry=><Tabs.Tab key={entry.id} value={entry.id} mih={44}>{entry.label}</Tabs.Tab>)}</Tabs.List><Tabs.Panel value={active||''} pt="md">{panel}</Tabs.Panel></Tabs>:<Stack gap="md">{tabs&&tabs.length>1&&<NativeSelect aria-label="Details sections" value={active} onChange={event=>chooseTab(event.currentTarget.value)} data={tabs.map(entry=>({value:entry.id,label:typeof entry.label==='string'?entry.label:entry.id}))}/>} {panel}</Stack>}
    {footer&&<Group component="footer" gap="sm">{footer}</Group>}
   </Stack>
  </DialogContent></Dialog>;
}
