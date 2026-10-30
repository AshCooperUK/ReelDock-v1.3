const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MantineProvider } = require('@mantine/core');

// The real shared derivation, so the tile is verified against the same
// running/queued/waiting rules used by the Overview activity link.
const activityView = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, 'src/activityView.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS }
}).outputText, { exports: activityView });

const feed = {
  items: [{ id: 'a', state: 'downloading', automatic: 1 }, { id: 'b', state: 'queued', automatic: 1 }],
  activity: { tasks: [{ id: 'sync', state: 'running' }, { id: 'discover-waiting', state: 'queued', waiting: 3 }] },
  error: '', loading: false,
};

const native={torrents:{jobs:[{id:'torrent',state:'downloading',progress:.2}],error:''},nzbs:{jobs:[{id:'nzb',state:'queued',progress:0}],error:''}};
const futureExports = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname + '/src/FutureDownloadTiles.tsx', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true }
}).outputText, {
  exports: futureExports,
  require: name => {
    if (['react', 'react/jsx-runtime', '@mantine/core'].includes(name)) return require(name);
    if (name === 'lucide-react') return new Proxy({}, { get: () => () => null });
    if (name.endsWith('/TorrentDownloads')) return { useTorrentJobs: () => native.torrents };
    if (name.endsWith('/NzbDownloads')) return { useNzbJobs: () => native.nzbs };
    if (name.endsWith('/appNavigation')) return { addDownloadEvent: 'reeldock-add-download' };
    return {};
  }
});
const layout={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/components/layout.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:layout,require});
const exportsObject = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname + '/src/dashboard/HomeShortcuts.tsx', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true }
}).outputText, {
  exports: exportsObject,
  require: name => {
    if (['react', 'react/jsx-runtime', '@mantine/core'].includes(name)) return require(name);
    if (name === 'lucide-react') return new Proxy({}, { get: () => () => null });
    if (name.endsWith('/FutureDownloadTiles')) return { __esModule: true, ...futureExports };
    if (name.endsWith('/downloadFeed')) return { useDownloadFeed: () => feed };
    if (name.endsWith('/TorrentDownloads')) return { useTorrentJobs: () => native.torrents };
    if (name.endsWith('/NzbDownloads')) return { useNzbJobs: () => native.nzbs };
    if (name.endsWith('/activityView')) return activityView;
    if (name.endsWith('/components/layout')) return layout;
    if (name.endsWith('/AtAGlance')) return { glanceTime: value => 'glance:' + value };
    return {};
  }
});
const HomeShortcuts = exportsObject.default;
const ready = {
  csrf: 'fixture',
  overview: { channels: 3, videos: 128, favourites: 2, downloads_paused: false, worker_online: true, downloads: { downloading: 1, cancelling: 0, queued: 2 } },
  glance: { indexed_youtube: 42, updated: 1791620000, next: { title: 'YouTube upload check', at: 1791621000 } },
  discover_initial: { feeds: {}, charts: {} },
};
const html = renderToStaticMarkup(React.createElement(MantineProvider, { forceColorScheme: 'dark' }, React.createElement(HomeShortcuts, { ready, navigate: () => {} })));
for(const name of ['Favourites','Watchlist','Guide','YouTube','Activity','Library'])assert.match(html,new RegExp(name,'i'),name+' destination remains visible');
assert.doesNotMatch(html,/Find media/,'Overview omits the removed header shortcut');assert.doesNotMatch(html,/>View Media</);assert.doesNotMatch(html,/>VOD</,'VOD remains reached through Discover');assert.doesNotMatch(html,/ReelDock Settings/,'Settings remains in navigation');
assert.match(html,/2 running · 2 queued · 3 waiting/,'Overview counts acquisitions and native jobs without the background sync');assert.match(html,/Worker online/);assert.match(html,/3 channels/);assert.match(html,/128 uploads indexed/);assert.match(html,/Waiting for release/);assert.match(html,/Saved YouTube videos/);assert.match(html,/>42</,'saved count uses the glance data');assert.match(html,/YouTube upload check/);assert.match(html,/glance:1791621000 UK/,'next schedule time remains explicit');
for(const name of ['Torrent Download','NZB Download'])assert.match(html,new RegExp(name));assert.equal((html.match(/1 active/g)||[]).length,2,'both engine counts remain exact');
feed.items=[];feed.activity.tasks=[{id:'discover-waiting',state:'queued',waiting:2}];native.torrents.jobs=[];native.nzbs.jobs=[{id:'ali',state:'downloading',progress:.4}];
const renderHome=()=>renderToStaticMarkup(React.createElement(MantineProvider,{forceColorScheme:'dark'},React.createElement(HomeShortcuts,{ready,navigate:()=>{}})));
assert.match(renderHome(),/1 running · 0 queued · 2 waiting/,'the reported Ali G case includes the one active NZB and two waiting requests');
native.nzbs.jobs[0].state='paused';assert.match(renderHome(),/0 running · 0 queued · 2 waiting · 1 paused/,'a paused native transport stays separate from running and queued');
native.nzbs.error='Status unavailable';assert.match(renderHome(),/Activity unavailable/,'failed native reads never publish a false idle summary');native.nzbs.error='';
ready.overview.downloads_paused=true;assert.match(renderHome(),/Downloads paused/,'the global pause and worker badge keep their existing meanings');ready.overview.downloads_paused=false;
feed.items=[];native.torrents.jobs=[];native.nzbs.jobs=[];
feed.activity.tasks=[{id:'guide-weeks',state:'queued'},{id:'youtube-push',state:'queued'}];
assert.match(renderHome(),/0 running · 0 queued/,'the reported two queued background checks do not claim downloads when Activity is empty');
feed.activity.tasks.push({id:'auto-stream',state:'running'},{id:'auto-youtube-unknown',state:'running'},{id:'server-download-vod',state:'queued'},{id:'discover-waiting',state:'queued',waiting:2});
assert.match(renderHome(),/2 running · 1 queued · 2 waiting/,'real watched-folder/YouTube transfers and VOD/release backlogs remain visible alongside background checks');
const destinations = [];
const tree = HomeShortcuts({ ready, navigate: view => destinations.push(view) });
function controls(node){return Array.isArray(node)?node.flatMap(controls):node&&typeof node==='object'?[node,...controls(node.props?.children),...controls(node.props?.actions)]:[];}
for(const control of controls(tree))if(typeof control.props?.onClick==='function')control.props.onClick();
assert.deepEqual([...new Set(destinations)].sort(),['Favourites','Watchlist','TV Guide','Subscriptions','Activity','Local Library','Discover'].sort(),'every prior destination still invokes its exact canonical route');
assert.match(fs.readFileSync(__dirname+'/src/dashboard/HomeShortcuts.tsx','utf8'),/FutureDownloadTiles/);
const source = fs.readFileSync(__dirname + '/src/dashboard/HomeShortcuts.tsx', 'utf8');
assert.doesNotMatch(source, /onOpenSettings/, 'the settings callback is no longer plumbed into Home shortcuts');
assert.match(source, /useDownloadFeed/, 'the Activity tile reads the shared download feed');
assert.doesNotMatch(source, /overview\.downloads\./, 'the Activity tile no longer derives counts from the overview snapshot');
console.log('Home shortcuts: Mantine cards retain all seven routes, live feed counts and both engine download actions; settings remain in navigation.');
