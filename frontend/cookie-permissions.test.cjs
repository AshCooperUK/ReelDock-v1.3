const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}});
function compile(file,imports){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:imports[n]||new Proxy({},{get:(_,key)=>key})});return out;}
function nodes(tree){return !tree||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];}
const saves=[];
let hook;
const mod=compile('settings/youtube/CookiePermissions.tsx',{'../../SettingsUI':{Loading:'Loading',Notice:'Notice',Toggle:'Toggle'},'../useEngineSettings':{useEngineSettings:(engine,csrf)=>{assert.equal(engine,'youtube');assert.equal(csrf,'fixture');return hook;}}});
const render=(props={})=>nodes(mod.default({csrf:'fixture',...props}));
const toggle=(all,title)=>all.find(n=>n.type==='Toggle'&&n.props.title===title);
const base={busy:false,error:'',notice:'',loadError:'',reload(){},save:async values=>{saves.push(values);return true;}};

// Nothing interactive renders before the server values load.
hook={...base,values:undefined};
let all=render();
assert.ok(all.some(n=>n.type==='Loading'));assert.equal(all.filter(n=>n.type==='Toggle').length,0);

// Defaults: import off, downloads on, labelled exactly.
hook={...base,values:{cookie_import_channels:false,cookie_downloads:true}};
all=render({importStatus:'Cookie subscription import checked 3 channels; 1 added.'});
const importToggle=toggle(all,'Allow import of channels'),downloadToggle=toggle(all,'Allow downloads using cookies');
assert.equal(importToggle.props.value,false);assert.equal(downloadToggle.props.value,true);
assert.equal(importToggle.props.disabled,false);
assert.match(importToggle.props.detail,/Turning this off keeps existing channels/);
assert.match(downloadToggle.props.detail,/authenticated-retry and channel settings still apply/);
assert.ok(!all.some(n=>n.type==='Notice'&&n.props.text&&n.props.text.includes('Cookie subscription import')),'import status hidden while import is off');

// Each switch saves only its own field.
importToggle.props.onChange(true);downloadToggle.props.onChange(false);
assert.deepEqual(JSON.parse(JSON.stringify(saves)),[{cookie_import_channels:true},{cookie_downloads:false}]);

// Import status shows only when import is on.
hook={...base,values:{cookie_import_channels:true,cookie_downloads:true}};
all=render({importStatus:'Status text'});
assert.ok(all.some(n=>n.type==='Notice'&&n.props.text==='Status text'));

// Busy disables both; errors and feedback surface.
hook={...base,busy:true,error:'Choose on or off.',notice:'Saved.',values:{cookie_import_channels:false,cookie_downloads:true}};
all=render();
assert.ok(all.filter(n=>n.type==='Toggle').every(n=>n.props.disabled===true));
assert.ok(all.some(n=>n.type==='Notice'&&n.props.error&&n.props.text==='Choose on or off.'));
assert.ok(all.some(n=>n.type==='Notice'&&!n.props.error&&n.props.text==='Saved.'));

// Placement: first control inside the cookies group; old independent status notice removed.
const page=fs.readFileSync(__dirname+'/src/settings/youtube/YouTubeSettings.tsx','utf8');
assert.match(page,/<Group title="YouTube cookies"><CookiePermissions csrf=\{csrf\} importStatus=\{cookies\.data\?\.import_status\|\|''\}\/><Row title="Cookie file">/);
assert.ok(!/cookies\.data\?\.import_status&&<Notice/.test(page));
console.log('Cookie permissions: defaults, independent saves, disabled/loading states, feedback and placement passed.');
