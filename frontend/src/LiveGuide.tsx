import { useEffect, useMemo, useState } from 'react';
import Guide, { type Channel } from './Guide';
import { api } from './api';
import { londonClock, mapGuide, shiftDay, weekStart, type GuideResponse } from './guideData';
import { onVisible, pageHidden } from './visibility';
import { guideDate, withGuideDate } from './appNavigation';
import { createWarmSnapshots } from './warmSnapshots';

type GuideFrame={channels:Channel[];truncated:boolean};
const snapshots=createWarmSnapshots<GuideFrame>({limit:4});
if(typeof window!=='undefined')snapshots.watch(window,['reeldock-movie-options-changed','reeldock-show-options-changed','reeldock-channel-changed','reeldock-channel-removed','reeldock-watchlist-changed','reeldock-library-changed','reeldock-display-snapshot-changed']);

export default function LiveGuide({ csrf }: { csrf: string }) {
  const [clock, setClock] = useState(londonClock);
  // ?date=YYYY-MM-DD opens the week containing that day; the address bar follows the week shown.
  const [date, setDate] = useState(() => weekStart(guideDate(location.search) || londonClock().date));
  useEffect(() => { const url = withGuideDate(location.href, date === weekStart(londonClock().date) ? null : date); if (url !== location.pathname + location.search + location.hash) history.replaceState(history.state, '', url); }, [date]);
  const scopeKey=JSON.stringify([csrf,date]),scope=useMemo(()=>snapshots.open(csrf,date),[csrf,date]);
  const [frame,setFrame]=useState<GuideFrame&{scope:string}>(()=>({scope:scopeKey,channels:[],truncated:false,...scope.read()}));
  const current=frame.scope===scopeKey?frame:{scope:scopeKey,channels:[],truncated:false,...scope.read()};
  const {channels,truncated}=current;
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [revision, setRevision] = useState(0);
  const [notice,setNotice]=useState('');
  const [saving, setSaving] = useState(false);
  const [favourites, setFavourites] = useState(() => { try { return localStorage.getItem('reeldock-guide-favourites') !== 'false'; } catch { return true; } });
  const [noticeKind,setNoticeKind]=useState<'warning'>();
  useEffect(() => { const tick = () => { if (!pageHidden()) setClock(londonClock()); }; const timer = setInterval(tick, 30000); const stopVisible = onVisible(tick); return () => { clearInterval(timer); stopVisible(); }; }, []);
  useEffect(() => {
    const controller = new AbortController(),requestSnapshot=snapshots.open(csrf,date); let timer: ReturnType<typeof setTimeout>; let requestError='', busy = false, started = false;
    setLoading(true);setFrame(old=>old.scope===scopeKey?old:{scope:scopeKey,channels:[],truncated:false,...requestSnapshot.read()});setError('');setNotice('');setNoticeKind(undefined);
    async function load() {
      if (busy||controller.signal.aborted) return; busy = true; clearTimeout(timer);
      const readSnapshot=snapshots.open(csrf,date);let refreshInvalidated=false;
      try { const data = await api<GuideResponse>(`/api/guide?start=${date}&end=${shiftDay(date,6)}`, controller.signal,undefined,undefined,{feedback:'inline'}); if (controller.signal.aborted) return;if(!readSnapshot.valid()){refreshInvalidated=readSnapshot.currentAccount();return;}const next={channels:mapGuide(data,date,shiftDay(date,6)),truncated:data.truncated};readSnapshot.write(next);setFrame(old=>readSnapshot.valid()?{scope:scopeKey,...next}:old);setNotice(requestError||data.notice||'');setNoticeKind(requestError?'warning':undefined);setError(''); }
      catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Could not load this week.'); }
      finally { busy = false; if (!controller.signal.aborted) {if(refreshInvalidated)void load();else{setLoading(false);if(!pageHidden())timer=setTimeout(load,60000);}} }
    }
    const stopVisible = onVisible(() => { if (started) void load(); });
    void api('/api/guide/week',controller.signal,{start:date},csrf,{feedback:'inline'}).catch(e=>{if(!controller.signal.aborted)requestError=e instanceof Error?e.message:'This week could not update.';}).finally(()=>{if(!controller.signal.aborted){started=true;void load();}}); return () => { controller.abort(); clearTimeout(timer); stopVisible(); };
  }, [date, revision, csrf]);
  useEffect(()=>{const changed=()=>setRevision(v=>v+1);window.addEventListener('reeldock-movie-options-changed',changed);window.addEventListener('reeldock-show-options-changed',changed);window.addEventListener('reeldock-channel-changed',changed);window.addEventListener('reeldock-channel-removed',changed);return()=>{window.removeEventListener('reeldock-movie-options-changed',changed);window.removeEventListener('reeldock-show-options-changed',changed);window.removeEventListener('reeldock-channel-changed',changed);window.removeEventListener('reeldock-channel-removed',changed);};},[]);
  async function favourite(id: string, value: boolean) {
    if (saving) return; setSaving(true);
    try { const channel=channels.find(c=>c.id===id),title=channel?.movie||channel?.show;await api(title?'/api/discovery-engine/'+(title.kind==='show'?'show-options':'movie-options'):'/api/channel-favourite/'+encodeURIComponent(id),undefined,title?{item:title,settings:{favourite:value}}:{favourite:value},csrf);window.dispatchEvent(new Event('reeldock-display-snapshot-changed'));setFrame(old=>{if(old.scope!==scopeKey)return old;return{...old,channels:old.channels.map(c=>c.id===id?{...c,favourite:value}:c)};}); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not save favourite.'); }
    finally { setSaving(false); }
  }
  return <Guide favouritesOnTop={favourites} onFavouritesOnTop={value => { setFavourites(value); try { localStorage.setItem('reeldock-guide-favourites', String(value)); } catch { /* Works for this visit. */ } }} live={{ csrf, channels, date, today: clock.date, now: clock.now, loading:frame.scope!==scopeKey||loading, error:frame.scope===scopeKey?error:'', notice:frame.scope===scopeKey?notice:'', noticeKind:frame.scope===scopeKey?noticeKind:undefined, truncated, saving, favourite, selectDate: selected=>setDate(weekStart(selected)), retry: () => setRevision(v => v + 1), changeDay: offset => setDate(old => offset === 0 ? weekStart(londonClock().date) : shiftDay(old, offset)) }} />;
}
