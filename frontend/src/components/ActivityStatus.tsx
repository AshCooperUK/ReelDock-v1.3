import { Badge } from '@mantine/core';
import { ArrowDownToLine, Check, CircleAlert, Clock3, SkipForward, XCircle } from 'lucide-react';

/** Display colours only: engine state, progress and available actions stay caller-owned. */
export function activityStatusColour(state?: string, phaseLabel?: string) {
 const value=state||'pending';
 if(['complete','completed','skipped','removed'].includes(value))return 'green';
 if(['failed','error','cancelled','expired','stopped'].includes(value))return 'red';
 if(['queued','paused','waiting','choose','pending'].includes(value)||/^(Paused|Choose type)$/.test(phaseLabel||''))return 'yellow';
 if(value==='cancelling')return 'blue';
 if(['verifying','encoding','merging','sponsors','metadata','processing'].includes(value)||/^(Encoding with FFmpeg|Merging audio and video|Removing sponsor sections|Saving metadata|Processing|Awaiting placement)$/.test(phaseLabel||''))return 'violet';
 if(['downloading','running','searching','starting','seeding','cancelling','preparing'].includes(value))return 'blue';
 return 'gray';
}

export function ActivityStatus({state,phaseLabel,hideIcon=false,concise=false}:{state?:string;phaseLabel?:string;hideIcon?:boolean;concise?:boolean}) {
 const Icon=state==='skipped'?SkipForward:['complete','completed','removed'].includes(state||'')?Check:['failed','error'].includes(state||'')?CircleAlert:['cancelled','expired','stopped'].includes(state||'')?XCircle:['queued','paused','waiting','choose'].includes(state||'')?Clock3:ArrowDownToLine;
 const label=({skipped:'Skipped',complete:'Completed',completed:'Completed',removed:'Removed',failed:'Failed',error:'Failed',cancelled:'Cancelled',expired:'Expired',queued:'Queued',paused:'Paused',waiting:'Waiting',choose:'Choose type',searching:'Searching',starting:'Searching',downloading:'Downloading',running:'Downloading',seeding:'Seeding',cancelling:'Cancelling',verifying:'Awaiting placement',stopped:'Stopped'} as Record<string,string>)[state||'']||'Pending';
 const colour=activityStatusColour(state,phaseLabel);
 const shownLabel=concise?({'Encoding with FFmpeg':'Encoding','Merging audio and video':'Merging','Saving metadata':'Metadata','Removing sponsor sections':'Processing','Awaiting placement':'Processing'} as Record<string,string>)[phaseLabel||'']||phaseLabel||label:phaseLabel||label;
 return <Badge data-activity-status-badge data-state={state||'pending'} variant="light" color={colour} leftSection={hideIcon?undefined:<Icon size={14} aria-hidden="true"/>} title={phaseLabel||label} style={{maxWidth:'100%',height:'auto',minHeight:24,whiteSpace:'normal',color:`var(--mantine-color-${colour}-light-color)`}}>{shownLabel}</Badge>;
}
