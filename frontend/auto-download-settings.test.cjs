const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}});
function compile(file,imports){const out={};const code=ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;vm.runInNewContext(code,{exports:out,encodeURIComponent,require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:imports[n]||new Proxy({},{get:(_,key)=>key})});return out;}
function nodes(tree){return !tree||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];}
const changes=[];
const form=compile('settings/auto-download/AutoDownloadSettings.tsx',{
 '../../SettingsUI':{Group:'Group',Loading:'Loading',Notice:'Notice',Row:'Row',Toggle:'Toggle'},
 '../useEngineSettings':{useEngineSettings:engine=>{assert.equal(engine,'auto-download');return{values:{enabled:false,torrent:true,nzb:true,strm:true},save:patch=>changes.push(patch)};}}
}).default({csrf:'token'});
const all=nodes(form);
for(const title of ['Watch for new files','Torrent files','NZB files','Stream-link files']) all.find(n=>n.props.title===title).props.onChange(false);
assert.deepEqual(changes.map(x=>Object.keys(x)[0]),['enabled','torrent','nzb','strm']);
assert.ok(!all.some(n=>n.type==='input'||n.type==='select'));
const shell=fs.readFileSync(__dirname+'/src/LiveSettings.tsx','utf8');
assert.match(shell,/section\s*===\s*'Auto-Download'\s*&&\s*<AutoDownloadSettings/);
assert.match(shell,/const AutoDownloadSettings\s*=\s*lazy\(\(\) => import\('\.\/settings\/auto-download\/AutoDownloadSettings'\)\)/,'the complete settings area stays available as a lazy chunk');
assert.ok(!shell.includes('<Toggle title="Auto Downloads"'));
console.log('Auto-Download settings: scoped saves, fixed routing and no duplicate master switch passed.');
