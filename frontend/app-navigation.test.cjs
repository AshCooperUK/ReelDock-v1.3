const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const nav = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname + '/src/appNavigation.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS }
}).outputText, { exports: nav, URL, URLSearchParams });

// Fake browser history: Back/Forward walk the same entries the app pushed.
function browser(start) {
  const entries = [start]; let index = 0;
  const place = () => { const url = new URL(entries[index], 'https://reeldock.test'); return { href: url.href, search: url.search }; };
  return {
    place, entries: () => entries.slice(), index: () => index,
    log: { state: null,
      pushState(_s, _u, url) { entries.splice(index + 1); entries.push(url); index++; },
      replaceState(_s, _u, url) { entries[index] = url; } },
    back() { index = Math.max(0, index - 1); }, forward() { index = Math.min(entries.length - 1, index + 1); },
  };
}

assert.equal(nav.viewFromSearch(''), 'Home');
assert.equal(nav.viewFromSearch('?view=TV+Guide'), 'TV Guide');
assert.equal(nav.viewFromSearch('?view=Local%20Library'), 'Local Library');
assert.equal(nav.viewFromSearch('?view=Settings'), 'Settings');
assert.equal(nav.viewFromSearch('?view=%3Cscript%3E'), 'Home');
assert.equal(nav.viewFromSearch('?view=Downloads'), 'Activity', 'legacy Downloads link canonicalises to Activity');
assert.equal(nav.viewFromSearch('?view=YouTube'), 'Subscriptions', 'legacy YouTube link canonicalises to Subscriptions');
assert.deepEqual([...nav.appViews].filter(view => ['VOD'].includes(view)), [], 'VOD is not a top-level page; its sources live in Discover');
assert.equal(nav.viewFromSearch('?view=Discover'), 'Discover', 'Discover is the canonical browse page');
assert.equal(nav.viewFromSearch('?view=View%20Media'), 'Discover', 'legacy View Media links canonicalise to Discover');
assert.equal(nav.viewFromSearch('?view=VOD'), 'Home', 'stale VOD links land on Home');

const b = browser('/app/');
nav.recordView('TV Guide', b.place(), b.log);
nav.recordView('YouTube', b.place(), b.log); // Legacy name records the canonical route.
nav.recordView('Subscriptions', b.place(), b.log); // Same page again: no extra entry.
assert.deepEqual(b.entries(), ['/app/', '/app/?view=TV+Guide', '/app/?view=Subscriptions']);
b.back(); assert.equal(nav.viewFromSearch(b.place().search), 'TV Guide');
b.back(); assert.equal(nav.viewFromSearch(b.place().search), 'Home');
b.forward(); assert.equal(nav.viewFromSearch(b.place().search), 'TV Guide');
nav.recordView('Home', b.place(), b.log);
assert.deepEqual(b.entries(), ['/app/', '/app/?view=TV+Guide', '/app/']); // A new branch drops the old forward entries.
console.log('Back and Forward: pages become history entries, repeats are not duplicated, unknown pages fall back to Home.');

assert.equal(nav.viewUrl('https://x.test/app/?view=TV+Guide&date=2026-10-05', 'YouTube'), '/app/?view=Subscriptions');
assert.equal(nav.viewUrl('https://x.test/app/?view=TV+Guide&date=2026-10-05', 'Activity'), '/app/?view=Activity');
assert.equal(nav.viewUrl('https://x.test/app/?view=Settings&section=Appearance', 'Home'), '/app/');
assert.equal(nav.guideDate('?view=TV+Guide&date=2026-10-05'), '2026-10-05');
for (const bad of ['?date=2026-02-30', '?date=2026-13-01', '?date=05-10-2026', '?date=2026-10-5', '?date=', '']) assert.equal(nav.guideDate(bad), null, bad);
assert.equal(nav.withGuideDate('https://x.test/app/?view=TV+Guide', '2026-10-12'), '/app/?view=TV+Guide&date=2026-10-12');
assert.equal(nav.withGuideDate('https://x.test/app/?view=TV+Guide&date=2026-10-12', null), '/app/?view=TV+Guide');
const id = 'UC' + 'a'.repeat(21) + '_';
assert.equal(nav.channelParam('?channel=' + id), id);
for (const bad of ['UCshort', 'XX' + 'a'.repeat(22), id + 'a', '../' + id, '']) assert.equal(nav.channelParam('?channel=' + encodeURIComponent(bad)), null, bad);
assert.equal(nav.withChannel('https://x.test/app/?view=YouTube', id), '/app/?view=YouTube&channel=' + id);
assert.equal(nav.withChannel('https://x.test/app/?view=YouTube&channel=' + id, null), '/app/?view=YouTube');
assert.equal(nav.withChannel('https://x.test/app/', 'not-a-channel'), '/app/');
console.log('Deep links: Guide dates and channel ids are validated, added and removed without touching other parameters.');
assert.equal(nav.youtubeCookiesUrl,'/app/?view=Settings&section=YouTube#cookies');
assert.equal(nav.withSettingsSection('https://x.test/app/?view=Settings','YouTube','cookies'),nav.youtubeCookiesUrl);
assert.equal(nav.withSettingsSection('https://x.test'+nav.youtubeCookiesUrl,'General'),'/app/?view=Settings&section=General','manual category changes retire the Cookies anchor');
assert.equal(nav.viewUrl('https://x.test'+nav.youtubeCookiesUrl,'Activity'),'/app/?view=Activity','leaving Settings clears its focus target');
assert.equal(nav.viewUrl('https://x.test/app/?view=Settings#other','Activity'),'/app/?view=Activity#other','unrelated existing anchors retain their behavior');
