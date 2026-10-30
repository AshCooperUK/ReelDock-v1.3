const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}});
function compile(file,imports){const out={};const code=ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;vm.runInNewContext(code,{exports:out,encodeURIComponent,require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:imports[n]||new Proxy({},{get:(_,key)=>key})});return out;}
function nodes(tree){return !tree||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];}
const changes=[];
const form=compile('settings/torrent/TorrentSettings.tsx',{
 '../../SettingsUI':{Group:'Group',Loading:'Loading',Notice:'Notice',Row:'Row',Toggle:'Toggle'},
 '../useEngineSettings':{useEngineSettings:(engine)=>{assert.equal(engine,'torrent');return {values:{max_active_downloads:3,download_limit_bps:0,upload_limit_bps:0,listen_port:6881,encryption:'prefer',enable_dht:true},save:patch=>changes.push(patch)};}}
}).default({csrf:'token'});
const all=nodes(form);
const speed=all.find(n=>n.props['aria-label']==='Download limit (KB/s)');
speed.props.onBlur({currentTarget:{value:'12',checkValidity:()=>true}});
assert.equal(changes[0].download_limit_bps,12288);
speed.props.onBlur({currentTarget:{value:'-1',checkValidity:()=>false}});
assert.equal(changes.length,1);
all.find(n=>n.props['aria-label']==='Peer encryption').props.onChange({target:{value:'required'}});
assert.equal(changes[1].encryption,'required');
all.find(n=>n.props.title==='DHT').props.onChange(false);
assert.equal(changes[2].enable_dht,false);
assert.ok(!all.some(n=>n.props.title==='Save local metadata & artwork'));
assert.ok(!all.some(n=>/upload|seeding|ratio/i.test(String(n.props.title))));
console.log('Torrent settings: speed units, validation, encryption and discovery saves passed.');
