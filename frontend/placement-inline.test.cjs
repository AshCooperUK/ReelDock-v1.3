const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}}),nodes=t=>Array.isArray(t)?t.flatMap(nodes):t&&typeof t==='object'?[t,...nodes(t.props?.children)]:[];
const text=t=>Array.isArray(t)?t.map(text).join(''):typeof t==='string'?t:t&&typeof t==='object'?text(t.props?.children):'';
const flush=()=>new Promise(setImmediate);
(async()=>{for(const [filename,exportName,url] of [['TorrentDownloads.tsx','TorrentPlacement','/api/torrents/'],['NzbDownloads.tsx','NzbPlacement','/api/nzb/']]){
 let jobs=[],calls=[];const out={};
 const imports={react:{useState:v=>[Array.isArray(v)?jobs:v,()=>{}],useEffect:()=>{},useCallback:fn=>fn,lazy:()=> 'Lazy',createContext:()=>({}),useRef:v=>({current:v})},'react/jsx-runtime':{jsx,jsxs:jsx},'@mantine/core':new Proxy({},{get:(_,name)=>String(name)}),'./components/ui':{Button:'Button',Dialog:'Dialog',DialogContent:'DialogContent'},'./api':{api:async(...args)=>{calls.push(args);return{};}}};
 vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+filename,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:n=>imports[n]||{}});
 assert.equal(out[exportName]({csrf:'fixture-token'}),null,'no placement prompt when no choice is pending');
 jobs=[{id:'job/one',placements:[{name:'folder/One.mkv',index:7},{name:'folder/Two.mkv',index:13}]}];
 const tree=nodes(out[exportName]({csrf:'fixture-token'}));
 assert.equal(tree.filter(n=>n.type==='Dialog'||n.type==='DialogContent').length,0,'ambiguous engines never auto-open competing overlays');
 assert.equal(tree.filter(n=>n.props['aria-label']==='Choose where to save').length,1,'one inline panel contains the pending choices');
 const buttons=tree.filter(n=>n.type==='Button');assert.equal(buttons.length,4,'each file retains Movie and TV show actions');
 for(const button of buttons)button.props.onClick();await flush();
 assert.equal(calls.length,4);for(const [i,call] of calls.entries()){assert.equal(call[0],url+'job%2Fone');assert.equal(call[2].action,'placement');assert.equal(call[2].index,i<2?7:13);assert.equal(call[2].category,text(buttons[i]).includes('TV')?'series':'movie');assert.equal(call[3],'fixture-token');}
 }
 const shell=fs.readFileSync(__dirname+'/src/LiveDashboard.tsx','utf8'),status=fs.readFileSync(__dirname+'/src/dashboard/SystemStatus.tsx','utf8');
 assert.equal(fs.existsSync(__dirname+'/src/ActivityWidget.tsx'),false,'duplicate activity implementation is removed');
 assert.doesNotMatch(status,/ActivityWidget|TorrentPlacement|NzbPlacement|Current activity|Expand activity/);
 assert.match(shell,/<ActivityNotifications\b/,'global alerts survive popup removal');assert.match(shell,/view:'Activity'/,'main Activity route stays reachable');
 console.log('Inline placement: both engines retain exact file/category/CSRF choices with no auto-dialogs; duplicate activity popup removed and main activity/alerts retained.');
})();
