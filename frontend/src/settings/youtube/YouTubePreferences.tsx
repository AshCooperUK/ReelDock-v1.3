import { SelectField as NativeSelect } from '../../components/SelectField';
import { Stack as SettingsStack } from '@mantine/core';

import { Group, Loading, Notice, Row, Toggle } from '../../SettingsUI';
import { TextInput } from '../../TextField';
import { useEngineSettings } from '../useEngineSettings';

type Preferences = Record<string, boolean | number | string>;
const processing = [['hardware_encoding', 'Hardware encoding'], ['convert_fallback', 'Convert unsupported formats'], ['force_transcode', 'Always transcode with FFmpeg']] as const;
const automation = [['authenticated_retry', 'Retry using saved YouTube cookies'], ['new_channel_videos', 'Auto-download videos from new channels'], ['restrict_filenames', 'Use restricted filenames']] as const;

export default function YouTubePreferences({ csrf }: { csrf: string }) {
  const settings = useEngineSettings<Preferences>('youtube', csrf);
  const d = settings.values;
  if (!d) return <Loading error={settings.loadError} retry={settings.reload}/>;
  function numberBlur(key: string, input: HTMLInputElement) {
    if (!input.value || !input.checkValidity()) { input.reportValidity(); return; }
    if (Number(input.value) !== d?.[key]) void settings.save({ [key]: Number(input.value) });
  }
  return <>
    <fieldset style={{ border: 0, margin: 0, padding: 0, minWidth: 0 }} disabled={settings.busy}><SettingsStack gap="md">
      <Group title="Picture & sound">
        <Row title="Maximum quality"><NativeSelect aria-label="Maximum quality" value={String(d.quality)} onChange={e=>void settings.save({quality:e.target.value})}>{['480','720','1080','2160'].map(q=><option key={q} value={q}>{q}p</option>)}</NativeSelect></Row>
        {processing.map(([key,title])=><Toggle key={key} title={title} detail={key==='convert_fallback'?'Controls automatic downloads. Manual selections always try available formats and convert when needed.':undefined} value={!!d[key]} onChange={value=>void settings.save({[key]:value})}/>)}
        <Row title="Minimum length (minutes)" detail="Also filters Discover’s YouTube results when Show Shorts is off. Automatic downloads use the channel type, date and minimum-length filters. Manual selections override automatic filters."><TextInput aria-label="Minimum length (minutes)" type="number" min="0" max="120" key={String(d.minimum_video_minutes)} defaultValue={Number(d.minimum_video_minutes)} onBlur={e=>numberBlur('minimum_video_minutes',e.currentTarget)}/></Row>
      </Group>
      <Group title="YouTube download defaults">
        {automation.map(([key,title])=><Toggle key={key} title={title} value={!!d[key]} onChange={value=>void settings.save({[key]:value})}/>)}
        <Row title="Default download range"><NativeSelect aria-label="Default download range" value={Number(d.default_range)} onChange={e=>void settings.save({default_range:Number(e.target.value)})}>{[0,1,2,7,14,30,90,180,365,730].map(n=><option key={n} value={n}>{n===0?'All uploads':n+' days'}</option>)}</NativeSelect></Row>
        <Row title="Wait between downloads (seconds)"><TextInput aria-label="Wait between downloads (seconds)" type="number" min="0" max="86400" key={String(d.sleep_interval)} defaultValue={Number(d.sleep_interval)} onBlur={e=>numberBlur('sleep_interval',e.currentTarget)}/></Row>
        <Row title="SponsorBlock"><NativeSelect aria-label="SponsorBlock" value={String(d.sponsorblock)} onChange={e=>void settings.save({sponsorblock:e.target.value})}><option value="off">Off</option><option value="mark">Mark chapters</option><option value="remove">Remove segments</option></NativeSelect></Row>
      </Group>
    </SettingsStack></fieldset>
    <Notice text={settings.error} error/><Notice text={settings.busy?'Saving…':settings.notice}/>
  </>;
}
