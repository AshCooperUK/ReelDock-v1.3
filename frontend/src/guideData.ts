import type { Channel, Programme } from './Guide';
import type { DiscoverItem } from './Discover';
import { mediaSeconds, type MediaDuration } from './mediaDuration';
type TitleGuideRow={key:string;item:DiscoverItem;favourite:boolean;events:{id:string;date:string;label:'Release date'|'Premiere date'|'Found in sources'|'Air date';note:string;title?:string;image?:string;description?:string}[]};
export type GuideResponse = { shows?:TitleGuideRow[]; movies?:{key:string;item:DiscoverItem;favourite:boolean;events:{id:string;date:string;label:'Release date'|'Premiere date'|'Found in sources';note:string}[]}[]; notice?:string; channels: { id: string; name: string; initials?: string; avatar?: string; description?: string; subscribers?: string | number; topics?: string[]; favourite?: boolean; sort_metrics?: {latest:number|null;upcoming:number|null;activity:number|null} }[]; uploads: GuideEvent[]; forecasts: GuideEvent[]; truncated: boolean };
type GuideEvent = MediaDuration & { id: string; channel: string; date: string; time: string; title: string; state: string; description?: string; downloaded?: boolean; views?: string | number; thumbnail?: string; confidence?: string };
export function londonClock(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(date);
  const get = (name: string) => parts.find(p => p.type === name)?.value || '00';
  return { date: `${get('year')}-${get('month')}-${get('day')}`, now: Number(get('hour')) + Number(get('minute')) / 60 };
}
export function shiftDay(date: string, offset: number) { const value = new Date(date + 'T12:00:00Z'); value.setUTCDate(value.getUTCDate() + offset); return value.toISOString().slice(0, 10); }
export function weekStart(date:string){const day=new Date(date+'T12:00:00Z').getUTCDay();return shiftDay(date,-((day+6)%7));}
export function mapGuide(data: GuideResponse, date: string, end=date): Channel[] {
  const events = new Map<string, Programme[]>();
  for (const event of [...data.uploads, ...(data.forecasts || [])]) {
    if ((event.date < date || event.date > end) || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(event.time)) continue;
    const status = ({ published: 'Uploaded', live: 'Live now', scheduled: 'Scheduled', expected: 'Expected' } as const)[event.state as 'published' | 'live' | 'scheduled' | 'expected'];
    if (!status) continue;
    const [hour, minute] = event.time.split(':').map(Number);
    const seconds = mediaSeconds(event);
    const item: Programme = { id: event.id, date:event.date, title: event.title, start: hour + minute / 60, duration: seconds ? Math.round(seconds / 60 * 10) / 10 : undefined, durationSeconds:seconds, metadata_pending:event.metadata_pending, status, description: event.description || 'No description available.', category: status === 'Expected' ? 'Upload estimate' : 'YouTube video', note: event.metadata_pending ? 'Details pending' : status === 'Expected' ? `Estimated around ${event.time}` : status === 'Scheduled' ? `Starts at ${event.time}` : status === 'Live now' ? 'Live now' : event.downloaded ? 'Downloaded' : 'Uploaded', views: event.views == null ? undefined : Number(event.views).toLocaleString('en-GB'), thumbnail: event.thumbnail, videoId: status !== 'Expected' && /^[A-Za-z0-9_-]{11}$/.test(event.id) ? event.id : undefined };
    const list = events.get(event.channel) || []; list.push(item); events.set(event.channel, list);
  }
  const channels:Channel[]=data.channels.map(channel => ({ id: channel.id, sortMetrics:channel.sort_metrics, subscriberCount:channel.subscribers==null?undefined:Number(channel.subscribers), name: channel.name, initials: channel.initials || channel.name.slice(0, 2).toUpperCase(), avatar: channel.avatar, subscribers: channel.subscribers == null ? 'Not available' : Number(channel.subscribers).toLocaleString('en-GB'), topic: (channel.topics || []).map(topic => (topic.split('/').pop() || '').replaceAll('_', ' ')).join(' · ') || 'YouTube channel', bio: channel.description || 'No channel description available.', favourite: !!channel.favourite, programmes: (events.get(channel.id) || []).sort((a, b) => (a.date||'').localeCompare(b.date||'') || a.start - b.start) }));
  const titleRows=(rows:TitleGuideRow[],kind:'movie'|'show'):Channel[]=>rows.map(row=>({id:row.key,[kind]:row.item,name:row.item.title,initials:'',avatar:row.item.image,subscribers:'',topic:kind==='movie'?'Movie':'TV show',bio:row.item.description||'',favourite:row.favourite,programmes:row.events.filter(event=>event.date>=date&&event.date<=end).map(event=>({id:event.id,date:event.date,title:event.title||event.label,start:0,allDay:true,status:event.label,note:event.note,description:event.description||row.item.description||'',category:kind==='movie'?'Movie date':'TV air date',thumbnail:event.image||row.item.image}))}));
  return [...channels,...titleRows(data.movies||[],'movie'),...titleRows(data.shows||[],'show')];
}

export function sortChannels(channels: Channel[], order: string, favouritesFirst: boolean, favouriteKeys: string[]) {
 const fav=(c:Channel)=>favouriteKeys.includes(c.id||c.name);
 const metric=(c:Channel):number|undefined|null=>order==='latest'?c.sortMetrics?.latest:order==='upcoming'?c.sortMetrics?.upcoming:order==='activity'?c.sortMetrics?.activity:c.subscriberCount;
 return channels.filter(c=>order!=='favourites'||fav(c)).slice().sort((a,b)=>{
  const favouriteDifference=favouritesFirst?Number(fav(b))-Number(fav(a)):0;if(favouriteDifference)return favouriteDifference;
  const name=a.name.localeCompare(b.name, 'en', {numeric:true,sensitivity:'base'})||(a.id||'').localeCompare(b.id||'');
  if(order==='name-desc')return -name;
  if(order==='name-asc'||order==='favourites')return name;
  const av=metric(a),bv=metric(b);
  if(av==null&&bv==null)return name;if(av==null)return 1;if(bv==null)return -1;
  return (order==='upcoming'||order==='subscribers-asc'?av-bv:bv-av)||name;
 });
}
