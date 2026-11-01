const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
let dismissed='';const exportsObject={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/viewMedia/MediaWarnings.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:exportsObject,require:name=>name==='react'?{useState:()=>[dismissed,value=>dismissed=value]}:name==='@mantine/core'?Mantine:name==='react/jsx-runtime'?require(name):(()=>{throw new Error('Source warnings must remain inline: '+name)})()});
const warnings=exportsObject.default,messages=['NZB indexer request limit reached: api.nzb.life. Checks will resume automatically.','YouTube search could not finish. Search again to retry.'];
assert.equal(warnings({}),null);
const notice=warnings({messages});
assert.equal(notice.type,Mantine.Alert,'source warnings stay beside the search results instead of interrupting a download sheet');
assert.equal(notice.props.color,'yellow');assert.equal(notice.props.title,'Some sources need attention');
assert.equal(notice.props.role,'status');assert.equal(notice.props['aria-live'],'polite');
assert.equal(notice.props.children.props.children,messages.join('\n'),'every provider warning remains available without truncation');
assert.equal(notice.props.closeButtonLabel,'Dismiss source warnings');assert.equal(notice.props.styles.closeButton.width,44);assert.equal(notice.props.styles.closeButton.height,44);
for(const scheme of ['light','dark']){
 const html=renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{forceColorScheme:scheme},notice));
 assert.match(html,/role="status"/);assert.match(html,/YouTube search could not finish/);assert.match(html,/aria-label="Dismiss source warnings"/);assert.match(html,/width:44px;height:44px/);
}
notice.props.onClose();
assert.equal(warnings({messages:[...messages]}),null,'identical polling results stay dismissed');
const next=warnings({messages:[...messages,'Another provider could not complete this search.']});
assert.equal(next.type,Mantine.Alert);assert.match(next.props.children.props.children,/Another provider could not complete this search\./,'a new warning is visible');
dismissed='';assert.equal(warnings({messages}).type,Mantine.Alert,'a new search remount restores its own notices');
console.log('Source warnings remain complete, local, accessible and dismissible without global download-screen toasts.');
const filterExports={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/viewMedia/filtering.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:filterExports});
const tv='NZB TV metadata: extra dates or artwork could not be checked for 1 series (Fixture). Known dates are kept; unknown dates sort last. Retrying automatically.';
const legacy='Some TV metadata could not be checked. Known dates are shown; unknown dates sort last.';
for(const known of [tv,legacy]){
 assert.deepEqual(Array.from(filterExports.filterMediaWarnings([known,...messages],'all','all')),[known,...messages]);
 assert.deepEqual(Array.from(filterExports.filterMediaWarnings([known,...messages],'discover','shows')),[known,...messages]);
 for(const [source,kind] of [['all','movies'],['vod','all'],['local','shows'],['subscriptions','youtube']])assert.deepEqual(Array.from(filterExports.filterMediaWarnings([known,...messages],source,kind)),messages,'only the known TV metadata scope can be omitted from unrelated filters');
}
assert.deepEqual(Array.from(filterExports.filterMediaWarnings(['Unknown source warning'],'vod','movies')),['Unknown source warning']);
