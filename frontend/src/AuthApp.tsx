import { SelectField as NativeSelect } from './components/SelectField';
import { notify } from './notifications';
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { ArrowRight, Check, CircleAlert, ExternalLink, FolderOpen, Globe, Layers, ShieldCheck, Upload, Youtube } from 'lucide-react';
import { Accordion, Alert, Anchor, Box, Button, Code, Container, FileInput, Group, List, Modal, Paper, PasswordInput, Progress, Stack, Stepper, Text, Textarea, TextInput, ThemeIcon, Title } from '@mantine/core';
import { useMediaQuery } from '@mantine/hooks';

type Storage = { host_path?: string; ready: boolean; base: string; roots: string[]; writable: boolean; free_gb: number | null; names: Record<string,string> };
type Setup = { storage?: Storage; local_only?: boolean; import_method?: 'google'|'cookies'|'manual'; source_ready?: boolean; cookies_saved?: boolean; use_google_api?: boolean; admin: boolean; origin: string; csrf: string; client_id_present: boolean; client_secret_present: boolean; id_from_yaml: boolean; secret_from_yaml: boolean; youtube_connected: boolean; reconnecting?: boolean; checks: { name: string; ok: boolean }[] };
export type Bootstrap = { page: 'setup' | 'login' | 'message'; version: string; initial?: Setup; candidate?: string; saved_guide?: boolean; csrf_token?: string; message?: string; title?: string; continue_url?: string; continue_label?: string };
async function jsonRequest(url: string, options?: RequestInit) {
  const response = await fetch(url, { credentials: 'same-origin', ...options });
  const value = await response.json().catch(() => ({ error: 'The server could not complete this request. Reload and try again.' }));
  if (!response.ok) throw new Error(value.error || 'Unable to save. Please try again.');
  return value;
}
function SetupHelp({ title, children }: { title: string; children: ReactNode }) {
  return <Accordion variant="separated"><Accordion.Item value="help"><Accordion.Control>{title}</Accordion.Control><Accordion.Panel><Stack gap="md">{children}</Stack></Accordion.Panel></Accordion.Item></Accordion>;
}
const stepTitles = ['Create your account', 'Choose your server address', 'Check your media folders', 'Add your YouTube channels', 'Review your setup'];
const stepDescriptions = ['Create an administrator account for this installation.', 'Use a public HTTPS address, or skip for local use without Google API.', 'Your media folders are configured by Docker Compose.', 'Choose Google access, or add channels directly without Google.', 'Check your installation, then open ReelDock.'];

export default function AuthApp({ bootstrap }: { bootstrap: Bootstrap }) {
  const [setup, setSetup] = useState(bootstrap.initial);
  const phone = useMediaQuery('(max-width: 47.99em)');
  const compact = useMediaQuery('(max-width: 61.99em)');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(bootstrap.message || '');
  const [nextUrl, setNextUrl] = useState('');
  const [editCredentials, setEditCredentials] = useState(false);
  const googleEditButton = useRef<HTMLButtonElement|null>(null), googleEditorWasOpen = useRef(false);
  const [channels, setChannels] = useState('');
  const [cookieFile, setCookieFile] = useState<File | null>(null);
  const cookieReset = useRef<() => void>(null);
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [info, setInfo] = useState('');
  const [credits, setCredits] = useState<{ name: string; version?: string; licence?: string; url?: string; id: string }[]>([]);
  const [creditsError, setCreditsError] = useState('');
  const step = !setup?.admin ? 0 : !setup.origin ? 1 : !setup.storage?.ready ? 2 : (setup.use_google_api !== false ? (!setup.youtube_connected || setup.reconnecting) : !setup.source_ready) ? 3 : 4;
  const steps = [{ title: 'Your account', subtitle: 'A secure place to start', icon: ShieldCheck }, { title: 'Your address', subtitle: 'Connect to your home server', icon: Globe }, { title: 'Your folders', subtitle: 'Check your mounted media folders', icon: FolderOpen }, { title: 'YouTube', subtitle: 'Bring your channels together', icon: Youtube }, { title: 'Ready to go', subtitle: 'One last check', icon: Check }];
  useEffect(() => { document.title = bootstrap.page === 'setup' ? 'ReelDock · Set up' : bootstrap.page === 'login' ? 'ReelDock · Sign in' : 'ReelDock'; }, [bootstrap.page]);
  useEffect(()=>{if(editCredentials)googleEditorWasOpen.current=true;else if(googleEditorWasOpen.current){googleEditorWasOpen.current=false;googleEditButton.current?.focus();}},[editCredentials]);
  async function act(action: string, values: Record<string, unknown> = {}) {
    if (!setup || busy) return;
    setBusy(true); setError('');
    try {
      const result = await jsonRequest('/api/setup/' + action, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': setup.csrf }, body: JSON.stringify(values) });
      if (action === 'verify-route') { setNextUrl(result.continue_url); return; }
      if (action === 'connect-google' || action === 'finish') { window.location.assign(result.url); return; }
      setSetup(await jsonRequest('/api/setup/state')); setEditCredentials(false);
    } catch (e) { setError(e instanceof Error ? e.message : 'Please try again.'); }
    finally { setBusy(false); }
  }
  async function importSource(kind: 'cookies' | 'manual') {
    if (!setup || busy) return;
    setBusy(true); setError('');
    try {
      if (kind === 'cookies') {
        if (!cookieFile) throw new Error('Choose cookies.txt first.');
        const data = new FormData(); data.append('cookies', cookieFile);
        const saved = await jsonRequest('/api/setup/cookies', { method: 'POST', headers: { 'X-CSRF-Token': setup.csrf }, body: data });
        if (saved.saved !== true) throw new Error('The server did not confirm that the cookies were saved. Please try again.');
      } else {
        if (!channels.trim()) throw new Error('Enter a channel link or nickname, or choose a CSV.');
        await jsonRequest('/api/setup/channels', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': setup.csrf }, body: JSON.stringify({ channels }) });
      }
      await jsonRequest('/api/setup/source-ready', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': setup.csrf }, body: '{}' });
      setSetup(await jsonRequest('/api/setup/state'));
      if (kind === 'cookies') { setCookieFile(null); cookieReset.current?.(); }
      notify({ kind: 'success', message: kind === 'cookies' ? 'Cookies saved and applied.' : 'Channels imported.' });
    } catch (e) { setError(e instanceof Error ? e.message : 'Import could not be saved.'); }
    finally { setBusy(false); }
  }
  async function chooseCsv(file: File | null) {
    setCsvFile(file);
    if (!file) return;
    if (file.size > 500000) { setCsvFile(null); setError('Choose a CSV smaller than 500 KB.'); return; }
    try { setChannels(await file.text()); setError(''); }
    catch { setCsvFile(null); setError('The CSV could not be read. Choose it again or paste channel links.'); }
  }
  function submit(action: string, event: FormEvent<HTMLFormElement>) { event.preventDefault(); void act(action, Object.fromEntries(new FormData(event.currentTarget).entries()) as Record<string, string>); }
  async function showInfo(name: string) {
    setInfo(name);
    if (name === 'About ReelDock') {
      try { setCreditsError(''); const response = await jsonRequest('/api/credits'); setCredits(response.items || []); }
      catch { setCreditsError('Credits could not be loaded. Close this panel and try again.'); }
    }
  }
  const actions = { gap: 'sm', wrap: 'wrap' } as const;
  const passwordToggle = { 'aria-label': 'Show or hide password' };
  const settingUp=bootstrap.page==='setup';
  return <Box mih="100dvh"><Container size={settingUp?'56rem':'28rem'} py={{base:'lg',sm:'xl'}}>
    <Stack gap="xl">
      <Group justify="center"><Anchor href="/app/" c="inherit" underline="never" aria-label="ReelDock home"><Group gap="sm" wrap="nowrap"><ThemeIcon size={44} radius="md" variant="light"><Layers size={25}/></ThemeIcon><Text size="xl" fw={700}>ReelDock</Text></Group></Anchor></Group>
      {settingUp&&<Stack gap="md"><Stack gap={4}><Title order={1} size="h2">Set up ReelDock</Title><Text c="dimmed">Create your account and connect your media in five steps.</Text></Stack>{compact?<Stack gap="xs"><Text size="sm" fw={500}>Step {step+1} of 5 · {steps[step].title}</Text><Progress value={(step+1)*20} aria-label={`Setup step ${step+1} of 5`}/></Stack>:<Stepper active={step} allowNextStepsSelect={false} size="sm">{steps.map(item=><Stepper.Step key={item.title} label={item.title} icon={<item.icon size={18}/>}/>)}</Stepper>}</Stack>}
      <Paper component="main" withBorder radius="md" p={{base:'md',sm:'xl'}} aria-busy={busy}>
        <Stack gap="lg">
          {bootstrap.page==='message'?<><Title order={1} size="h3">{bootstrap.title}</Title><Text c="dimmed">{bootstrap.message}</Text><Button component="a" href={bootstrap.continue_url||'/setup'} rightSection={<ArrowRight size={16}/>} fullWidth>{bootstrap.continue_label||'Return to setup'}</Button></>:bootstrap.page==='login'?<>
            <Stack gap={4}><Title order={1} size="h2">Sign in</Title><Text c="dimmed">Open your library, guide and activity.</Text></Stack>
            {error&&<Alert color="red" title="Could not sign in" role="alert">{error}</Alert>}
            <Box component="form" method="post" action="/login" onSubmit={()=>setBusy(true)}><Stack gap="md"><input type="hidden" name="csrf" value={bootstrap.csrf_token}/><TextInput inputWrapperOrder={['label','input','description','error']} label="Username" name="username" autoComplete="username" required autoFocus/><PasswordInput inputWrapperOrder={['label','input','description','error']} label="Password" name="password" autoComplete="current-password" required maxLength={256} visibilityToggleFocusable visibilityToggleButtonProps={passwordToggle}/><Button type="submit" disabled={busy} loading={busy} fullWidth>{busy?'Signing in…':'Sign in'}</Button></Stack></Box>
          </>:nextUrl?<><Title order={2} size="h3">Your address is ready</Title><Text c="dimmed">Continue at your verified address and sign in with the account you just created.</Text><Button component="a" href={nextUrl} rightSection={<ArrowRight size={16}/>} fullWidth={phone}>Continue securely</Button></>:<>
            <Stack gap={4}><Text size="sm" c="dimmed">STEP {step+1} OF 5</Text><Title order={2} size="h3">{stepTitles[step]}</Title><Text c="dimmed">{stepDescriptions[step]}</Text></Stack>
            {error&&<Alert color="red" title="Could not continue" role="alert">{error}</Alert>}
                {step === 0 && <Box component="form" onSubmit={event => submit('admin', event)}><Stack gap="md">
                  <TextInput inputWrapperOrder={['label','input','description','error']} label="Username" description="3–64 letters, numbers, dots, underscores or hyphens." name="username" autoComplete="username" pattern="[A-Za-z0-9_.\-]{3,64}" minLength={3} maxLength={64} required disabled={busy} />
                  <PasswordInput inputWrapperOrder={['label','input','description','error']} label="Password" description="At least 12 characters." name="password" autoComplete="new-password" minLength={12} maxLength={256} required disabled={busy} visibilityToggleFocusable visibilityToggleButtonProps={passwordToggle} />
                  <PasswordInput inputWrapperOrder={['label','input','description','error']} label="Confirm password" name="confirm" autoComplete="new-password" minLength={12} maxLength={256} required disabled={busy} visibilityToggleFocusable visibilityToggleButtonProps={passwordToggle} />
                  <Group {...actions}><Button disabled={busy} loading={busy} type="submit" rightSection={<ArrowRight size={16} />} fullWidth={phone}>{busy ? 'Creating account…' : 'Create account'}</Button></Group>
                </Stack></Box>}
                {step === 1 && <Box component="form" onSubmit={event => submit('verify-route', event)}><Stack gap="md">
                  <TextInput inputWrapperOrder={['label','input','description','error']} label="Public HTTPS address" name="url" type="url" defaultValue={bootstrap.candidate || ''} placeholder="https://reeldock.example.com" required disabled={busy} />
                  <SetupHelp title="Help with your route"><Text>Point your published route to HTTP and the same server address and port you used to open this setup page. Leave the path empty. ReelDock checks DNS, HTTPS and a unique response from this installation.</Text></SetupHelp>
                  <Group {...actions}><Button disabled={busy} loading={busy} type="submit" rightSection={<ArrowRight size={16} />} fullWidth={phone}>{busy ? 'Checking address…' : 'Verify address'}</Button><Button variant="default" disabled={busy} type="button" onClick={() => act('skip-route')} fullWidth={phone}>Skip — use locally</Button></Group>
                </Stack></Box>}
                {step === 2 && setup?.storage && <Stack gap="md">
                  <Text>Mount the ReelDock media directory and make its folders writable by the container user. Directory changes are managed in Docker Compose.</Text>
                  <Code block style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{setup.storage.base || 'Media mount not configured'}</Code>
                  <Group {...actions}><Button type="button" disabled={busy} loading={busy} onClick={() => act('storage', {})} fullWidth={phone}>{busy ? 'Checking folders…' : 'Check folders again'}</Button></Group>
                </Stack>}
                {step === 3 && setup && <Stack gap="lg">
                  <NativeSelect aria-label="Import your channels" value={setup.import_method || 'google'} disabled={busy} onChange={event => act('import-method', { method: event.currentTarget.value })}>
                    <option value="google" disabled={setup.local_only}>Google API</option><option value="cookies">cookies.txt</option><option value="manual">CSV or channel nickname</option>
                  </NativeSelect>
                  <Text size="sm" c="dimmed">ReelDock prefers Google API, then signed-in cookies, then channels you import manually. Routine upload checks use public feeds to save API allowance.</Text>
                  {setup.use_google_api !== false && <Stack gap="md">
                    <SetupHelp title="Connect with Google Cloud"><Text>Create a Web application OAuth client, enable YouTube Data API v3 and configure the consent screen. Add your account as a test user if needed.</Text><Group {...actions}><Button component="a" variant="default" href="https://console.cloud.google.com/" target="_blank" rel="noopener noreferrer" rightSection={<ExternalLink size={16} />} fullWidth={phone}>Open Google Cloud</Button></Group></SetupHelp>
                    <TextInput inputWrapperOrder={['label','input','description','error']} label="Google redirect address" readOnly value={setup.origin + '/oauth/google/callback'} onFocus={event => event.target.select()} />
                    {(!setup.client_id_present || !setup.client_secret_present || editCredentials) && <Box component="form" id="google-credentials" onSubmit={event => submit('google', event)}><Stack gap="md">
                      {!setup.id_from_yaml && <TextInput inputWrapperOrder={['label','input','description','error']} label="Client ID" name="client_id" autoComplete="off" autoFocus={editCredentials} placeholder={setup.client_id_present ? 'Saved · leave blank to keep' : '…apps.googleusercontent.com'} required={!setup.client_id_present} disabled={busy} />}
                      {!setup.secret_from_yaml && <PasswordInput inputWrapperOrder={['label','input','description','error']} label="Client secret" name="client_secret" autoComplete="off" autoFocus={editCredentials&&setup.id_from_yaml} placeholder={setup.client_secret_present ? 'Saved · leave blank to keep' : 'Your Google client secret'} required={!setup.client_secret_present} disabled={busy} visibilityToggleFocusable visibilityToggleButtonProps={{ 'aria-label': 'Show or hide client secret' }} />}
                      <Group {...actions}><Button type="submit" disabled={busy} loading={busy} fullWidth={phone}>{busy ? 'Saving…' : 'Save connection details'}</Button>{setup.client_id_present&&setup.client_secret_present&&<Button type="button" variant="default" disabled={busy} aria-expanded={editCredentials} aria-controls="google-credentials" onClick={()=>setEditCredentials(false)} fullWidth={phone}>Cancel editing</Button>}</Group>
                    </Stack></Box>}
                    {setup.client_id_present && setup.client_secret_present && !editCredentials && <Stack gap="md"><Group gap="xs"><Check size={18} /><Text size="sm">Connection details saved</Text></Group><Group {...actions}><Button type="button" disabled={busy} loading={busy} onClick={() => act('connect-google')} rightSection={<ArrowRight size={16} />} fullWidth={phone}>Connect YouTube</Button>{!(setup.id_from_yaml && setup.secret_from_yaml) && <Button ref={googleEditButton} variant="default" type="button" disabled={busy} aria-expanded={false} aria-controls="google-credentials" onClick={() => setEditCredentials(true)} fullWidth={phone}>Edit saved connection details</Button>}</Group></Stack>}
                    <Text size="sm" c="dimmed">Your secrets are never displayed. You can add cookies later as a fallback.</Text>
                  </Stack>}
                  {setup.import_method === 'cookies' && <Stack gap="md">
                    <Text size="sm" c="dimmed">Upload a Netscape cookies.txt export from a signed-in YouTube session. ReelDock will import your YouTube channels after setup. If YouTube rejects the session, you can use CSV or channel links instead.</Text>
                    <FileInput inputWrapperOrder={['label','input','description','error']} label="YouTube cookies.txt" aria-label="YouTube cookies.txt" name="cookies" accept=".txt" placeholder="Choose cookies.txt" leftSection={<Upload size={18} />} value={cookieFile} onChange={setCookieFile} resetRef={cookieReset} disabled={busy} clearable clearButtonProps={{ 'aria-label': 'Clear cookies file' }} />
                    <Group {...actions}><Button type="button" disabled={busy || !cookieFile} loading={busy} onClick={() => importSource('cookies')} rightSection={<ArrowRight size={16} />} fullWidth={phone}>Save cookies and continue</Button>{setup.cookies_saved && <Button type="button" variant="default" disabled={busy} onClick={() => act('source-ready')} fullWidth={phone}>Use saved cookies</Button>}</Group>
                  </Stack>}
                  {setup.import_method === 'manual' && <Stack gap="md">
                    <Textarea inputWrapperOrder={['label','input','description','error']} label="Channel links or @nicknames" description="One per line. Use the channel’s unique @handle rather than its display name." aria-label="Channel links or @nicknames" value={channels} onChange={event => setChannels(event.currentTarget.value)} minRows={4} autosize maxRows={10} maxLength={500000} disabled={busy} />
                    <FileInput inputWrapperOrder={['label','input','description','error']} label="YouTube CSV" aria-label="YouTube CSV" name="channels_csv" description="Choose a CSV smaller than 500 KB, or paste channel links above." accept=".csv,text/csv" placeholder="Choose CSV file" leftSection={<Upload size={18} />} value={csvFile} onChange={file => void chooseCsv(file)} disabled={busy} clearable clearButtonProps={{ 'aria-label': 'Clear CSV file' }} />
                    <Group {...actions}><Button type="button" disabled={busy || !channels.trim()} loading={busy} onClick={() => importSource('manual')} rightSection={<ArrowRight size={16} />} fullWidth={phone}>Import and continue</Button><Button type="button" variant="default" disabled={busy} onClick={() => act('source-ready')} fullWidth={phone}>Add channels later</Button></Group>
                  </Stack>}
                </Stack>}
                {step === 4 && setup && <Stack gap="md">
                  <List spacing="sm" center>{setup.checks.map(check => <List.Item key={check.name} icon={<ThemeIcon size={24} color={check.ok ? 'teal' : 'orange'} variant="light">{check.ok ? <Check size={16} /> : <CircleAlert size={16} />}</ThemeIcon>}>{check.name}</List.Item>)}</List>
                  <Text size="sm" c="dimmed">Upload notifications and background checks will keep your channels up to date. You can add channels in YouTube settings.</Text>
                  <Group {...actions}><Button type="button" disabled={busy || setup.checks.some(check => !check.ok)} loading={busy} onClick={() => act('finish')} rightSection={<ArrowRight size={16} />} fullWidth={phone}>{busy ? 'Finishing…' : 'Open ReelDock'}</Button></Group>
                </Stack>}

            {bootstrap.saved_guide&&<Button component="a" variant="default" href="/guide/" fullWidth={phone}>Open saved library</Button>}
          </>}
        </Stack>
      </Paper>
      <Stack gap="md" align="center"><Group component="nav" gap="xs" justify="center" aria-label="Information">{['About ReelDock','Privacy','TMDB credits','Responsible use'].map(name=><Button type="button" variant="subtle" key={name} onClick={()=>showInfo(name)}>{name}</Button>)}</Group><Text size="xs" c="dimmed">{bootstrap.version}</Text></Stack>
    </Stack>
  </Container>
    <Modal opened={Boolean(info)} onClose={() => setInfo('')} title={info} size="lg" centered>
      <Stack gap="md"><Text c="dimmed" size="sm">{info === 'Privacy' ? 'Your data stays with your installation.' : info === 'TMDB credits' ? 'With thanks to The Movie Database and its community.' : 'Your media, in one place.'}</Text>
        {info === 'Responsible use' ? <><Text>ReelDock is independent of YouTube and Google. Use media only where you have the necessary rights and permissions. Personal use alone does not establish permission.</Text><Text>Check the content owner’s permissions, applicable law and the terms of each service. Account access or a cookies file does not itself grant download rights.</Text><Anchor href="https://www.youtube.com/t/terms" target="_blank" rel="noopener noreferrer">YouTube terms</Anchor><Anchor href="https://developers.google.com/youtube/terms/developer-policies" target="_blank" rel="noopener noreferrer">YouTube API policies</Anchor></> : info === 'Privacy' ? <><Text>ReelDock stores your account, connection credentials, library and preferences in its own data folder. Sign-in uses a session cookie. Your appearance preference is saved in this browser for this ReelDock address.</Text><Text>Connected services receive the requests needed for features you use. Google provides authorisation and YouTube metadata. Optional TMDB matching sends media identifiers, not playlist stream addresses. Artwork, metadata and download requests contact the providers you configure.</Text></> : info === 'TMDB credits' ? <><Box component="img" src="/static/tmdb-logo.svg" alt="TMDB" w={140} maw="100%" /><Text>This product uses the TMDB API but is not endorsed or certified by TMDB.</Text><Anchor href="https://www.themoviedb.org" target="_blank" rel="noopener noreferrer">Visit TMDB</Anchor></> : <><Text>ReelDock brings your guide, YouTube channels and media library together. {bootstrap.version}</Text><Anchor href="https://github.com/AshCooperUK/ReelDock-v1.3" target="_blank" rel="noopener noreferrer">ReelDock on GitHub</Anchor><Title order={3} size="h4">Software credits</Title>{creditsError && <Alert color="red" role="alert">{creditsError}</Alert>}{credits.map((item, index) => <Stack key={index} gap={4}><Text><strong>{item.name}</strong> {item.version} · {item.licence || 'See licence notice'}</Text><Group gap="sm"><Anchor href={'/licenses/' + encodeURIComponent(item.id)} target="_blank" rel="noopener noreferrer">Full licence</Anchor>{item.url && <Anchor href={item.url} target="_blank" rel="noopener noreferrer">Project</Anchor>}</Group></Stack>)}</>}
      </Stack>
    </Modal>
  </Box>;
}
