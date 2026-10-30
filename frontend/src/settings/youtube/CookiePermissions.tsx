import { Loading, Notice, Toggle } from '../../SettingsUI';
import { useEngineSettings } from '../useEngineSettings';

type Permissions = { cookie_import_channels: boolean; cookie_downloads: boolean };

export default function CookiePermissions({ csrf, importStatus = '' }: { csrf: string; importStatus?: string }) {
  const settings = useEngineSettings<Permissions>('youtube', csrf);
  const values = settings.values;
  if (!values) return <Loading error={settings.loadError} retry={settings.reload}/>;
  return <>
    <Toggle title="Allow import of channels" value={values.cookie_import_channels} disabled={settings.busy} detail="Import subscribed channels from the uploaded YouTube cookies. Turning this off keeps existing channels." onChange={value => void settings.save({ cookie_import_channels: value })}/>
    {values.cookie_import_channels && <Notice text={importStatus} inline/>}
    <Toggle title="Allow downloads using cookies" value={values.cookie_downloads} disabled={settings.busy} detail="Allow new downloads to use the uploaded cookies when authentication is needed. Existing authenticated-retry and channel settings still apply." onChange={value => void settings.save({ cookie_downloads: value })}/>
    <Notice text={settings.error} error/><Notice text={settings.notice}/>
  </>;
}
