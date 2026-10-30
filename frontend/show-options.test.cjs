const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
function moduleFor(path){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+path+'.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:out});return out;}
const {showSections,showSorts,defaultShowSort}=moduleFor('viewMedia/showOptions');
assert.equal(showSections.map(o=>o.label).join('|'),'New TV Shows|Recently Aired TV Shows|Premiering TV Shows|Trending TV Shows|Top Rated TV Shows|Endorsed TV Shows|View All TV Shows');
assert.equal(showSorts.map(o=>o.label).join('|'),'A-Z|Z-A|Latest|Latest Episode|Oldest|Oldest Episode|NZB Order|Year|Rating');assert.equal(defaultShowSort,'release');
// All season choices use the same styled/portaled field as every other menu.
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),mantine=require('@mantine/core');
const fieldOut={},seasonOut={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/components/SelectField.tsx','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:fieldOut,require});
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/discovery/SeasonSelect.tsx','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:seasonOut,require:name=>name==='../components/SelectField'?fieldOut:require(name)});
let selectedSeason;
const season=seasonOut.default({seasons:[2026,3,0],value:'0',onChange:value=>selectedSeason=value});
assert.equal(season.type,fieldOut.SelectField,'the season selector shares the application dropdown adapter');
assert.equal(fieldOut.normalizeSelectData({data:season.props.data}).map(option=>option.label).join('|'),'All seasons|Season 2026|Season 3|Specials');
assert.equal(season.props.comboboxProps,undefined,'the caller cannot reintroduce an in-modal clipped portal');
assert.equal(fieldOut.selectDropdownProps.withinPortal,true);
assert.equal(fieldOut.selectDropdownProps.floatingStrategy,'fixed');
assert.ok(fieldOut.selectDropdownProps.middlewares.flip&&fieldOut.selectDropdownProps.middlewares.shift&&fieldOut.selectDropdownProps.middlewares.size);
for(const value of ['2026','0','']){season.props.onChange({target:{value},currentTarget:{value}});assert.equal(selectedSeason,value);}
const seasonHtml=renderToStaticMarkup(React.createElement(mantine.MantineProvider,{forceColorScheme:'dark'},season));
assert.match(seasonHtml,/role="combobox"/);assert.doesNotMatch(seasonHtml,/<select\b/);
assert.match(seasonHtml,/aria-label="TV season"/);
assert.match(seasonHtml,/value="Specials"/);
const {orderCards}=moduleFor('viewMedia/ordering');
const items=[{id:'old',title:'Old series',releaseDate:'2013-01-01',lastEpisodeReleased:'2026-10-14'},{id:'new',title:'New series',releaseDate:'2026-09-01',lastEpisodeReleased:'2026-10-01'},{id:'unknown',title:'Unknown'}];
assert.equal(orderCards(items,'release').map(i=>i.id).join(','),'new,old,unknown');assert.equal(orderCards(items,'episode_released').map(i=>i.id).join(','),'old,new,unknown');assert.equal(orderCards(items,'episode_released_asc').map(i=>i.id).join(','),'new,old,unknown');
console.log('TV choices, distinct premiere/episode sorts and Mantine season selector contract passed.');
