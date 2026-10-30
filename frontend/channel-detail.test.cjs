// CHANNEL-01: channel popup tabs, header, overview, settings safety, releases, forecasts and history.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript');
const React = require('react'), { renderToStaticMarkup:renderMarkup } = require('react-dom/server');
const mantine=require('@mantine/core');
const renderToStaticMarkup=tree=>renderMarkup(React.createElement(mantine.MantineProvider,{forceColorScheme:'dark'},tree));
const src = path.join(__dirname, 'src');
const jsx = (type, props, key) => ({ type, props: props || {}, key });
const listeners = new Map();
const windowStub = { addEventListener: (n, f) => listeners.set(n, f), removeEventListener: n => listeners.delete(n), dispatchEvent: e => listeners.get(e.type)?.(e) };
class CustomEvent { constructor(type, options) { this.type = type; this.detail = options?.detail; } }
const observers = [];
class FakeResizeObserver { constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); } observe(element) { this.element = element; } disconnect() { this.disconnected = true; } }
function compile(file, imports, globals = {}, real = false) {
  const out = {}, code = ts.transpileModule(fs.readFileSync(path.join(src, file), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  vm.runInNewContext(code, { exports: out, Error: SandboxError, ResizeObserver: FakeResizeObserver, window: windowStub, CustomEvent, URLSearchParams, Intl, Date, AbortController, Event: class { constructor(t) { this.type = t; } }, ...globals,
    require: n => n === '@mantine/core' && real ? mantine : n === './components/ui' && real ? {Button:mantine.Button} : n === 'react/jsx-runtime' ? (real ? require('react/jsx-runtime') : { jsx, jsxs: jsx, Fragment: 'fragment' }) : imports[n] || (n.endsWith('.css') ? {} : new Proxy({}, { get: (_, key) => key === '__esModule' ? false : key })) });
  return out;
}
// Minimal hook runtime: state/refs persist across renders; effects run only when asked.
function hooks() {
  const slots = {}; let slot = 'main', s = 0, r = 0, e = 0, effects = [], cleanups = [];
  const mem = () => slots[slot] ||= { state: [], refs: [] };
  const react = {
    useState: initial => { const m = mem(), i = s++; if (!(i in m.state)) m.state[i] = typeof initial === 'function' ? initial() : initial; return [m.state[i], v => { m.state[i] = typeof v === 'function' ? v(m.state[i]) : v; }]; },
    useRef: v => { const m = mem(), i = r++; if (!(i in m.refs)) m.refs[i] = { current: v }; return m.refs[i]; },
    lazy:()=> 'LazyChannelDetail', Suspense:'Suspense',
    useId: () => 'id-' + (e++), useMemo: f => f(), useCallback: f => f,
    useEffect: (f, deps) => { effects.push({ f, deps }); }, useLayoutEffect: (f, deps) => { effects.push({ f, deps }); },
  };
  return { react, begin(name = 'main') { slot = name; s = 0; r = 0; e = 0; effects = []; }, runEffects() { const list = effects; effects = []; for (const c of cleanups) c?.(); cleanups = list.map(x => x.f()); }, cleanup() { for (const c of cleanups) c?.(); cleanups = []; } };
}
const tree = v => v == null || typeof v === 'boolean' ? [] : Array.isArray(v) ? v.flatMap(tree) : typeof v === 'object' ? [v, ...tree(v.props && v.props.children), ...tree(v.props && v.props.media), ...tree(v.props && v.props.actions)] : [v];
const text = v => tree(v).filter(x => typeof x === 'string' || typeof x === 'number').join(' ');
const find = (v, pred) => tree(v).find(x => x && typeof x === 'object' && pred(x));
const tick = () => new Promise(r => setImmediate(r));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const SandboxError = vm.runInNewContext('Error');
const CH = 'UC' + 'a'.repeat(22), OTHER = 'UC' + 'b'.repeat(22);

(async () => {
  // ---- ChannelDialog: tabs, header, settings safety, releases, video swap ----
  function scene({ items = [{ id: CH, name: 'Channel', handle: '@chan', avatar: '/a.jpg', banner: '/b.jpg' }], readOnly = false, info = { name: 'Channel', description: 'About', subscribers: 5, handle: '@chan' } } = {}) {
    const calls = [], h = hooks();
    const api = async (...args) => { calls.push(args); return {}; };
    const useSettings = url => url.startsWith('/api/subscriptions') ? { data: { items, csrf: 'sub-csrf' }, setData() {}, reload() {}, error: '' } : { data: { channel: info, csrf: 'info-csrf' }, reload() {}, error: '' };
    const detailImports = { react: h.react, './api': { api }, './SettingsUI': { useSettings, Loading: 'Loading' }, './ChannelAvatar': { default: 'ChannelAvatar' },
      './ChannelInfo': { ChannelUploads: 'ChannelUploads' }, './ChannelOverview': { default: 'ChannelOverview' }, './ChannelForecasts': { default: 'ChannelForecasts' }, './ChannelHistory': { default: 'ChannelHistory' },
      './ChannelOptions': { default: 'ChannelOptions' }, './VideoSheet': { default: 'VideoSheet' }, './VideoDownloadControls': { default: 'VideoDownloadControls' }, './components/ui': { Button: 'Button' },
      './components/DetailSheet': { DetailSheet: 'DetailSheet' }, './collections/TileActions': { default: 'TileActions' }, './notifications': { NotificationMessage: 'NotificationMessage' },
      './channelNavigation': { channelEvent: 'reeldock-open-channel' }, './appNavigation': { channelParam: () => null, withChannel: () => '/app/' } };
    const detailModule=compile('ChannelDetail.tsx',detailImports);
    const dialog=compile('ChannelDialog.tsx',detailImports,
      { location: { href: 'https://x/app/', pathname: '/app/', search: '', hash: '' }, history: { state: null, replaceState() {} } });
    function open(mode) { h.begin('dialog'); const host = dialog.default({ readOnly }); h.runEffects(); listeners.get('reeldock-open-channel')({ type: 'reeldock-open-channel', detail: { id: CH, name: 'Fallback', mode } }); h.begin('dialog'); const again = dialog.default({ readOnly }); return again; }
    function detail(treeValue) { const el = find(treeValue,x=>x.type==='LazyChannelDetail'); h.begin('detail'); return detailModule.default(el.props); }
    return { calls, h, open, detail, dialog };
  }
  const labels = ['Overview', 'Downloads', 'Manage', 'History'], ids = ['overview', 'downloads', 'manage', 'history'];
  for (const [mode, active] of [[undefined, 'overview'], ['info', 'overview'], ['options', 'manage'], ['overview', 'overview'], ['settings', 'manage'], ['releases', 'downloads'], ['history', 'history']]) {
    const sc = scene(), sheet = sc.detail(sc.open(mode));
    assert.equal(sheet.type, 'DetailSheet');
    assert.equal(JSON.stringify(sheet.props.tabs.map(t => [t.id, t.label])), JSON.stringify(ids.map((id, i) => [id, labels[i]])), 'four tabs in order');
    assert.equal(sheet.props.tab, active, String(mode) + ' opens ' + active);
  }
  // Header: large square avatar through DetailSheet.media, plain-text name, handle/status subtitle, banner.
  let sc = scene(), sheet = sc.detail(sc.open());
  assert.equal(sheet.props.title, 'Channel');
  assert.equal(sheet.props.media.type,'ChannelAvatar');assert.equal(sheet.props.media.props.detail,true);
  assert.ok(find(sheet.props.media, x => x.type === 'ChannelAvatar' && x.props.src === '/a.jpg'));
  assert.match(text(sheet.props.subtitle), /@chan\s+·\s+YouTube Channel\s+·\s+Channel added/);
  const channelLink=find(sheet.props.subtitle,x=>x.type==='Anchor');
  assert.equal(text(channelLink),'YouTube Channel','header link label has no trailing dash or status text');
  assert.equal(channelLink.props.href,'https://www.youtube.com/channel/'+CH);
  assert.equal(channelLink.props.target,'_blank');assert.equal(channelLink.props.rel,'noopener noreferrer');
  assert.equal(channelLink.props.underline,'always');assert.match(channelLink.props['aria-label'],/Open Channel on YouTube.*new tab/);
  assert.equal(channelLink.props.onClick,undefined,'native external link does not trigger channel navigation/settings/download actions');
  assert.equal(sheet.props.banner, '/b.jpg');
  assert.ok(!sheet.props.className,'the shared detail does not depend on a legacy channel skin');
  assert.ok(find(sheet.props.actions, x => x.type === 'TileActions'));
  // Overview body uses the public info.
  assert.equal(find(sheet.props.tabs[0].content, x => x.type === 'ChannelOverview').props.channel.description, 'About');
  // Settings: subscribed -> ChannelOptions embedded + note; nothing is written when the tab is read or opened.
  let settingsBody = sheet.props.tabs[2].content;
  assert.equal(find(settingsBody, x => x.type === 'ChannelOptions').props.embedded, true);
  assert.equal(find(settingsBody,x=>x.type==='ChannelOptions').props.csrf,'sub-csrf','settings keep the authenticated subscription contract');
  assert.equal(find(settingsBody,x=>x.type==='ChannelOptions').props.channel.id,CH);
  sc.h.runEffects(); assert.equal(sc.calls.length,1,'opening queues only this channel recent-upload priority');assert.equal(sc.calls[0][0],'/api/guide/channel/'+CH);assert.equal(sc.calls[0][2].action,'prioritise_recent');assert.equal(sc.calls[0][3],'sub-csrf');
  // Unsubscribed: honest panel, still four tabs, no writable options.
  sc = scene({ items: [] }); sheet = sc.detail(sc.open('settings'));
  assert.equal(sheet.props.tabs.length, 4);
  assert.ok(!find(sheet.props.tabs[2].content, x => x.type === 'ChannelOptions'));
  assert.match(text(sheet.props.tabs[2].content), /Add this channel to ReelDock/);
  assert.match(text(sheet.props.subtitle), /Not added/);
  const addFavourite=find(sheet.props.actions,x=>x.type==='Button'&&text(x)==='Add channel');
  assert.ok(addFavourite);addFavourite.props.onClick();await tick();
  assert.equal(sc.calls.length,1);assert.equal(sc.calls[0][0],'/api/discover/subscribe');
  assert.equal(sc.calls[0][2].action,'subscribe');assert.equal(sc.calls[0][2].item.channelId,CH);assert.equal(sc.calls[0][3],'sub-csrf');
  assert.ok(!sc.calls.some(call=>/channel-favourite|watchlist/.test(call[0])),'YouTube channels keep their subscription contract and do not toggle the independent Favourite or Watchlist');
  // Read-only: no writable ChannelOptions is mounted at all.
  sc = scene({ readOnly: true }); sheet = sc.detail(sc.open('settings'));
  assert.ok(!find(sheet.props.tabs[2].content, x => x.type === 'ChannelOptions'));
  assert.match(text(sheet.props.tabs[2].content), /read-only/i);
  // Releases: uploads + forecasts; opening a video swaps in the unchanged VideoSheet and closing returns to Releases.
  sc = scene(); let top = sc.open('releases'); sheet = sc.detail(top);
  const uploads = find(sheet.props.tabs[1].content, x => x.type === 'ChannelUploads');
  assert.equal(uploads.props.title, 'Uploads'); assert.equal(typeof uploads.props.onVideoOpen, 'function');
  assert.ok(find(sheet.props.tabs[1].content, x => x.type === 'ChannelForecasts' && x.props.id === CH));
  const releaseNodes = tree(sheet.props.tabs[1].content), forecastAt = releaseNodes.findIndex(x => x && x.type === 'ChannelForecasts'), uploadsAt = releaseNodes.findIndex(x => x && x.type === 'ChannelUploads');
  assert.ok(forecastAt >= 0 && forecastAt < uploadsAt, 'estimated uploads come before the paged uploads list');
  uploads.props.onVideoOpen({ id: 'abcdefghijk', title: 'Clip', state: 'published' });
  const swapped = sc.detail(top);
  assert.equal(swapped.type, 'VideoSheet');
  assert.ok(!('player' in swapped.props)); assert.equal(find(swapped.props.downloads, x => x.type === 'VideoDownloadControls').props.showCollections, false);
  assert.equal(find(swapped.props.downloads, x => x.type === 'VideoDownloadControls').props.canDownload, true);
  swapped.props.onOpenChange(false);
  const back = sc.detail(top);
  assert.equal(back.type, 'DetailSheet'); assert.equal(back.props.tab, 'downloads', 'closing the video returns to the previous channel tab');
  assert.equal(find(sc.detail(sc.open('history')).props.tabs[3].content, x => x.type === 'ChannelHistory').props.id, CH);
  sc.h.cleanup();

  // ---- ChannelOverview ----
  const infoModule = compile('ChannelInfo.tsx', { './VideoTitle': { default: 'VideoTitle' }, './mediaDate': compile('mediaDate.ts', {}), './Description': { default: 'Description' }, './ChannelVideos': { default: 'ChannelVideos' }, './SettingsUI': { Group: 'Group', Row: 'Row', Loading: 'Loading', useSettings: () => ({}) } });
  const overviewOf = channel => { const mod = compile('ChannelOverview.tsx', { react: React, './ChannelInfo': infoModule }, {}, true); return renderToStaticMarkup(React.createElement(mod.default, { channel })); };
  const long = 'x'.repeat(5000);
  let html = overviewOf({ description: long + ' end', subscribers: 0, total_views: 0, video_count: 0, handle: '@chan', country: 'GB' });
  assert.match(html, /aria-expanded="false"/); assert.match(html, />More</); assert.match(html, /data-line-clamp="true"/);
  assert.match(html, /aria-controls="/); assert.match(html, /<dl[ >]/);
  assert.match(html, /<dt[^>]*>Subscribers<\/dt><dd[^>]*>0<\/dd>/, 'zero statistics are kept'); assert.match(html, /<dt[^>]*>Public videos<\/dt><dd[^>]*>0<\/dd>/);
  assert.doesNotMatch(html, /Recent average|Language|Created|Topics/, 'unknown facts are omitted, none invented');
  assert.doesNotMatch(html, /<a /, 'description is plain text, never linked');
  html = overviewOf({ description: 'https://example.com/watch?v=1 <b>bold</b>' });
  assert.doesNotMatch(html, /<a |<b>/); assert.match(html, /&lt;b&gt;bold&lt;\/b&gt;/);
  assert.doesNotMatch(overviewOf({ description: 'short' }), />More</, 'short descriptions have no More button');
  assert.match(overviewOf({}), /No channel description/);
  { const hx = hooks(), mod = compile('ChannelOverview.tsx', { react: hx.react, './ChannelInfo': infoModule }); hx.begin(); let out = mod.default({ channel: { description: long } });
    const button = find(out, x => x.props?.['aria-expanded']!==undefined&&typeof x.props?.onClick==='function'); assert.equal(button.props['aria-expanded'], false); button.props.onClick(); hx.begin(); out = mod.default({ channel: { description: long } });
    const expanded = find(out, x => x.props?.['aria-expanded']!==undefined&&typeof x.props?.onClick==='function'); assert.equal(expanded.props['aria-expanded'], true); assert.equal(text(expanded), 'Show less'); assert.equal(find(out,x=>x.type==='Text'&&x.props.id).props.lineClamp,undefined); }

  // Overflow is measured, not guessed from length alone.
  { const mod = compile('ChannelOverview.tsx', { react: hooks().react, './ChannelInfo': infoModule });
    const run = (description, el) => { const hx = hooks(), mod2 = compile('ChannelOverview.tsx', { react: hx.react, './ChannelInfo': infoModule }); const draw = () => { hx.begin(); return mod2.default({ channel: { description } }); };
      let out = draw(); const paragraph = find(out, x => x.type === 'Text' && x.props.id); paragraph.props.ref.current = el; hx.runEffects(); out = draw(); return { draw, hx, out, paragraph, hasMore: () => !!find(hx.out ?? out, () => false) }; };
    const more = out => find(out, x => x.props?.['aria-expanded']!==undefined&&typeof x.props?.onClick==='function' && /More|Show less/.test(text(x)));
    let el = { scrollHeight: 40, clientHeight: 40 }, c = run('A short normal description.', el); assert.ok(!more(c.draw()), 'short text that fits has no More'); c.hx.cleanup();
    el = { scrollHeight: 40, clientHeight: 40 }; c = run('one\ntwo\nthree\nfour\nfive', el); assert.ok(more(c.draw()) === undefined, 'measurement says it fits, so the newline guess is corrected'); c.hx.cleanup();
    { const hx = hooks(), m = compile('ChannelOverview.tsx', { react: hx.react, './ChannelInfo': infoModule }); hx.begin(); const first = m.default({ channel: { description: 'one\ntwo\nthree\nfour\nfive' } }); assert.ok(find(first, x => x.props?.['aria-expanded']!==undefined&&typeof x.props?.onClick==='function'), 'unmeasured multi-line text shows More from the length/newline guess'); }
    el = { scrollHeight: 120, clientHeight: 60 }; observers.length = 0; c = run('x'.repeat(150), el);
    let out = c.draw(); let button = more(out); assert.ok(button, 'a 150 character description clipped on a phone shows More'); assert.equal(button.props['aria-expanded'], false);
    button.props.onClick(); out = c.draw(); button = more(out); assert.equal(text(button), 'Show less'); assert.equal(button.props['aria-expanded'], true);
    el.scrollHeight = 60; observers.at(-1).callback(); out = c.draw(); assert.equal(text(more(out)), 'Show less', 'Show less stays while expanded even if the width now fits');
    button.props.onClick(); out = c.draw(); observers.at(-1).callback(); out = c.draw(); assert.ok(!more(out), 'once collapsed and it fits (after a resize) More disappears');
    el.scrollHeight = 120; observers.at(-1).callback(); out = c.draw(); assert.ok(more(out), 'a narrower width brings More back');
    const last = observers.at(-1); c.hx.cleanup(); assert.equal(last.disconnected, true, 'observer is disconnected on cleanup'); }

  // ---- ChannelHistory ----
  const optionsModule = compile('ChannelOptions.tsx', {});
  assert.equal(optionsModule.channelActionLabel('deep_scan'), 'Scan channel history'); assert.equal(optionsModule.channelActionLabel('remove_channel'),'Remove channel'); assert.equal(optionsModule.channelActionLabel('unsubscribe_youtube'),'Unsubscribe on YouTube','the separate remote account action remains explicit'); assert.equal(optionsModule.channelActionLabel('mystery_action'), 'Channel action');
  { const hx = hooks(), mod = compile('ChannelHistory.tsx', { react: hx.react, './ChannelOptions': optionsModule, './api': { api: async () => ({ items: [{ id: 'channel:' + CH, kind: 'channel-task', title: 'deep_scan', state: 'complete', message: 'Done', created: 1 }, { id: 'channel:x', kind: 'channel-task', title: 'raw_unknown', state: 'complete', message: '', created: 1 }, { id: 'download:1', kind: 'download', title: 'Exact Video Title', state: 'complete', message: '', created: 1 }], total: 3, more: false }) }, './notifications': { NotificationMessage: 'NotificationMessage' }, './components/ui': { Button: 'Button' } });
    hx.begin(); mod.default({ id: CH }); hx.runEffects(); await tick(); hx.begin(); const out = mod.default({ id: CH }), shown = text(out);
    assert.match(shown, /Scan channel history/); assert.doesNotMatch(shown, /deep_scan|raw_unknown/); assert.match(shown, /Channel action/); assert.match(shown, /Exact Video Title/); hx.cleanup(); }
  { const requests = [], hx = hooks(), page = (n, start, more) => ({ items: Array.from({ length: n }, (_, i) => ({ id: 'download:' + (start + i), kind: 'download', title: 'Video ' + (start + i), state: 'complete', message: 'Saved', created: 1700000000 + i })), total: 3, more });
    let queue = [], failNext = false;
    const api = (url, signal) => { requests.push({ url, signal }); if (failNext) { failNext = false; return Promise.reject(new SandboxError('History offline')); } const d = deferred(); queue.push(d); return d.promise; };
    const mod = compile('ChannelHistory.tsx', { react: hx.react, './ChannelOptions': optionsModule, './api': { api }, './notifications': { NotificationMessage: 'NotificationMessage' }, './components/ui': { Button: 'Button' } });
    const draw = id => { hx.begin(); return mod.default({ id }); };
    let out = draw(CH); hx.runEffects();
    assert.match(requests[0].url, new RegExp('/api/guide/channel/' + CH + '/history\\?limit=20&offset=0')); assert.match(text(out), /Recorded downloads, management events and latest channel action\./);
    queue[0].resolve(page(2, 0, true)); await tick(); out = draw(CH);
    assert.equal(tree(out).filter(x => x && x.props?.component === 'li').length, 2); const more = find(out, x => x.type === 'Button' && /Load more/.test(text(x))); assert.ok(more);
    more.props.onClick(); await tick(); assert.match(requests[1].url, /offset=2/);
    queue[1].resolve({ items: [page(2, 0, false).items[1], ...page(1, 2, false).items], total: 3, more: false }); await tick(); out = draw(CH);
    assert.equal(tree(out).filter(x => x && x.props?.component === 'li').length, 3, 'a duplicate event across pages is shown once'); assert.ok(!find(out, x => x.type === 'Button' && /Load more/.test(text(x))));
    // Channel change: the previous request is aborted and its late answer never leaks into the new channel.
    hx.cleanup(); assert.equal(requests[0].signal.aborted, true);
    out = draw(OTHER); hx.runEffects(); assert.equal(tree(out).filter(x => x && x.props?.component === 'li').length, 0);
    // The component state resets because the effect clears items for a new id (the dialog also remounts per channel).
    queue.at(-1).resolve(page(1, 9, false)); await tick(); out = draw(OTHER);
    assert.ok(tree(out).filter(x => x && x.props?.component === 'li').length <= 1);
    // Error + retry.
    const hx2 = hooks(); failNext = true; const mod2 = compile('ChannelHistory.tsx', { react: hx2.react, './ChannelOptions': optionsModule, './api': { api }, './notifications': { NotificationMessage: 'NotificationMessage' }, './components/ui': { Button: 'Button' } });
    const draw2 = () => { hx2.begin(); return mod2.default({ id: CH }); }; draw2(); hx2.runEffects(); await tick(); out = draw2();
    assert.ok(find(out, x => x.type === 'NotificationMessage' && /History offline/.test(x.props.message))); const retry = find(find(out, x => x.type === 'NotificationMessage').props.actions, x => x.type === 'Button' && /Try again/.test(text(x))); assert.ok(retry);
    retry.props.onClick(); await tick(); queue.at(-1).resolve({ items: [], total: 0, more: false }); await tick(); out = draw2();
    assert.match(text(out), /No recorded history for this channel yet\./); assert.ok(!find(out, x => x.type === 'NotificationMessage'));
    hx2.cleanup(); }

  // ---- ChannelForecasts: plain estimates only ----
  { const hx = hooks(), requests = []; let answer;
    const mod = compile('ChannelForecasts.tsx', { react: hx.react, './api': { api: (url, signal) => { requests.push({ url, signal }); return answer(); } }, './notifications': { NotificationMessage: 'NotificationMessage' }, './components/ui': { Button: 'Button' }, './mediaDate': compile('mediaDate.ts', {}) });
    const draw = id => { hx.begin(); return mod.default({ id }); };
    answer = () => Promise.resolve({ items: [{ id: 'f1', channel: CH, date: '2026-10-20', time: '15:00', title: 'Expected upload', state: 'expected', description: 'High confidence based on 5 matching uploads.', confidence: 'High', videoId: 'abcdefghijk' }] });
    draw(CH); hx.runEffects(); await tick(); const out = draw(CH);
    assert.equal(requests[0].url, '/api/guide/channel/' + CH + '/forecasts'); assert.match(text(out), /Estimated uploads/); assert.match(text(out), /Expected\s+·\s+High\s+confidence/);
    assert.ok(!tree(out).some(x => x && typeof x === 'object' && (x.type === 'Button' || x.props?.['aria-expanded']!==undefined&&typeof x.props?.onClick==='function' || x.type === 'a' || x.props?.onClick)), 'no video or download action on an estimate');
    assert.ok(!/Download|Open|Play/.test(text(out)));
    hx.cleanup(); assert.equal(requests[0].signal.aborted, true);
    answer = () => Promise.reject(new SandboxError('Forecast offline')); draw(OTHER); hx.runEffects(); await tick(); const failed = draw(OTHER);
    assert.ok(find(failed, x => x.type === 'NotificationMessage' && /Forecast offline/.test(x.props.message))); hx.cleanup();
    answer = () => Promise.resolve({ items: [] }); draw(CH); hx.runEffects(); await tick(); assert.match(text(draw(CH)), /No estimated uploads/); hx.cleanup(); }

  // ---- Shared sources: labels only distinguish confirmed future uploads; settings schema untouched ----
  const videos = fs.readFileSync(path.join(src, 'ChannelVideos.tsx'), 'utf8');
  assert.match(videos, /item\.state==='scheduled'\?'Scheduled':item\.state==='live'\?'Live now'/);
  const navigation = fs.readFileSync(path.join(src, 'channelNavigation.ts'), 'utf8');
  for (const mode of ['info', 'options', 'overview', 'settings', 'releases', 'history']) assert.ok(navigation.includes("'" + mode + "'"), mode);
  const dialogSource = fs.readFileSync(path.join(src, 'ChannelDetail.tsx'), 'utf8');
  assert.doesNotMatch(dialogSource, /\/api\/subscriptions['"`]\s*,|settings\/youtube/, 'opening the dialog never writes preferences');
  console.log('Channel detail: four tabs, header, overview, settings safety, releases/video swap, forecasts and history passed.');
})().catch(e => { console.error(e); process.exit(1); });
