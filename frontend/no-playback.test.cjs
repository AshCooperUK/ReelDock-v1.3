// ReelDock is a media manager/downloader: no UI code may play, embed, stream or link to watch video.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript');
const React = require('react'), jsxRuntime = require('react/jsx-runtime'), { renderToStaticMarkup:renderMarkup } = require('react-dom/server');
const mantine=require('@mantine/core');const renderToStaticMarkup=tree=>renderMarkup(React.createElement(mantine.MantineProvider,{forceColorScheme:'dark'},tree));
const h = React.createElement, src = path.join(__dirname, 'src');

// 1. Source scan: every TS/TSX file, no media elements, playback APIs, embeds or watch links.
function walk(dir) { return fs.readdirSync(dir, { withFileTypes: true }).flatMap(e => e.isDirectory() ? walk(path.join(dir, e.name)) : /\.(ts|tsx)$/.test(e.name) ? [path.join(dir, e.name)] : []); }
const forbidden = [
  [/<\s*(video|audio|iframe)\b/, 'media element'], [/new\s+Audio\s*\(/, 'Audio constructor'], [/createElement\(\s*['"](video|audio|iframe)/, 'createElement media'],
  [/\.play\s*\(/, '.play()'], [/youtube-nocookie|youtube\.com\/embed/, 'YouTube embed'], [/api\/(playback|saved-playback)|m3u-stream|m3u-converted|m3u-local|\/media\/video|media-management\/file|\/convert\b/, 'playback API'],
  [/\/file'\s*\+|\/file`|view-media\/local\/[^'"`]*\/file/, 'local file stream URL'], [/>\s*Play\s*<|MovieTrailer|ManagedFilePlayer|LocalFilePlayer|StreamVideo|M3uPlayer|SearchYouTubePlayer|Watch trailer|Watch on YouTube|View on YouTube|aria-label=\{?['"]Play\b/, 'watch/play affordance'],
  [/HTMLVideoElement|new\s+MediaSource|window\.MediaSource|\bhls(js)?\b|playbackPosition|rememberPosition/i, 'player runtime'],
];
const watchLinkAllowed = new Set(['components/YouTubeDownload.tsx']);
const files = walk(src);
for (const file of files) {
  const rel = path.relative(src, file).split(path.sep).join('/'), text = fs.readFileSync(file, 'utf8');
  for (const [pattern, name] of forbidden) assert.doesNotMatch(text, pattern, rel + ' contains ' + name);
  if (!watchLinkAllowed.has(rel)) assert.doesNotMatch(text, /youtube\.com\/watch|youtu\.be\//, rel + ' links to YouTube watch pages');
}
for (const gone of ['VideoPlayer.tsx', 'StreamVideo.tsx', 'hlsPlayback.ts', 'playbackPosition.ts', 'player.css', 'viewMedia/SearchYouTubePlayer.tsx'])
  assert.ok(!fs.existsSync(path.join(src, gone)), gone + ' is removed');
function cssFiles(dir){return fs.readdirSync(dir,{withFileTypes:true}).flatMap(entry=>entry.isDirectory()?cssFiles(path.join(dir,entry.name)):entry.name.endsWith('.css')?[path.join(dir,entry.name)]:[]);}
const css=cssFiles(src).map(file=>fs.readFileSync(file,'utf8')).join('\n');
assert.doesNotMatch(css, /\.reeldock-(player|video)|\.m3u-video|\.poster-play|\.discovery-trailer|\.vm-youtube-(dialog|header)/, 'playback-only CSS removed');
// Download processing, media-server integrations and metadata stay.
for (const kept of ['VideoDownloadControls.tsx', 'settings/media-players/ServerSettings.tsx', 'FixMatch.tsx', 'MediaManagement.tsx', 'M3uLibrary.tsx']) assert.ok(fs.existsSync(path.join(src, kept)), kept + ' is kept');

// 2. Runtime renders against a small hook harness.
const generic = new Proxy({}, { get: (_, key) => key === '__esModule' ? false : key === 'default' ? undefined : props => h('div', { 'data-stub': String(key) }, props && props.children) });
function load(file, mocks = {}, react = React) {
  const out = {}, code = ts.transpileModule(fs.readFileSync(path.join(src, file), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText;
  vm.runInNewContext(code, { exports: out, URLSearchParams, AbortController, require: name => name === 'react' ? react : name === 'react/jsx-runtime' ? jsxRuntime : mocks[name] || (name.endsWith('.css') ? {} : generic) });
  return out;
}
function harness() {
  const store = [], effects = []; let cursor = 0, rerender = () => {};
  const react = { ...React,
    useState: initial => { const i = cursor++; if (!(i in store)) store[i] = typeof initial === 'function' ? initial() : initial; return [store[i], v => { store[i] = typeof v === 'function' ? v(store[i]) : v; }]; },
    useEffect: fn => { effects.push(fn); }, useRef: v => ({ current: v }), useMemo: fn => fn(), useCallback: fn => fn };
  return { react, effects, begin() { cursor = 0; effects.length = 0; } };
}
const tree = v => v == null || typeof v === 'boolean' ? [] : Array.isArray(v) ? v.flatMap(tree) : typeof v === 'object' ? [v, ...tree(v.props && v.props.children)] : [v];
const text = v => tree(v).filter(n => typeof n === 'string' || typeof n === 'number').join(' ');
const tick = () => new Promise(r => setImmediate(r));
const mediaTags = nodes => nodes.filter(n => n && typeof n === 'object' && ['video', 'audio', 'iframe', 'source'].includes(n.type));

(async () => {
  // VideoDownloadControls: status, queue, cancel, saved and read-only states; never a media request.
  const requests = []; let status = { job: null, saved: false, quality: '1080', worker_online: true };
  const api = async (url, signal, body, csrf) => { requests.push({ url, body, csrf }); return body ? {} : status; };
  const ui = { Button: props => h('button', props, props.children) };
  const panel = props => h('div', { 'data-panel': props.action }), fav = props => h('i', { 'data-fav': String(!!props.disabled) }), watch = props => h('b', { 'data-watch': String(!!props.disabled) });
  const mocks = { './api': { api }, './components/ui': ui, './downloadUI/DownloadPanel': { __esModule: true, default: panel }, './VideoFavourite': { __esModule: true, default: fav }, './collections/WatchlistButton': { __esModule: true, default: watch }, './notifications': { NotificationMessage: p => h('p', null, p.message) } };
  const run = async (props, next) => {
    if (next) status = next;
    const hx = harness(), mod = load('VideoDownloadControls.tsx', mocks, hx.react);
    let node; const draw = () => { hx.begin(); node = mod.default(props); return node; };
    draw(); const cleanups = hx.effects.map(fn => fn()); await tick(); draw();
    return { node, mod, draw, hx, cleanups };
  };
  let r = await run({ id: 'abcdefghijk', title: 'Clip', csrf: 'tok', canDownload: true });
  assert.deepEqual(requests.map(q => q.url), ['/api/downloads/abcdefghijk'], 'only the download status is read');
  assert.equal(mediaTags(tree(r.node)).length, 0);
  const find = (n, pred) => tree(n).find(x => x && typeof x === 'object' && pred(x));
  let button = find(r.node, x => x.props && x.props['data-x'] === undefined && typeof x.props.onDownload === 'function');
  assert.equal(button.props.action, 'Download'); assert.equal(button.props.disabled, false);
  button.props.onDownload(); await tick();
  assert.equal(JSON.stringify(requests.at(-1)), JSON.stringify({ url: '/api/downloads', body: { id: 'abcdefghijk', action: 'queue', quality: '1080' }, csrf: 'tok' }));
  r = await run({ id: 'abcdefghijk', title: 'Clip', csrf: 'tok', canDownload: true }, { job: { state: 'downloading', progress: 40, message: '' }, saved: false, quality: '1080', worker_online: true });
  button = find(r.node, x => x.props && typeof x.props.onDownload === 'function'); assert.equal(button.props.action, 'Cancel download');
  button.props.onDownload(); await tick(); assert.equal(requests.at(-1).body.action, 'cancel');
  assert.match(text(r.node), /Downloading/); assert.equal(find(r.node,x=>x.props?.['aria-label']==='Download progress').props.value,40,'framework progress retains the active job percentage');
  r = await run({ id: 'abcdefghijk', title: 'Clip', csrf: 'tok', canDownload: true }, { job: { state: 'complete', progress: 100, message: '' }, saved: true, quality: '1080', worker_online: true });
  assert.ok(!find(r.node, x => x.props && typeof x.props.onDownload === 'function'), 'saved videos cannot be queued again'); assert.match(text(r.node), /Saved video/);
  r = await run({ id: 'abcdefghijk', title: 'Clip', csrf: 'tok', canDownload: false }, { job: null, saved: false, quality: '720', worker_online: true });
  assert.ok(!find(r.node, x => x.props && typeof x.props.onDownload === 'function'), 'no download without permission');
  r = await run({ id: 'abcdefghijk', title: 'Clip', csrf: 'tok', canDownload: true, readOnly: true }, { job: null, saved: false, quality: '720', worker_online: true });
  assert.ok(find(r.node, x => x.props && x.props['data-fav'] === undefined && x.type === fav && x.props.disabled === true), 'favourite is disabled read-only');
  const compact = await run({ id: 'abcdefghijk', title: 'Clip', csrf: 'tok', canDownload: true, settingsOnlyDownload: true }, { job: null, saved: false, quality: '720', worker_online: true });
  assert.ok(find(compact.node, x => x.type === ui.Button && x.props['aria-label'] === 'Download video'), 'compact download icon');
  assert.ok(requests.every(q => !/playback|\/file|convert|media\/video/.test(q.url)));

  // Read-only and local-only never expose queue/cancel and never write, even with permission and CSRF.
  for (const [label, flag, next] of [['readOnly', { readOnly: true }, { job: null, saved: false, quality: '720', worker_online: true }], ['readOnly active', { readOnly: true }, { job: { state: 'downloading', progress: 5, message: '' }, saved: false, quality: '720', worker_online: true }], ['localOnly', { localOnly: true }, { job: null, saved: false, quality: '720', worker_online: true }]]) {
    const before = requests.length, held = await run({ id: 'abcdefghijk', title: 'Clip', csrf: 'tok', canDownload: true, ...flag }, next);
    assert.ok(!find(held.node, x => x.props && typeof x.props.onDownload === 'function'), label + ' has no queue/cancel panel');
    assert.ok(!find(held.node, x => x.type === ui.Button && /download/i.test(x.props['aria-label'] || '')), label + ' has no download icon');
    assert.deepEqual(requests.slice(before).map(q => q.url), ['/api/downloads/abcdefghijk'], label + ' only reads status');
    assert.ok(requests.slice(before).every(q => !q.body), label + ' wrote nothing');
  }
  // A saved video (even with an unexpected active job) offers no new queue.
  r = await run({ id: 'abcdefghijk', title: 'Clip', csrf: 'tok', canDownload: true }, { job: { state: 'complete', progress: 100, message: '' }, saved: true, quality: '720', worker_online: true });
  assert.ok(!find(r.node, x => x.props && typeof x.props.onDownload === 'function'));
  // Identity swap: the previous video's status, quality and error must not carry over to the next id.
  {
    status = { job: null, saved: false, quality: '2160', worker_online: true };
    const hx2 = harness(); let release; const gate = new Promise(res => { release = res; });
    const swapRequests = [];
    const swapMocks = { ...mocks, './api': { api: async (url, signal, body) => { swapRequests.push(url); if (/\/api\/downloads\/BBBBBBBBBBB/.test(url)) { await gate; return status; } return body ? {} : status; } } };
    const mod = load('VideoDownloadControls.tsx', swapMocks, hx2.react);
    const draw = id => { hx2.begin(); return mod.default({ id, title: 'T', csrf: 'tok', canDownload: true }); };
    draw('AAAAAAAAAAA'); hx2.effects.map(fn => fn()); await tick(); let node = draw('AAAAAAAAAAA');
    assert.ok(find(node, x => x.props && typeof x.props.onDownload === 'function'), 'first video can be queued');
    node = draw('BBBBBBBBBBB'); hx2.effects.map(fn => fn()); node = draw('BBBBBBBBBBB');
    assert.ok(!find(node, x => x.props && typeof x.props.onDownload === 'function'), 'queue controls disappear until the new video status arrives');
    assert.ok(!/error|Could not/i.test(text(node)));
    release(); await tick(); await tick(); node = draw('BBBBBBBBBBB');
    assert.ok(find(node, x => x.props && typeof x.props.onDownload === 'function'), 'new video status enables its own queue');
  }
  // One heart/bookmark pair per sheet: a header with collection controls turns the body pair off.
  for (const [showCollections, expected] of [[false, 0], [true, 1], [undefined, 1]]) {
    const held = await run({ id: 'abcdefghijk', title: 'Clip', csrf: 'tok', canDownload: true, ...(showCollections === undefined ? {} : { showCollections }) }, { job: null, saved: false, quality: '720', worker_online: true });
    assert.equal(tree(held.node).filter(x => x && typeof x === 'object' && x.type === fav).length, expected, 'favourite heart count for ' + showCollections);
    assert.equal(tree(held.node).filter(x => x && typeof x === 'object' && x.type === watch).length, expected, 'bookmark count for ' + showCollections);
    assert.ok(find(held.node, x => x.props && typeof x.props.onDownload === 'function'), 'download stays visible');
  }
  // Header callers pass showCollections={false}; collection shelves (no header controls) keep the body pair.
  for (const [file, expected] of [['ChannelDetail.tsx', false], ['ChannelVideos.tsx', false], ['Guide.tsx', false], ['viewMedia/SearchYouTubeDownload.tsx', false], ['viewMedia/ViewMedia.tsx', false], ['discovery/DiscoveryTitle.tsx', false], ['LibraryPages.tsx', false]])
    assert.match(fs.readFileSync(path.join(src, file), 'utf8'), /showCollections=\{false\}/, file);
  for (const file of ['collections/Favourites.tsx', 'collections/Watchlist.tsx']) assert.doesNotMatch(fs.readFileSync(path.join(src, file), 'utf8'), /showCollections/, file + ' keeps the default pair');

  // VideoSheet: one shared information/download/management/history sequence,
  // with caller-supplied download permissions and no player or watch link.
  const captured = []; const sheet = { DetailSheet: props => { captured.push(props); return h('section', null, props.children); } };
  const vs = load('VideoSheet.tsx', { './components/DetailSheet': sheet, './components/ui': { cn: (...a) => a.filter(Boolean).join(' ') }, './MediaManagement': { __esModule: true, default: () => h('div', { 'data-manage': 1 }), MediaTitleHistory: () => h('div', { 'data-history': 1 }) }, './ChannelIdentity': { __esModule: true, default: () => h('span', null, 'Channel') } });
  renderToStaticMarkup(h(vs.default, { csrf: 'tok', open: true, onOpenChange() {}, video: { id: 'abcdefghijk', title: 'Clip', image: '/art.jpg', channel: 'Creator' }, content: h('div', { 'data-information': 1 }), downloads: h('div', { 'data-controls': 1 }), description: 'About the clip' }));
  const props = captured.at(-1);
  assert.equal(JSON.stringify(props.tabs.map(t => [t.id, t.label])), JSON.stringify([['overview', 'Overview'], ['downloads', 'Downloads'], ['manage', 'Manage'], ['history', 'History']]));
  assert.doesNotMatch(String(props.className), /vm-youtube-dialog/); assert.equal(props.title, 'Clip');
  const overviewHtml = renderToStaticMarkup(h('div', null, props.tabs[0].content));
  assert.match(overviewHtml, /data-information/); assert.doesNotMatch(overviewHtml, /data-controls/); assert.match(overviewHtml, /About the clip/); assert.doesNotMatch(overviewHtml, /<(video|iframe|audio)|Watch|Play\b/);
  assert.match(renderToStaticMarkup(h('div', null, props.tabs[1].content)), /data-controls/);
  renderToStaticMarkup(h(vs.default, { csrf: 'tok', open: true, onOpenChange() {}, readOnly: true, video: { id: 'abcdefghijk', title: 'Clip' }, content: null }));
  assert.equal(captured.at(-1).tabs, undefined, 'read-only sheets have no Manage tab');

  // M3U details: after the descriptor resolves, remote items get server + browser downloads, saved items only the attachment.
  const descriptor = { remote: { csrf: 'x'.repeat(32), local: false, download: '/api/m3u/s/download?path=Film.strm', downloadAvailable: true, downloadReason: '' },
    local: { csrf: 'x'.repeat(32), local: true, download: '/api/m3u/s/download?path=Film.strm', downloadAvailable: true, downloadReason: '' },
    hls: { csrf: 'x'.repeat(32), local: false, download: '', downloadAvailable: false, downloadReason: 'Download this source to the server or export its stream link.' } };
  for (const [kind, info] of Object.entries(descriptor)) {
    const m3uRequests = [], hx = harness();
    const m3u = load('M3uLibrary.tsx', { '@mantine/core':mantine, './api': { api: async url => { m3uRequests.push(url); return /download-info/.test(url) ? info : {}; } }, './components/DetailSheet': { DetailSheet: p => h('section', null, p.tabs ? p.tabs.find(tab=>tab.id==='downloads').content : p.children) }, './components/ui': ui, './M3uDownloads': { ServerDownload: () => h('b', { 'data-server-download': 1 }) } }, hx.react);
    const draw = () => { hx.begin(); return m3u.M3uDetails({ source: 's', item: { title: 'Film', path: 'Film.strm', kind: 'movies' } }); };
    draw(); hx.effects.forEach(fn => fn()); await tick(); await tick();
    const html = renderToStaticMarkup(draw());
    assert.ok(m3uRequests.some(u => u.includes('/download-info?')) && m3uRequests.every(u => !/playback/.test(u)), kind);
    assert.doesNotMatch(html, /<(video|iframe|audio)|Watch on YouTube|Opening stream/, kind);
    assert.equal(/data-server-download/.test(html), kind === 'remote', kind + ' server download control');
    assert.equal(/aria-label="Download video"[^>]*href|href="[^"]*download\?path[^"]*"[^>]*aria-label="Download video"/.test(html), kind !== 'hls', kind + ' browser attachment link');
    if (kind === 'hls') assert.match(html, /disabled/);
  }
  console.log('No playback: sources, runtime download controls, video sheet and M3U details contain no player, embed, trailer or watch link.');
})().catch(e => { console.error(e); process.exit(1); });
