import { Text as PresentationText } from '@mantine/core';
import { SelectField as NativeSelect } from '../../components/SelectField';

import { Row, Notice, useAction, useSettings } from '../../SettingsUI';
import { mediaDate } from '../../mediaDate';
export default function UploadSchedule({csrf}:{csrf:string}){
 const r=useSettings<{minutes:number;last:number;next:number;paused:boolean;running:boolean;remaining:number}>('/api/youtube-schedule'),a=useAction(csrf);
 const format=(value:number)=>value?mediaDate(new Date(value*1000).toISOString()):'Not yet';
 if(!r.data)return <Notice text={r.error} error/>;
 const d=r.data;
 return <><Row title="Check YouTube for new uploads" detail="Time between full upload scans. Signed upload notifications can arrive sooner. Feed checks run in batches; a large channel list takes time. Shorter intervals use more requests when Google API is enabled."><NativeSelect aria-label="Check YouTube for new uploads" value={d.minutes} disabled={a.busy} onChange={async e=>{if(await a.run('/api/settings/youtube',{values:{check_minutes:Number(e.target.value)}}))r.reload();}}>{[15,30,60,180,360,720,1440].map(n=><option key={n} value={n}>{n<60?n+' minutes':n===60?'1 hour':n/60+' hours'}</option>)}</NativeSelect></Row><Row title="Last full scan"><PresentationText component="span" size="sm">{format(d.last)}</PresentationText></Row><Row title="Next upload check"><PresentationText component="span" size="sm">{d.paused?'Paused':d.running?'In progress':d.next&&d.next<=Date.now()/1000?'Waiting for worker':format(d.next)}{d.remaining>0?' ('+d.remaining+' channels remaining)':''}</PresentationText></Row><Notice text={a.error} error/><Notice text={a.busy?'Saving…':a.notice}/></>;
}
