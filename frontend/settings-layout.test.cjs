const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript'),React=require('react');
const {MantineProvider}=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
let width=1440;
function load(file,imports={},transform=source=>source){const output={};vm.runInNewContext(ts.transpileModule(transform(fs.readFileSync(path.join(__dirname,'src',file),'utf8')),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:output,require:name=>imports[name]||(name.endsWith('/SelectField')?require('./select-field-test-helper.cjs')():require(name))});return output;}
const hooks={useMediaQuery:query=>width<=(Number(/max-width:\s*([\d.]+)em/.exec(query)?.[1]||0)*16)};
const ui=load('components/ui.tsx',{'../ScrollToTop':{default:()=>null},'../notifications-state':{registerNotificationHost:()=>()=>{}},'@mantine/hooks':hooks});
const layout=load('components/layout.tsx'),fields=load('TextField.tsx'),theme=load('mantineTheme.ts').mantineTheme;
const settings=load('SettingsUI.tsx',{'./api':{},'./components/ui':ui,'./components/layout':layout,'./notifications':{notify(){}}});
const Reset=load('ResetReelDock.tsx',{'./api':{},'./TextField':fields,'./SettingsUI':settings,'./components/ui':ui});
// Open the actual shared Accordion for SSR so its lazy body is included in the
// layout assertions without changing the product's collapsed initial state.
const expandedUI={...ui,PanelButton:props=>React.cloneElement(ui.PanelButton(props),{defaultValue:'details'})};
const ChannelImport=load('YouTubeChannelImport.tsx',{'./api':{},'./TextField':fields,'./SettingsUI':settings,'./components/ui':expandedUI});
const page=load('LiveSettings.tsx',{'./ResetReelDock':Reset,'./TextField':fields,'./SettingsUI':settings,'./components/ui':ui,'@mantine/hooks':hooks},source=>source.replace('function SettingsCategories','export function SettingsCategories'));
const render=element=>renderToStaticMarkup(React.createElement(MantineProvider,{theme,forceColorScheme:'dark'},element));
for(width of [320,768,1440]) {
 const html=render(React.createElement(page.default,{open:true,onClose(){},initial:'Account & security',csrf:'fixture'}));
 assert.match(html,/>Settings<\/h1>/);assert.doesNotMatch(html,/role="dialog"/,'Settings is a page rather than a window');
 for(const label of ['Current password','New password','Confirm new password'])assert.ok(html.includes(label));
 assert.ok(html.includes('Change password')&&html.includes('Sign out')&&html.includes('Reset ReelDock'));
 assert.doesNotMatch(html,/class="[^"]*(settings-dialog|setting-row|value-badge|media-dialog)/);
 if(width<992)assert.match(html,/aria-label="Settings category"/);else for(const label of ['General','YouTube','VOD connections','Usenet &amp; NZB','Torrents','Watched folders','Metadata','VPN','Media servers','Appearance','Account &amp; security'])assert.ok(html.includes(label),label);
 const fieldOrder=html.indexOf('Use at least 12 characters.');const password=html.indexOf('autoComplete="new-password"');assert.ok(fieldOrder>password,'settings descriptions follow their inputs');
 const importHtml=render(React.createElement(ChannelImport.default,{csrf:'fixture'}));
 assert.match(importHtml,/mantine-Accordion-root/,'channel import stays in the shared inline disclosure');
 assert.doesNotMatch(importHtml,/role="dialog"/);
 assert.match(importHtml,/<p[^>]*class="[^"]*mantine-Text-root[^>]*>Add channels to ReelDock/,'import guidance uses standard Mantine typography');
 assert.match(importHtml,/<textarea[^>]*name="channels"/);assert.match(importHtml,/<input[^>]*type="file"[^>]*name="channels_csv"/);
 assert.match(importHtml,/<button[^>]*type="submit"[^>]*disabled[^>]*>/,'empty channel import keeps its explicit disabled submit action');
 assert.ok(importHtml.indexOf('Choose a CSV smaller than 500 KB')>importHtml.indexOf('aria-label="YouTube CSV"'),'CSV guidance follows the input');
}
width=320;let selected;
const choice=page.SettingsCategories({section:'General',onSelect:value=>selected=value});
assert.deepEqual(Array.from(choice.props.data,item=>item.value),['General','YouTube','VOD Settings','NZB Settings','Torrent Settings','Auto-Download','Metadata','VPN','Media Players','Appearance','Account & security']);
choice.props.onChange({currentTarget:{value:'NZB Settings'}});assert.equal(selected,'NZB Settings','display names preserve canonical route values');
// Keep one mounted Settings page in sync with deep links/notification routes
// without resetting manual category choices on unrelated parent renders.
let hookCursor=0,hookValues=[],effects=[],changed=false;
const fakeReact={...React,useState(initial){const at=hookCursor++;if(!(at in hookValues))hookValues[at]=typeof initial==='function'?initial():initial;return[hookValues[at],next=>{const value=typeof next==='function'?next(hookValues[at]):next;if(!Object.is(value,hookValues[at])){hookValues[at]=value;changed=true;}}];},useEffect(effect,deps){const at=hookCursor++,before=hookValues[at];if(!before||deps.some((value,index)=>!Object.is(value,before[index])))effects.push(effect);hookValues[at]=deps;}};
const mounted=load('LiveSettings.tsx',{'react':fakeReact,'./ResetReelDock':Reset,'./TextField':fields,'./SettingsUI':settings,'./components/ui':ui,'@mantine/hooks':hooks},source=>source.replace('function SettingsCategories','export function SettingsCategories'));
const seen=[];
function walk(node){return !node||typeof node!=='object'?[]:Array.isArray(node)?node.flatMap(walk):[node,...walk(node.props?.children)];}
function rerender(initial){let result;do{changed=false;hookCursor=0;effects=[];result=mounted.default({open:true,onClose(){},initial,csrf:'fixture',onSection:value=>seen.push(value)});for(const effect of effects)effect();}while(changed);return walk(result).find(node=>node.type===mounted.SettingsCategories);}
let navigation=rerender('General');assert.equal(navigation.props.section,'General');
navigation.props.onSelect('VPN');navigation=rerender('General');assert.equal(navigation.props.section,'VPN','ordinary renders keep the manual category');assert.deepEqual(seen,['VPN']);
navigation=rerender('YouTube');assert.equal(navigation.props.section,'YouTube','Update cookies changes an already-mounted page to YouTube');
navigation=rerender('General');assert.equal(navigation.props.section,'General','browser Back/Forward initial changes are applied');
assert.deepEqual(seen,['VPN'],'external route synchronization does not add redundant history changes');
assert.equal(mounted.canonicalSettingsSection('Downloads'),'General');assert.equal(mounted.canonicalSettingsSection('Xtream'),'VOD Settings');
const names=[];function audit(directory){for(const entry of fs.readdirSync(directory,{withFileTypes:true})){const file=path.join(directory,entry.name);if(entry.isDirectory())audit(file);else if(file.endsWith('.tsx'))names.push(file);}}
audit(path.join(__dirname,'src/settings'));
names.push(path.join(__dirname,'src/YouTubeChannelImport.tsx'));
for(const file of names){const source=fs.readFileSync(file,'utf8');assert.doesNotMatch(source,/import\s+['"][^'"]+\.css['"]|className="(?:settings-|setting-|media-dialog|section-heading|value-badge|status-chip|nzb-size)/,path.basename(file)+' does not depend on the former presentation');assert.doesNotMatch(source,/<Dialog(?:\s|>)/,'settings actions stay in the page');}
console.log('Settings page: responsive category navigation, all account actions, canonical routes, one form grammar and no legacy/dialog layout passed.');
