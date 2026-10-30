const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const effects=[],requests=[],out={},dates={};
const compile=path=>ts.transpileModule(fs.readFileSync(__dirname+'/src/'+path,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
vm.runInNewContext(compile('mediaDate.ts'),{exports:dates,Date,Intl});
vm.runInNewContext(compile('discovery/MovieInformation.tsx'),{exports:out,AbortController,URLSearchParams,require:name=>{
 if(name==='react')return {useEffect:fn=>effects.push(fn),useState:value=>[value,()=>{}],useRef:value=>({current:value})};
 if(name==='../components/ui')return {Button:props=>({type:'button',props})};
 if(name==='../api')return {api:async(url,signal)=>{requests.push({url,signal});return {facts:{},cast:[],source:'TMDB'};}};
 if(name==='../mediaDate')return dates;
 return require(name);
}});
function nodes(value){if(value==null)return [];if(Array.isArray(value))return value.flatMap(nodes);return typeof value==='object'?[value,...nodes(value.props?.children)]:[value];}
const info={facts:{Runtime:'98 min',Released:'2026-02-20'},cast:[{id:1,name:'Actor One',role:'Nikki',image:''}],source:'TMDB'};
assert.ok(nodes(out.MovieFacts({info})).includes('20.02.2026'));
const cast=nodes(out.MovieCast({info,busy:false,unavailable:false}));
assert.ok(cast.includes('Actor One'));assert.ok(cast.includes('Nikki'));
assert.equal(cast.filter(node=>node?.type==='a').length,0,'Cast contains no external links');
assert.equal(out.MovieTrailer,undefined,'ReelDock has no trailer or external watch links');
assert.match(nodes(out.MovieCast({info:null,busy:false,unavailable:true})).filter(node=>typeof node==='string').join(' '),/unavailable/);
out.useMovieInformation({id:'movie:1234567',kind:'movie',externalId:'1234567'});
const cleanup=effects.pop()();assert.equal(requests.length,1);assert.match(requests[0].url,/^\/api\/view-media\/movie-info\?/);cleanup();assert.ok(requests[0].signal.aborted);
out.useMovieInformation({id:'show:77',kind:'show'});effects.pop()();assert.equal(requests.length,1,'Only movies fetch optional movie metadata');
out.useShowInformation({id:'show:77',kind:'show',externalId:'77',imdbId:'tt1234567'});effects.pop()();
assert.match(requests[1].url,/^\/api\/view-media\/show-info\?/);assert.match(requests[1].url,/tvdb=77/);assert.match(requests[1].url,/imdb=tt1234567/);
assert.ok(nodes(out.MovieFacts({info:{...info,facts:{Premiered:'2020-02-20'}}})).includes('20.02.2020'));
assert.equal(out.MovieCast({info,busy:false,unavailable:false,titleKind:'show'}).props['aria-label'],'More TV show information');
console.log('Movie popup: cast, UK facts, no trailer links, optional loading and cancellation passed.');
