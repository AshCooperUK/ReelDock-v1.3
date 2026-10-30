const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const out = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname + '/src/viewMedia/localParent.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, { exports: out, AbortController });

const group = (kind, id = 'group-' + 'a'.repeat(24)) => ({ id, title: 'The Simpsons', kind, count: 2, action: { type: 'local-group', id } });
const CHANNEL = 'UC' + 'a'.repeat(22);
function harness(respond) {
  const calls = { requests: [], discover: [], channel: [], group: [] };
  const opener = out.createParentOpener({
    request: (id, signal) => { calls.requests.push({ id, signal }); return respond(id, signal); },
    discover: card => calls.discover.push(card), channel: target => calls.channel.push(target), group: card => calls.group.push(card),
  });
  return { calls, opener };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const show = { id: 'tvdb:show:71663', kind: 'show', externalId: '71663', title: 'The Simpsons' };

(async () => {
  // A known show opens the standard discover overview: no initial episode.
  let h;
  for (const kind of ['show','shows']) {
  h = harness(async () => ({ target: { type: 'discover', item: show } }));
  h.opener.open(group(kind)); await tick();
  assert.equal(h.calls.requests[0].id, 'group-' + 'a'.repeat(24));
  assert.equal(h.calls.discover.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.discover[0].action)), { type: 'discover', item: show });
  assert.ok(!('initialEpisode' in h.calls.discover[0].action.item));
  assert.equal(h.calls.group.length + h.calls.channel.length, 0);
  }

  // YouTube Library groups always open their saved children, even when an
  // older server could resolve the folder to an added channel's catalogue.
  h = harness(async () => ({ target: { type: 'channel', id: CHANNEL, name: 'Some Channel', avatar: '/a' } }));
  const youtube = group('youtube');
  h.opener.open(youtube); await tick();
  assert.equal(h.calls.requests.length + h.calls.channel.length + h.calls.discover.length, 0);
  assert.deepEqual(h.calls.group, [youtube]);

  // Unknown, malformed, failed: the original group list stays available.
  for (const respond of [async () => ({ target: null }), async () => ({ target: { type: 'channel', id: 'bad', name: 'x' } }),
    async () => ({ target: { type: 'discover', item: { id: 'x', kind: 'movie' } } }), async () => { throw new Error('offline'); }]) {
    h = harness(respond);
    const card = group('shows');
    h.opener.open(card); await tick();
    assert.deepEqual(h.calls.group, [card]);
    assert.equal(h.calls.discover.length + h.calls.channel.length, 0);
  }

  // Movies and ordinary items never request a parent.
  h = harness(async () => assert.fail('no request expected'));
  const movies = group('movies'), single = { id: 'local:1', title: 'F', kind: 'movies', action: { type: 'local', id: '1' } };
  h.opener.open(movies); h.opener.open(single);
  assert.deepEqual(h.calls.group, [movies, single]);
  assert.equal(h.calls.requests.length, 0);

  // Stale and out-of-order responses are ignored; cancel and abort stop late results.
  const pending = [];
  h = harness((id, signal) => new Promise(resolve => pending.push({ id, signal, resolve })));
  h.opener.open(group('shows', 'group-' + '1'.repeat(24)));
  h.opener.open(group('shows', 'group-' + '2'.repeat(24)));
  assert.equal(pending[0].signal.aborted, true);
  pending[1].resolve({ target: { type: 'discover', item: { ...show, id: 'tvdb:show:9', externalId: '9' } } });
  pending[0].resolve({ target: { type: 'discover', item: show } });
  await tick();
  assert.equal(h.calls.channel.length, 0);
  assert.equal(h.calls.discover.length, 1);
  assert.equal(h.calls.discover[0].action.item.id, 'tvdb:show:9');

  // An immediate saved-YouTube group also cancels a held show resolution.
  h = harness((id, signal) => new Promise(resolve => pending.push({ id, signal, resolve })));
  const held = pending.length;
  h.opener.open(group('shows')); h.opener.open(youtube);
  assert.equal(pending[held].signal.aborted, true);
  pending[held].resolve({ target: { type: 'discover', item: show } }); await tick();
  assert.deepEqual(h.calls.group, [youtube]);
  assert.equal(h.calls.discover.length + h.calls.channel.length, 0);
  h = harness((id, signal) => new Promise(resolve => pending.push({ id, signal, resolve })));
  const before = pending.length;
  h.opener.open(group('shows')); h.opener.cancel();
  pending[before].resolve({ target: { type: 'discover', item: show } });
  await tick();
  assert.equal(h.calls.discover.length + h.calls.group.length, 0);

  // Component wiring: click-time resolver, flat search untouched, one overlay, cleanup.
  const view = fs.readFileSync(__dirname + '/src/viewMedia/ViewMedia.tsx', 'utf8');
  assert.match(view, /\/api\/view-media\/local\/'\+encodeURIComponent\(id\)\+'\/parent'/);
  assert.match(view, /openItem=useCallback\(\(item:MediaCard\)=>.*else parentOpener\.current\?\.open\(item\)/);
  assert.match(view, /useEffect\(\(\)=>\(\)=>parentOpener\.current\?\.cancel\(\),\[\]\)/);
  assert.doesNotMatch(view, /channel:target=>/, 'opening a local group never opens the channel catalogue');
  assert.match(view, /channel=\{selected\.kind==='youtube'&&selected\.channelId\?<ChannelIdentity id=\{selected\.channelId\}/, 'the group retains an explicit channel navigation link');
  assert.match(view, /selected\?\.action\.type==='local-group'/, 'unknown groups keep the generic list fallback');
  console.log('Local Library parent routing: saved YouTube children, explicit channel link, show overview, stale-click cancellation and file fallback passed.');
})().catch(error => { console.error(error); process.exit(1); });
