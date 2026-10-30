const assert = require('node:assert/strict');
const fs = require('node:fs');
const viewMedia = fs.readFileSync(__dirname + '/src/viewMedia/ViewMedia.tsx', 'utf8');
const management = fs.readFileSync(__dirname + '/src/MediaManagement.tsx', 'utf8');
const title = fs.readFileSync(__dirname + '/src/discovery/DiscoveryTitle.tsx', 'utf8');
const videoSheet = fs.readFileSync(__dirname + '/src/VideoSheet.tsx', 'utf8');

// Every View Media section that can show a downloaded file reaches the shared
// management panel; local files enrol through their scan identity first.
// Management lives inside the one DetailSheet as a Manage tab — never a
// stacked PanelButton dialog.
assert.match(viewMedia, /localId=\{item\.action\.id\}/, 'local files pass their scan identity to enrol-local');
assert.match(viewMedia, /id:item\.action\.id/, 'subscription videos pass their id to the shared video sheet');
assert.match(videoSheet, /manageKey=\{item\.id\}/, 'the video sheet scopes management to the video key');
assert.match(videoSheet, /id:'video:'\+video\.id/, 'the video sheet prefixes the managed video key');
assert.doesNotMatch(viewMedia, /PanelButton/, 'no stacked manage dialog is opened from View Media');
assert.match(management, /action:'enrol-local'/, 'the panel enrols local files before listing them');
assert.match(title, /manageKey=\{details\?\.key\}/, 'episode popups scope management to the opened episode');
assert.match(title, /<MediaManagement csrf=\{csrf\} item=\{title\}/, 'movie and show popups expose title-level management');
console.log('View Media management wiring: local enrolment, video keys and shared popup panels passed.');

// Local Library folders collapse into parent cards; a child swaps the group
// overlay for its own item sheet — one overlay max, closing it re-opens the
// group underneath.
const types = fs.readFileSync(__dirname + '/src/viewMedia/types.ts', 'utf8');
const tileActions = fs.readFileSync(__dirname + '/src/collections/TileActions.tsx', 'utf8');
assert.match(types, /'local-group'/, 'group cards carry their own action type');
assert.match(tileActions, /type==='local-group'\)return \{\}/, 'group cards never resolve favourite/watchlist targets');
assert.match(viewMedia, /selected\?\.action\.type==='local-group'/, 'the dialog renders group contents');
assert.match(viewMedia, /selected\.children\|\|\[\]/, 'the group dialog lists its children files');
assert.match(viewMedia, /setChildSelected\(child\)/, 'a child item swaps into its own sheet');
assert.match(viewMedia, /open=\{!childSelected\}/, 'the group overlay hides while the child sheet is open — never stacked');
assert.doesNotMatch(viewMedia, /activeGroup/, 'group parent state is not retained for back navigation');
assert.match(viewMedia, /actions=\{!grouped&&<TileActions/, 'group cards render no tile actions');
assert.match(viewMedia, /!!item\.count&&<Badge[^>]*>\{countLabel\}/, 'group cards badge their child count');
console.log('Local Library grouping: parent cards, child drill-down and no group tile actions passed.');

// Shows resolve their exact identity on click. Saved YouTube groups stay in
// the local file list; their channel link remains an explicit separate action.
assert.match(viewMedia, /createParentOpener/, 'parent cards are resolved through the click-time opener');
assert.match(viewMedia, /group:card=>setSelected\(card\)/, 'unresolved parents fall back to the original group list');
console.log('Local Library parents: exact-identity routing with grouped fallback passed.');
