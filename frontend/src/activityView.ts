import type { ActivityTask, DownloadItem } from './downloadFeed';

export type ActivityEntry =
 | { key: string; state: 'running' | 'queued'; kind: 'download'; item: DownloadItem }
 | { key: string; state: 'running' | 'queued'; kind: 'task'; item: ActivityTask };

export function activeEntries(downloads: DownloadItem[], tasks: ActivityTask[]): ActivityEntry[] {
 const entries: ActivityEntry[] = [];
 for (const item of downloads) {
  if (!item.automatic) continue;
  if (['downloading', 'cancelling', 'queued'].includes(item.state || ''))
   entries.push({key:'download-'+item.id, state:item.state==='queued'?'queued':'running', kind:'download', item});
 }
 for (const item of tasks) {
  if (item.id.startsWith('server-download-')) continue;
  if (item.state==='running'||item.state==='queued')
   entries.push({key:'task-'+item.id, state:item.state, kind:'task', item});
 }
 return entries.sort((a,b)=>Number(a.state==='queued')-Number(b.state==='queued'));
}

export type ActivityCounts = { running: number; queued: number; waiting: number };
export type OverviewActivityCounts = ActivityCounts & { paused: number };
type NativeActivityJob={id:string;state:string;progress?:number};

/** Merge the existing light feeds. A linked Discover request and its transport
 * are one download; waiting backlogs remain waiting rather than queued twice. */
export function overviewActivityCounts(downloads:readonly DownloadItem[],tasks:readonly ActivityTask[],torrents:readonly NativeActivityJob[]=[],nzbs:readonly NativeActivityJob[]=[]):OverviewActivityCounts{
 const counts:OverviewActivityCounts={running:0,queued:0,waiting:0,paused:0};
 const transports=new Set<string>(),seen=new Set<string>();
 function count(key:string,state:string){
  if(seen.has(key))return;seen.add(key);
  if(state==='paused')counts.paused++;
  else if(['queued','choose','pending','waiting'].includes(state))counts.queued++;
  else if(['downloading','running','seeding','cancelling','verifying','processing'].includes(state))counts.running++;
 }
 for(const item of downloads){const key='youtube:'+item.id;transports.add(key);count(key,item.state||'');}
 for(const [kind,jobs] of [['torrent',torrents],['nzb',nzbs]] as const)for(const job of jobs){
  const key=kind+':'+job.id;transports.add(key);
  // Torrent seeding is already a completed acquisition in Activity. NZB
  // processing at 100% remains active until the engine stops successfully.
  if(kind==='torrent'&&(job.progress||0)>=1)continue;
  count(key,job.state);
 }
 for(const item of tasks)if(item.id.startsWith('server-download-'))transports.add('vod:'+item.id.slice('server-download-'.length));
 for(const item of tasks){
  // Guide, notification and metadata checks are background work, not downloads.
  if(!item.id.startsWith('discover-')&&!item.id.startsWith('server-download-')&&!item.id.startsWith('auto-'))continue;
  if(!['running','queued'].includes(item.state))continue;
  if(item.engine&&item.engine_id&&transports.has(item.engine+':'+item.engine_id))continue;
  const key='task:'+item.id;
  if(seen.has(key))continue;
  const waiting=item.state==='queued'&&Number.isSafeInteger(item.waiting)&&Number(item.waiting)>0?Number(item.waiting):0;
  if(waiting){seen.add(key);counts.waiting+=waiting;}
  else count(key,item.state);
 }
 return counts;
}

// One counts derivation for the overview link to the main Activity page. Queued task entries flagged `waiting` are
// discover requests held for a release or indexer retry — the same rows the
// Activity page buckets as "Waiting for release". An aggregated backlog task
// reports its full count, not 1.
export function activityCounts(entries: ActivityEntry[]): ActivityCounts {
 const counts: ActivityCounts = { running: 0, queued: 0, waiting: 0 };
 for (const entry of entries) {
  if (entry.state === 'running') { counts.running += 1; continue; }
  const held = entry.kind === 'task' ? entry.item.waiting || 0 : 0;
  if (held) counts.waiting += held; else counts.queued += 1;
 }
 return counts;
}

export function activitySummaryText(counts: ActivityCounts & {paused?:number}): string {
 return counts.running + ' running · ' + counts.queued + ' queued' + (counts.waiting ? ' · ' + counts.waiting + ' waiting' : '')+(counts.paused?' · '+counts.paused+' paused':'');
}

export function manualYoutubeEntries(downloads: DownloadItem[]): ActivityEntry[] {
 return downloads.filter(item=>!item.automatic&&['downloading','cancelling','queued'].includes(item.state||''))
  .map(item=>({key:'download-'+item.id,state:item.state==='queued'?'queued':'running',kind:'download',item}));
}
