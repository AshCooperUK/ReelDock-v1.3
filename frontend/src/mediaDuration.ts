export type MediaDuration={duration?:string;durationSeconds?:number|string;duration_seconds?:number|string;runtimeMinutes?:number|string;metadata_pending?:boolean};
const positive=(value:unknown,maximum=31536000)=>typeof value==='number'||typeof value==='string'&&/^\d+(?:\.\d+)?$/.test(value.trim())?Number.isFinite(Number(value))&&Number(value)>0&&Number(value)<=maximum?Number(value):undefined:undefined;
/** Units are explicit: duration is clock/ISO text, never a bare number. */
export function mediaSeconds(item:MediaDuration):number|undefined{
 let seconds=positive(item.durationSeconds)??positive(item.duration_seconds);
 if(seconds===undefined){const minutes=positive(item.runtimeMinutes,525600);if(minutes!==undefined)seconds=minutes*60;}
 if(seconds===undefined&&typeof item.duration==='string'){
  const match=/^(\d+):(\d{2})(?::(\d{2}))?$/.exec(item.duration.trim());
  if(match&&Number(match[2])<60&&(match[3]===undefined||Number(match[3])<60))seconds=match[3]===undefined?Number(match[1])*60+Number(match[2]):Number(match[1])*3600+Number(match[2])*60+Number(match[3]);
  const iso=/^PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$/.exec(item.duration.trim());
  if(seconds===undefined&&iso&&iso.slice(1).some(value=>value!==undefined))seconds=Number(iso[1]||0)*3600+Number(iso[2]||0)*60+Number(iso[3]||0);
 }
 return seconds!==undefined&&Number.isFinite(seconds)&&seconds>0&&seconds<=31536000?seconds:undefined;
}
export function mediaLength(item:MediaDuration):string{
 const seconds=mediaSeconds(item);
 if(seconds===undefined)return item.metadata_pending?'Length loading':'Length unavailable';
 const value=Math.floor(seconds),hours=Math.floor(value/3600),minutes=Math.floor(value/60)%60,remainder=value%60;
 return[hours?hours+'h':'',minutes?minutes+'m':'',remainder?remainder+'s':''].filter(Boolean).join(' ')||'Length unavailable';
}

/** Show/channel/groups are containers; local and VOD episode actions are files. */
export function mediaCardLength(item:MediaDuration&{kind?:string;episodeKey?:string;episode?:MediaDuration;action?:{type?:string;item?:{kind?:string}}}):string|undefined{
 if(item.action?.type==='local-group'||['channel','playlist','live'].includes(item.kind||''))return undefined;
 if(item.episodeKey)return mediaLength(item.episode||{});
 if(item.kind==='show'&&item.action?.type!=='local'&&!(item.action?.type==='vod'&&['series','tv','unsorted'].includes(item.action.item?.kind||'')))return undefined;
 return mediaLength(item);
}
