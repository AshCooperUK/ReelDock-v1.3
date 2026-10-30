import { Disclosure as PanelButton } from '../../SettingsUI';
import { Text as PresentationText } from '@mantine/core';
import { SelectField as NativeSelect } from '../../components/SelectField';
import { Stack as SettingsStack, Group as SettingsActions } from '@mantine/core';

import { Button } from '../../components/ui';
import { Group, Loading, Notice, Row, Toggle, useAction, useSettings } from '../../SettingsUI';
type Data={source:string;enabled:boolean;status:string;cookie_check:string;sources:{id:string;ready:boolean}[];attempts:{id:string;title:string;state:string;message:string}[]};
const names:Record<string,string>={emby:'Emby',jellyfin:'Jellyfin',plex:'Plex',kodi:'Kodi'};
export default function WatchedMatching({csrf}:{csrf:string}){
 const r=useSettings<Data>('/api/youtube/watched-matching'),a=useAction(csrf),d=r.data;
 async function action(body:object){const result=await a.run<Data>('/api/youtube/watched-matching',body,'Saved.');if(result)r.setData(result);}
 if(!d)return <Loading error={r.error} retry={r.reload}/>;
 return <><Group title="Watched matching">
 <Row title="Source player" detail="Uses the user or library selected in Media Players. Plex uses the token owner’s watched state; Kodi uses its active profile. Only downloaded YouTube videos with an exact video ID or file path match are included."><NativeSelect aria-label="Source player" value={d.source} disabled={a.busy} onChange={e=>void action({action:'preferences',values:{source:e.target.value}})}>{d.sources.map(p=><option key={p.id} value={p.id}>{names[p.id]}{p.ready?'':' — not configured'}</option>)}</NativeSelect></Row>
 <Toggle title="Match watched status" value={d.enabled} disabled={a.busy} detail="Send watched videos from the selected player to YouTube using your signed-in cookies. Checks every five minutes. Does not copy history between players or mark videos unwatched. Pause upload checks also pauses matching." onChange={enabled=>void action({action:'preferences',values:{enabled}})}/>
 <Row title="Status"><PresentationText component="span" size="sm">{d.enabled?d.status:'Off'}</PresentationText></Row>
 <SettingsStack gap="md"><SettingsActions gap="sm" wrap="wrap"><Button variant="secondary" disabled={a.busy||!d.enabled} onClick={()=>void action({action:'sync'})}>Check now</Button><Button variant="secondary" disabled={a.busy} onClick={()=>void action({action:'check_cookies'})}>Check cookies</Button></SettingsActions>
 <SettingsActions gap="sm" wrap="wrap"><PanelButton title="Recent activity"><Row title="YouTube cookies"><PresentationText component="span" size="sm">{d.cookie_check||'Not checked yet'}</PresentationText></Row>{!d.attempts.length?<PresentationText size="sm" c="dimmed">No activity yet.</PresentationText>:d.attempts.map(item=><Row key={item.id} title={item.title||item.id} detail={item.message}><PresentationText component="span" size="sm">{item.state}</PresentationText>{!['sent','sending','retry'].includes(item.state)&&<Button variant="secondary" disabled={a.busy||!d.enabled} onClick={()=>void action({action:'retry',id:item.id})}>Retry</Button>}</Row>)}</PanelButton></SettingsActions></SettingsStack>
 </Group><Notice text={a.error} error/><Notice text={a.busy?'Saving…':a.notice}/></>;
}
