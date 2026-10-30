const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/discoverPayload.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:out});
const large={id:'show:12345',kind:'show',title:'NCIS',externalId:'12345',description:'D'.repeat(800),releases:Array.from({length:1000},(_,i)=>({id:'release'+i,title:'NCIS.S24E01.1080p.release'+i,size:4000000000}))};
assert.ok(Buffer.byteLength(JSON.stringify(large))>16384);
const item=out.acquisitionItem(large);
const body={item,episodes:[{id:1,season:24,number:1,name:'Wayward Son'}],quality:'1080',fallback:true,source:'NZB first',requestId:'fixture-request-id'};
assert.ok(Buffer.byteLength(JSON.stringify(body))<16384);
assert.equal(item.externalId,'12345');assert.equal(item.releases,undefined);
assert.equal(out.acquisitionItem({id:'youtube:x',kind:'video',title:'Video',videoId:'abcdefghijk',channelId:'channel',channel:'Name'}).videoId,'abcdefghijk');
console.log('Discover payload: large release lists omitted, single episode fits form limit, YouTube identity retained.');

// Exercise the real popup request effect with an indexer-sized release list.
const effects=[],requests=[];
const popup={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/discovery/DiscoveryTitle.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{
 exports:popup,AbortController,setTimeout:()=>0,clearTimeout:()=>{},Date,
 require:name=>{
  if(name==='react')return {useEffect:fn=>effects.push(fn),useRef:value=>({current:value}),useState:value=>[value,()=>{}]};
  if(name==='react/jsx-runtime')return require(name);
  if(name==='../api')return {api:async(url,signal,body)=>{requests.push({url,body});return {items:[],loading:false};}};
  if(name==='../discoverPayload')return out;
  if(name==='./MovieInformation')return {useMovieInformation:()=>({info:null,busy:false,unavailable:false}),useShowInformation:()=>({info:null,busy:false,unavailable:false}),MovieFacts:()=>null,MovieCast:()=>null,MovieTrailer:()=>null};
  return new Proxy({},{get:()=>()=>null});
 }
});
popup.default({item:{...large,kind:'movie',releaseDate:'2010-07-15'},csrf:'fixture',onClose:()=>{}});
for(const effect of effects)effect();
const titleRequest=requests.find(r=>r.url==='/api/discovery-engine/title');
assert.ok(titleRequest,'popup must issue the title request');
assert.ok(Buffer.byteLength(JSON.stringify(titleRequest.body))<16384,'popup must fit Flask form limit even with thousands of releases');
assert.equal(titleRequest.body.item.releases,undefined);
assert.equal(titleRequest.body.item.releaseDate,'2010-07-15');
console.log('Real movie popup posts compact identity and release date within the server limit.');
