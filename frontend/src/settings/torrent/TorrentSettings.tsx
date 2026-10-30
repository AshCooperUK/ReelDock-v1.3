import { Disclosure as PanelButton } from '../../SettingsUI';
import { SelectField as NativeSelect } from '../../components/SelectField';
import { Group as SettingsActions } from '@mantine/core';

import { TextInput } from '../../TextField';

import { Group, Loading, Notice, Row, Toggle } from '../../SettingsUI';
import { useEngineSettings } from '../useEngineSettings';

type Preferences = {
  download_limit_bps: number; max_active_downloads: number;
  listen_port: number; encryption: string; enable_dht: boolean; enable_pex: boolean;
  enable_lsd: boolean; enable_upnp: boolean; enable_natpmp: boolean; enable_web_seeds: boolean;
};
export default function TorrentSettings({csrf}:{csrf:string}) {
  const settings = useEngineSettings<Preferences>('torrent', csrf);
  const values = settings.values;
  if (!values) return <Loading error={settings.loadError} retry={settings.reload}/>;
  function number(key: 'download_limit_bps'|'max_active_downloads'|'listen_port', title:string, detail:string, max?:number, scale=1) {
    const current = values![key];
    return <Row title={title} detail={detail}><TextInput aria-label={title} type="number" min="0" max={max} required disabled={settings.busy} key={current} defaultValue={current/scale} step={scale===1?1:'any'} onBlur={event=>{
      const input=event.currentTarget;
      const value=Math.round(Number(input.value)*scale);
      if(input.checkValidity() && Number.isSafeInteger(value) && value!==current) void settings.save({[key]:value});
    }}/></Row>;
  }
  function toggle(key:'enable_dht'|'enable_pex'|'enable_lsd'|'enable_upnp'|'enable_natpmp'|'enable_web_seeds',title:string,detail:string) {
    return <Toggle title={title} detail={detail} value={values![key]} disabled={settings.busy} onChange={value=>void settings.save({[key]:value})}/>;
  }
  const feedback=<><Notice text={settings.error} error/><Notice text={settings.busy?'Saving…':settings.notice}/></>;
  return <><Group title="Download limits">
    {number('max_active_downloads','Torrents at once','Maximum active torrent downloads. Zero holds downloads in the queue.',999)}
    {number('download_limit_bps','Download limit (KB/s)','Zero leaves the download speed unlimited.',undefined,1024)}
  </Group><Group title="Connections">
    {number('listen_port','Incoming port','Port inside the container; zero chooses a free port. Takes effect after ReelDock restarts. External reachability also depends on Docker port mappings and your router.',65535)}
    <Row title="Peer encryption" detail="Applies to new peer connections. Required rejects unencrypted peers; encryption does not hide your IP address."><NativeSelect aria-label="Peer encryption" disabled={settings.busy} value={values.encryption} onChange={event=>void settings.save({encryption:event.target.value})}><option value="disabled">Disabled</option><option value="prefer">Preferred</option><option value="required">Required</option></NativeSelect></Row>
  </Group><SettingsActions gap="sm" wrap="wrap"><PanelButton title="Advanced networking"><Group title="Peer discovery">
    {toggle('enable_dht','DHT','Find peers through the distributed hash table. Private torrents do not use DHT.')}
    {toggle('enable_pex','Peer exchange','Allow connected peers to share peer addresses. Private torrents do not use peer exchange.')}
    {toggle('enable_lsd','Local peer discovery','Find peers on your local network. Enabling this may require a ReelDock restart. Private torrents do not use local discovery.')}
    {toggle('enable_web_seeds','Web seeds','Allow downloads from HTTP sources included in the torrent.')}
  </Group><Group title="Router port mapping">
    {toggle('enable_upnp','UPnP','Request automatic router port mapping when ReelDock starts. Changes require a restart; existing router mappings may remain until they expire or are removed on the router.')}
    {toggle('enable_natpmp','NAT-PMP','Request automatic router port mapping when ReelDock starts. Changes require a restart; Docker and router support are also required.')}
  </Group></PanelButton></SettingsActions>{feedback}</>;
}
