import type { ActivityLogEntry } from './activityLog';

export type ActivityClearSection='attention'|'completed'|'logs';
export type ActivityVisibility={activityRevision?:number;clearedEngineIds?:{torrent:string[];nzb:string[]};clearedEngineGenerations?:{torrent:Record<string,number>;nzb:Record<string,number>};suppressedActivityIds?:Record<string,string[]>};
export type ActivityClearAcknowledgement=ActivityVisibility&{ok:true;cleared:number;activityRevision:number};
export type ActivityClearResult=ActivityClearAcknowledgement&{section:ActivityClearSection};
export type ActivityClearItemResult=ActivityClearAcknowledgement&{kind:string;id:string|number;held:boolean;stopping:boolean};
export type PendingActivityClear=ActivityClearAcknowledgement&{signatures:ReadonlySet<string>};
export type ActivityObservation={state:string;generation:number;attempt:string};
export type CapturedActivityClear={kind:string;id:string|number;observed:ActivityObservation;signature:string};

/** Copy the server-issued observation now; a later poll must not upgrade this action. */
export function captureActivityClear(entry:ActivityLogEntry):CapturedActivityClear|null{
 const item=entry.data.item as {id?:unknown;activity_observation?:unknown};
 const value=item.activity_observation;
 if((typeof item.id!=='string'&&typeof item.id!=='number')||(typeof item.id==='string'&&(!item.id||item.id.length>500))||(typeof item.id==='number'&&!Number.isSafeInteger(item.id))||!value||typeof value!=='object'||Array.isArray(value))return null;
 const observed=value as Partial<ActivityObservation>;
 if(typeof observed.state!=='string'||!observed.state||observed.state.length>64||!Number.isSafeInteger(observed.generation)||(observed.generation as number)<0||typeof observed.attempt!=='string'||!/^[0-9a-f]{64}$/.test(observed.attempt))return null;
 return {kind:entry.data.kind,id:item.id,observed:{state:observed.state,generation:observed.generation as number,attempt:observed.attempt},signature:activityClearSignature(entry)};
}

export function finishedActivityAttempt(state:string):boolean{
 return ['failed','error','cancelled','expired','stopped','complete','skipped'].includes(state);
}

/** A new attempt/outcome with the same source id must remain visible. */
export function activityClearSignature(entry:ActivityLogEntry):string{
 const item=entry.data.item as Record<string,unknown>;
 const observed=item.activity_observation as Partial<ActivityObservation>|undefined;
 return JSON.stringify([entry.identity,entry.state,entry.phase||'',item.activity_generation||0,item.created||0,item.updated||0,item.finished||0,item.added||0,item.progress||0,item.message||'',item.error||'',observed?.state||'',observed?.generation??'',observed?.attempt||'']);
}
export function activityEntryCleared(entry:ActivityLogEntry,bucket:string,visibility:ActivityVisibility,pending?:PendingActivityClear|null):boolean{
 const kind=entry.data.kind;
 const item=entry.data.item as {id?:string;state?:string;activity_generation?:number};
 const pendingApplies=!!pending&&(visibility.activityRevision||0)<pending.activityRevision;
 const suppressed=pendingApplies&&pending.suppressedActivityIds?pending.suppressedActivityIds:visibility.suppressedActivityIds;
 if(suppressed?.[kind]?.includes(String(item.id)))return true;
 if(bucket!=='failed'&&bucket!=='completed')return false;
 if((kind==='torrent'||kind==='nzb')&&['queued','downloading','seeding','paused','choose','running','cancelling','starting','searching','verifying','pending'].includes(item.state||''))return false;
 if(pendingApplies&&pending.signatures.has(activityClearSignature(entry)))return true;
 if(kind!=='torrent'&&kind!=='nzb')return false;
 const id=String(item.id||'');
 const current=pendingApplies&&pending.clearedEngineGenerations?pending:visibility;
 const generations=current.clearedEngineGenerations?.[kind];
 if(generations){const cleared=generations[id];return typeof cleared==='number'&&cleared===(item.activity_generation||0);}
 return current.clearedEngineIds?.[kind]?.includes(id)||false;
}
