import { Group, Loading, Notice, Row, Toggle } from '../../SettingsUI';
import { TextInput } from '../../TextField';
import { useEngineSettings } from '../useEngineSettings';

type Preferences = {enabled:boolean; torrent:boolean; nzb:boolean; strm:boolean;release_check_minutes:number};
export default function AutoDownloadSettings({csrf}:{csrf:string}) {
  const settings=useEngineSettings<Preferences>('auto-download',csrf);
  const values=settings.values;
  if(!values) return <Loading error={settings.loadError} retry={settings.reload}/>;
  return <><Group title="Discover"><Row title="Release checks" detail="Minutes between checks for requested movies and episodes waiting for a release. Download preferences stay with each source engine."><TextInput aria-label="Release check interval in minutes" type="number" min="5" max="1440" disabled={settings.busy} key={values.release_check_minutes} defaultValue={values.release_check_minutes} onBlur={e=>{if(e.currentTarget.checkValidity())void settings.save({release_check_minutes:Number(e.target.value)});}}/></Row></Group><Group title="Watched files">
    <Toggle title="Watch for new files" detail="Check the Compose-managed Auto-Download folder every few seconds. Turning this off stops new files being picked up; downloads already started continue. The shared Pause downloads control also pauses watching." value={values.enabled} disabled={settings.busy} onChange={enabled=>void settings.save({enabled})}/>
  </Group><Group title="File handling">
    <Toggle title="Torrent files" detail="Hand .torrent files to the torrent engine. Its limits, naming and metadata preferences apply. When disabled, these files stay in the watched folder." value={values.torrent} disabled={settings.busy} onChange={torrent=>void settings.save({torrent})}/>
    <Toggle title="NZB files" detail="Hand .nzb files to the NZB engine using your configured Usenet servers. When disabled, these files stay in the watched folder." value={values.nzb} disabled={settings.busy} onChange={nzb=>void settings.save({nzb})}/>
    <Toggle title="Stream-link files" detail="Read .strm links. YouTube links use the YouTube downloader; other supported stream links download as movies or episodes. When disabled, these files stay in the watched folder." value={values.strm} disabled={settings.busy} onChange={strm=>void settings.save({strm})}/>
  </Group><Notice text={settings.error} error/><Notice text={settings.busy?'Saving…':settings.notice}/></>;
}
