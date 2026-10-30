const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const states=[],effects=[];let cursor=0,timer,intersection,disconnected=false;
const document={hidden:false};
const react={useRef:()=>({current:{}}),useState(initial){const i=cursor++;if(!(i in states))states[i]=initial;return [states[i],value=>{states[i]=typeof value==='function'?value(states[i]):value;}];},useEffect:fn=>effects.push(fn)};
const window={setInterval(fn,ms){assert.equal(ms,5000);timer=fn;return 1;},clearInterval(){timer=null;}};
class IntersectionObserver{constructor(fn){intersection=fn;}observe(){}disconnect(){disconnected=true;}}
const source=fs.readFileSync(require('node:path').join(__dirname,'src/LiveArtwork.tsx'),'utf8');
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const out={};vm.runInNewContext(code,{exports:out,document,window,IntersectionObserver,require(name){if(name==='react')return react;return {jsx:(type,props)=>typeof type==='function'?type(props):props,jsxs:(type,props)=>typeof type==='function'?type(props):props};}});
function render(src='/artwork/test',placeholder){cursor=0;effects.length=0;return out.default({src,placeholder});}
let img=render();assert.equal(img.src,'/artwork/test');assert.equal(effects[0](),undefined);
img.onError();img=render();const cleanup=effects[0]();assert.equal(img.style.visibility,'hidden');
timer();assert.equal(states[1],0);
intersection([{isIntersecting:true}]);document.hidden=true;timer();assert.equal(states[1],0);
document.hidden=false;timer();img=render();assert.equal(img.src,'/artwork/test?artwork_retry=1');
img.onLoad();cleanup();img=render();assert.equal(img.style.visibility,undefined);assert.equal(timer,null);assert.equal(disconnected,true);assert.equal(effects[0](),undefined);
states.length=0;let avatar=render('/artwork/avatar','temporary icon');assert.equal(avatar.children[0].children,'temporary icon');assert.equal(avatar.children[1].style.visibility,'hidden');
avatar.children[1].onLoad();avatar=render('/artwork/avatar','temporary icon');assert.equal(avatar.children[0],false);assert.equal(avatar.children[1].style.visibility,undefined);
avatar.children[1].onError();avatar=render('/artwork/avatar','temporary icon');assert.equal(avatar.children[0].children,'temporary icon');
console.log('Artwork: pending retries, visibility, successful recovery and cleanup passed.');
