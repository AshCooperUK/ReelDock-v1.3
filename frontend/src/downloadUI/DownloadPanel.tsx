import { Group, Paper, Stack, Title } from '@mantine/core';
import { SelectField as NativeSelect } from '../components/SelectField';
import { NotificationMessage } from '../notifications';
import type { ReactNode } from 'react';
import { Button } from '../components/ui';
import { Toggle } from '../SettingsUI';

/** Shared presentation for each engine. Callers retain their queue/cancel semantics. */
export default function DownloadPanel({ children, quality, qualities = ['720','1080','2160'], onQuality, fallback, onFallback, action = 'Download', onDownload, disabled, busy, status, error, actions, compact=false, fill=false }: {
 children?: ReactNode; quality?: string; qualities?: string[]; onQuality?: (value: string) => void;
 fallback?: boolean; onFallback?: (value: boolean) => void; action?: string; onDownload?: () => void;
 disabled?: boolean; busy?: boolean; status?: string; error?: string; actions?: ReactNode; compact?:boolean; fill?:boolean;
}) {
 const filling=fill&&!compact;
 const feedback=<><NotificationMessage kind="info" message={status}/>{error&&<NotificationMessage kind="error" message={error}/>}</>;
 return <Paper component="section" withBorder radius="md" p="md" data-download-panel data-compact={compact||undefined} data-download-fill={filling||undefined} aria-label="Download options" style={filling?{display:'flex',flexDirection:'column',flex:'1 0 auto',minWidth:0}:undefined}><Stack gap="md" style={filling?{flex:'1 0 auto',minWidth:0}:undefined}>
  {!compact&&<Title order={3} size="h5">Download options</Title>}
  <Stack gap="md">{children}
   {onQuality && <NativeSelect aria-label="Preferred quality" value={quality} disabled={busy} onChange={e=>onQuality(e.target.value)}>{qualities.map(q=><option key={q} value={q}>{q}p</option>)}</NativeSelect>}
  </Stack>
  {onFallback && <Toggle title="Allow lower quality" detail="Allow a lower resolution if the preferred quality is unavailable." value={!!fallback} disabled={busy} onChange={onFallback}/>}
  {filling&&feedback}<Group component={filling?'footer':undefined} gap="sm" wrap="wrap" justify={filling?'flex-end':undefined} mt={filling?'auto':undefined} pt={filling?'md':undefined} style={filling?{flexShrink:0}:undefined}>{onDownload && <Button type="button" variant={compact?'secondary':undefined} disabled={disabled || busy} onClick={onDownload}>{busy ? 'Requesting…' : action}</Button>}{actions}</Group>
  {!filling&&feedback}
 </Stack></Paper>;
}
