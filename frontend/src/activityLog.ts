import { api } from './api';
import type { ActivityHistoryRow } from './DownloadHistory';

export type ActivityLogEntry={id:string;identity:string;state:string;phase?:string;at:number;pin:number;data:{kind:string;item:object}};
type Page={items:ActivityHistoryRow[];more:boolean;total:number};
type Facts={id?:string|number;title?:string;name?:string;state?:string;message?:string;description?:string;subscription?:string;sourceType?:string;sourceName?:string;kind?:string;mediaKind?:string;channel_name?:string;progress?:number;created?:number;updated?:number;finished?:number;added?:number;destination?:string;metadata_note?:string;error?:string;stop_reason?:string;published?:string[];placements?:unknown[];files?:{selected?:boolean;size?:number}[];down_rate?:number;up_rate?:number;peers?:number;connections?:number;stats?:{changes?:Record<string,{added:number;removed:number;updated:number}>};year?:string;genre?:string;rating?:string;archived?:boolean};
type RecordEntry={entry:ActivityLogEntry;title:string;source:string;progress?:number;dates:string[];details:string[]};
export type ActivityLogDownload={text:string;filename:string;count:number};
const MAX_HISTORY=100_000;

/** Export only public text facts, then remove access-bearing text from notes. */
export function redactActivityText(value:unknown):string{
 if(typeof value!=='string'&&typeof value!=='number')return '';
 return String(value).replace(/\u001b\[[0-9;]*m/g,'').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g,'')
  .replace(/(^|[\s;{])["']?(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-csrf-token|referer|origin|user-agent|host|accept|content-type|content-length)["']?\s*:[^\n]*/gim,'$1[header omitted]')
  .replace(/\b[A-Za-z][A-Za-z0-9+.-]*:\/\/[^\s<>"']+/g,'[URL omitted]')
  .replace(/\/\/[^\s<>"']+/g,'[URL omitted]')
  .replace(/\b(?:file|smb|magnet|data|plugin):[^\s<>"']+/gi,'[URL omitted]')
  .replace(/\b[A-Za-z][A-Za-z0-9+.-]*%3a[^\s<>"']+/gi,'[URL omitted]')
  .replace(/\b[A-Za-z0-9_.%-]+=[^\s<>"']*&[^\s<>"']+/g,'[query omitted]')
  .replace(/\b(?:bearer|basic)\s+[^\s,;"']+/gi,'[credential omitted]')
  .replace(/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g,'[credential omitted]')
  .replace(/(["']?\b(?:access[_ -]?token|refresh[_ -]?token|id[_ -]?token|auth[_ -]?token|(?:[a-z]+[_-])*token|passkey|authkey|auth_key|session|sessionid|session_id|sid|api[_ -]?key|apikey|password|passwd|secret|client[_ -]?secret|authorization|cookie|csrf[_ -]?token|x-api-key|x-csrf-token|x-amz-[a-z-]+|x-goog-[a-z-]+|signature|credential|awsaccesskeyid|key-pair-id|policy)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;&]+)/gi,'$1[omitted]')
  .replace(/\r\n?/g,'\n').trim();
}
function stamp(value:unknown){const n=Number(value);if(!Number.isFinite(n)||n<=0)return '';const date=new Date(n*1000);return Number.isNaN(date.getTime())?'':date.toISOString();}
function record(entry:ActivityLogEntry):RecordEntry{
 const item=entry.data.item as Facts,kind=entry.data.kind;
 const source=kind==='youtube'?['YouTube',item.channel_name].filter(Boolean).join(' · '):kind==='m3u'?item.sourceType==='strm'?'STRM':['M3U',item.subscription].filter(Boolean).join(' · '):kind==='discover'?'Discover':kind==='torrent'?'Torrent':kind==='nzb'?'NZB':['ReelDock',item.subscription].filter(Boolean).join(' · ');
 const details=[item.message,kind==='discover'?item.description:undefined,item.metadata_note,item.error,item.stop_reason,item.sourceType==='strm'&&item.sourceName?'STRM file: '+item.sourceName:undefined,item.destination?'Path: '+item.destination:undefined,...(item.published||[]).map(path=>'Path: '+path)];
 if(item.archived)details.unshift('Cleared from Activity; retained in the export archive.');
 if(item.sourceType==='strm')details.push('Type: '+(item.kind==='series'?'TV episode':'Movie'));
 if(kind==='torrent'||kind==='nzb'){
  if(item.state)details.push('Engine state: '+item.state);
  for(const [label,value]of [['Download rate (bytes/s)',item.down_rate],['Upload rate (bytes/s)',item.up_rate],['Peers',item.peers],['Connections',item.connections]] as const)if(typeof value==='number'&&Number.isFinite(value))details.push(label+': '+Math.max(0,value));
  if(kind==='torrent'&&item.state==='downloading'&&item.down_rate&&item.down_rate>0){const total=(item.files||[]).filter(file=>file.selected).reduce((sum,file)=>sum+(Number(file.size)||0),0);if(total>0)details.push('Estimated seconds remaining: '+Math.round(total*Math.max(0,1-(item.progress||0))/item.down_rate));}
 }
 if(kind==='discover'){const facts=[item.mediaKind==='show'?'TV show':'Movie',item.year,item.rating?item.rating+' IMDb':'',item.genre].filter(Boolean).join(' · ');if(facts)details.push(facts);}
 if(item.placements?.length)details.push('Choose a Movies or TV Shows folder for this download.');
 for(const [type,counts]of Object.entries(item.stats?.changes||{}))details.push(type+': '+Number(counts.added||0)+' added · '+Number(counts.removed||0)+' removed · '+Number(counts.updated||0)+' updated');
 const dates=[['Added',item.created||item.added],['Updated',item.updated],['Finished',item.finished]].flatMap(([label,value])=>{const date=stamp(value);return date?[label+': '+date]:[];});
 const progress=typeof item.progress==='number'&&Number.isFinite(item.progress)?Math.round(Math.max(0,Math.min(100,item.progress*(kind==='torrent'||kind==='nzb'||kind==='discover'?100:1)))):undefined;
 return {entry,title:redactActivityText(item.title||item.name||'Activity'),source:redactActivityText(source),progress,dates,details:[...new Set(details.map(redactActivityText).filter(Boolean))]};
}
function cancelled(){const error=new Error('Log export cancelled.');error.name='AbortError';return error;}
export async function exportActivityLog({entries,filter,label,matches,historyEntry,compare,signal,onProgress,readPage,now=new Date()}:{
 entries:readonly ActivityLogEntry[];filter:string;label:string;matches:(entry:ActivityLogEntry)=>boolean;historyEntry:(row:ActivityHistoryRow)=>ActivityLogEntry;compare:(a:ActivityLogEntry,b:ActivityLogEntry)=>number;signal:AbortSignal;onProgress?:(done:number,total:number)=>void;readPage?:(offset:number,signal:AbortSignal)=>Promise<Page>;now?:Date;
}):Promise<ActivityLogDownload>{
 const read=readPage||((offset:number,current:AbortSignal)=>api<Page>('/api/download-history?limit=50&offset='+offset+'&include_archived=1&plain=1',current,undefined,undefined,{feedback:'inline'}));
 const records=new Map<string,RecordEntry>(),seen=new Set<string>();let total:number|undefined,offset=0;
 for(let page=0;page<=MAX_HISTORY;page++){
  if(signal.aborted)throw cancelled();
  const batch=await read(offset,signal);if(signal.aborted)throw cancelled();
  if(!batch||!Array.isArray(batch.items)||typeof batch.more!=='boolean'||!Number.isSafeInteger(batch.total)||batch.total<0)throw new Error('Activity returned an incomplete log. No file was exported; try again.');
  if(batch.total>MAX_HISTORY)throw new Error('Activity has more than 100,000 history entries. No partial log was exported.');
  if(total===undefined)total=batch.total;else if(total!==batch.total)throw new Error('Activity history changed during export. No partial log was exported; try again.');
  if((!batch.items.length&&batch.more)||offset+batch.items.length>total)throw new Error('Activity returned an incomplete log. No file was exported; try again.');
  for(const row of batch.items){
   if(!row||!row.item||!['youtube','m3u','m3u_sync','event','discover','torrent','nzb'].includes(row.kind)||row.item.id==null||((row.kind==='torrent'||row.kind==='nzb')&&!row.item.archived))throw new Error('Activity returned an incomplete log. No file was exported; try again.');
   const entry=historyEntry(row);if(seen.has(entry.identity))throw new Error('Activity history changed during export. No partial log was exported; try again.');seen.add(entry.identity);records.set(entry.identity,record(entry));
  }
  offset+=batch.items.length;onProgress?.(offset,total);
  if(!batch.more){if(offset!==total)throw new Error('Activity returned an incomplete log. No file was exported; try again.');break;}
  if(offset>=total)throw new Error('Activity returned an incomplete log. No file was exported; try again.');
 }
 // These are the exact live/loaded rows visible when Export was pressed. They
 // replace duplicate history rows, preserving active state and engine policy.
 for(const entry of entries)records.set(entry.identity,record(entry));
 const selected=[...records.values()].filter(value=>matches(value.entry)).sort((a,b)=>compare(a.entry,b.entry));
 const lines=['ReelDock Activity log','Exported (UTC): '+now.toISOString(),'Status filter: '+redactActivityText(label||filter),'Coverage: All available saved history, cleared Activity archive and the current live Torrent/NZB snapshot.','Entries: '+selected.length,'URLs, access credentials and request headers are omitted.',''];
 for(const [index,value]of selected.entries()){
  if(signal.aborted)throw cancelled();
  lines.push((index+1)+'. '+value.title,'Source: '+value.source,'Status: '+redactActivityText(value.entry.state)+(value.entry.phase?' · '+redactActivityText(value.entry.phase):''),...value.dates);
  if(value.progress!==undefined)lines.push('Progress: '+value.progress+'%');
  if(value.details.length)lines.push('Details:',...value.details.map(detail=>'  '+detail.replace(/\n/g,'\n  ')));
  lines.push('');
 }
 if(!selected.length)lines.push('No matching activity is recorded.','');
 return {text:lines.join('\n'),filename:'ReelDock-Activity-'+now.toISOString().replace(/[-:]/g,'').replace(/\.\d{3}Z$/,'Z')+'.txt',count:selected.length};
}
export function saveActivityLog(result:ActivityLogDownload){
 const blob=new Blob(['\uFEFF',result.text],{type:'text/plain;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');
 link.href=url;link.download=result.filename;link.style.display='none';
 try{document.body.appendChild(link);link.click();}finally{link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
}
