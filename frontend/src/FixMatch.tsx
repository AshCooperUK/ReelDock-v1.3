import { Badge, Box, Fieldset, Group as MantineGroup, Image, Paper, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { UnstyledButton as ControlTrigger } from '@mantine/core';
import { SelectField as NativeSelect } from './components/SelectField';
import { useEffect, useState } from 'react';
import { Film } from 'lucide-react';
import { TextArea, TextInput } from './TextField';
import { Button, DialogDescription, DialogTitle } from './components/ui';
import { api } from './api';
import { Field, Notice, useAction } from './SettingsUI';
type Result = { id: number; media_type: string; title: string; year: string; overview: string; poster: string };
type Manual = { title?: string; year?: number | null; description?: string; poster_url?: string; genres?: string[]; rating?: number | null; cast?: string[] };
export type MatchState = { locked: boolean; mode: '' | 'tmdb' | 'manual'; data: Manual & { title?: string }; media_type: 'movie' | 'tv'; name: string; year: number | string; tmdb_saved: boolean; filename?: string; original_filename?: string; source_name?: string };
export const matchSummary = (state: MatchState) => !state.locked ? 'No manual match selected. '+(state.tmdb_saved?'Choose a TMDB result to confirm this title, or use your own details.':'Basic local metadata needs no login. Use your own details to edit it; TMDB is optional for posters, plot and cast.') : state.mode === 'manual' ? 'Using your own details. Automatic matching is locked.' : `Matched by you to “${state.data.title || 'TMDB title'}”. Automatic matching is locked.`;
export const matchFiles = (state: MatchState) => [
  ['Filename', state.filename],
  ['Original video filename', state.original_filename && state.original_filename !== state.filename ? state.original_filename : ''],
  ['Source release name', state.source_name],
].filter((row): row is [string, string] => !!row[1]);
export default function FixMatch({ source, path, csrf, onChanged, url, embedded = false }: { source?: string; path: string; csrf: string; onChanged: () => void; url?: string; embedded?: boolean }) {
  const matchUrl = url || `/api/m3u/${encodeURIComponent(source || '')}/match`;
  const [state, setState] = useState<MatchState | null>(null), [error, setError] = useState(''), [method, setMethod] = useState<'search' | 'manual'>('search');
  const [query, setQuery] = useState(''), [year, setYear] = useState(''), [type, setType] = useState<'movie' | 'tv'>('movie'), [results, setResults] = useState<Result[] | null>(null);
  const [fields, setFields] = useState({ title: '', year: '', plot: '', poster_url: '', genres: '', rating: '', cast: '' });
  const a = useAction(csrf);
  useEffect(() => { const c = new AbortController(); api<MatchState>(matchUrl + '?' + new URLSearchParams({ path }), c.signal).then(data => { if (c.signal.aborted) return; setState(data); setQuery(data.name); setYear(data.year ? String(data.year) : ''); setType(data.media_type); const d = data.mode === 'manual' ? data.data : {}; setMethod(data.mode === 'manual' ? 'manual' : 'search'); setFields({ title: d.title || data.name, year: d.year ? String(d.year) : data.year ? String(data.year) : '', plot: d.description || '', poster_url: d.poster_url || '', genres: (d.genres || []).join(', '), rating: d.rating != null ? String(d.rating) : '', cast: (d.cast || []).join(', ') }); }).catch(e => { if (!c.signal.aborted) setError(e instanceof Error ? e.message : 'Please try again.'); }); return () => c.abort(); }, [matchUrl, path]);
  async function apply(body: object, success: string) { const result = await a.run<MatchState>(matchUrl, { ...body, path }, success); if (result) { setState(result); onChanged(); } }
  async function search() { const result = await a.run<{ results: Result[] }>(matchUrl, { action: 'search', path, query, year, media_type: type }, ''); if (result) setResults(result.results); }
  const set = (key: keyof typeof fields) => (e: { target: { value: string } }) => setFields({ ...fields, [key]: e.target.value });
  return <>{!embedded&&<Stack gap="xs" component="header"><DialogTitle>Fix match</DialogTitle><DialogDescription>{state?.name || 'Title details'}</DialogDescription></Stack>}<Stack gap="lg">
    {!state ? error ? <Notice text={error} error /> : <Text size="sm" role="status">Loading match…</Text> : <>
      {matchFiles(state).length > 0 && <Stack component="dl" gap="sm" m={0}>{matchFiles(state).map(([label,value])=><Paper withBorder p="sm" radius="md" key={label}><Text component="dt" size="xs" c="dimmed">{label}</Text><Text component="dd" m={0} size="sm" style={{overflowWrap:"anywhere"}}>{value}</Text></Paper>)}</Stack>}
      <Text size="sm" role="status">{matchSummary(state)}</Text>
      {state.locked && <MantineGroup gap="sm"><Button variant="secondary" disabled={a.busy} onClick={() => void apply({ action: 'revert' }, 'Reverted. ReelDock will match this title automatically again.')}>Revert to automatic</Button></MantineGroup>}
      <Field label="Method"><NativeSelect aria-label="Match method" value={method} onChange={e => setMethod(e.target.value as 'search' | 'manual')}><option value="search">Search TMDB</option><option value="manual">Use my own details</option></NativeSelect></Field>
      {method === 'search' ? <Stack component="form" gap="md" onSubmit={e => { e.preventDefault(); void search(); }}><Fieldset variant="unstyled" p={0} disabled={a.busy || !state.tmdb_saved}><Stack gap="md">
        {!state.tmdb_saved && <Notice text="Basic local metadata works without a login. For catalogue search and richer artwork, connect TMDB in Settings > Metadata; you can also use your own details." inline />}
        <Field label="Title"><TextInput required maxLength={200} value={query} onChange={e => setQuery(e.target.value)} /></Field>
        <Field label="Year (optional)"><TextInput inputMode="numeric" pattern="[0-9]{4}" maxLength={4} value={year} onChange={e => setYear(e.target.value)} /></Field>
        <Field label="Type"><NativeSelect aria-label="Match media type" value={type} onChange={e => setType(e.target.value as 'movie' | 'tv')}><option value="movie">Film</option><option value="tv">TV series</option></NativeSelect></Field>
        <MantineGroup gap="sm"><Button type="submit">Search</Button></MantineGroup></Stack></Fieldset>
        {results && (!results.length ? <Text size="sm" role="status">No TMDB results. Try another title or year.</Text> : <Stack component="ul" gap="sm" m={0} p={0} style={{listStyle:'none'}}>{results.map(r=><Paper component="li" key={r.media_type+r.id} withBorder p="sm" radius="md"><ControlTrigger type="button" w="100%" disabled={a.busy} onClick={()=>void apply({action:'tmdb',media_type:r.media_type,tmdb_id:r.id},'Match saved and locked.')} aria-label={`Use ${r.title}${r.year?' ('+r.year+')':''}`}><MantineGroup align="flex-start" wrap="nowrap">{r.poster?<Image src={r.poster} alt="" loading="lazy" w={56} h={84} radius="sm"/>:<Film size={24}/>}<Stack gap={4} style={{flex:1,minWidth:0}}><Text fw={600}>{r.title}{r.year&&` (${r.year})`}</Text><Text size="sm" c="dimmed" lineClamp={3}>{r.overview||'No overview available.'}</Text></Stack></MantineGroup></ControlTrigger></Paper>)}</Stack>)}
      </Stack> : <Stack component="form" gap="md" onSubmit={e => { e.preventDefault(); void apply({ action: 'manual', fields }, 'Your details are saved and locked.'); }}><Fieldset variant="unstyled" p={0} disabled={a.busy}><Stack gap="md">
        <Field label="Title"><TextInput required maxLength={300} value={fields.title} onChange={set('title')} /></Field>
        <Field label="Year"><TextInput inputMode="numeric" pattern="[0-9]{4}" maxLength={4} value={fields.year} onChange={set('year')} /></Field>
        <Field label="Plot"><TextArea maxLength={10000} rows={4} value={fields.plot} onChange={set('plot')} /></Field>
        <Field label="Poster image URL"><TextInput type="url" autoComplete="off" spellCheck={false} placeholder="https://… (JPEG or PNG)" value={fields.poster_url} onChange={set('poster_url')} /></Field>
        <Field label="Genres (comma-separated)"><TextInput maxLength={1200} value={fields.genres} onChange={set('genres')} /></Field>
        <Field label="Rating (0–10)"><TextInput inputMode="decimal" maxLength={4} value={fields.rating} onChange={set('rating')} /></Field>
        <Field label="Cast (comma-separated)"><TextArea maxLength={4000} rows={2} value={fields.cast} onChange={set('cast')} /></Field>
        <MantineGroup gap="sm"><Button type="submit">Save my details</Button></MantineGroup></Stack></Fieldset></Stack>}
      <Notice text={a.error} error /><Notice text={a.notice} />
    </>}</Stack></>;
}
