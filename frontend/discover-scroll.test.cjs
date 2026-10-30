const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const out={},effects=[],observations=[];let revealed=0,requested=0;
const jsx=(type,props)=>({type,props:props||{}});
const react={memo:f=>f,useState:v=>[v,fn=>{revealed=typeof fn==='function'?fn(v):fn;}],useRef:()=>({current:{}}),useEffect:fn=>effects.push(fn)};
class Observer{constructor(callback,options){this.callback=callback;this.options=options;observations.push(this);}observe(){}disconnect(){}}
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/Discover.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:n=>n==='react'?react:n==='react/jsx-runtime'?{jsx,jsxs:jsx}:{},IntersectionObserver:Observer});
function render(count,layout,loadingMore=false){effects.length=0;observations.length=0;out.Collection({group:{title:'Results',items:Array.from({length:count},(_,i)=>({id:String(i),title:String(i),kind:'video'}))},layout,onSelect:()=>{},onLoadMore:()=>requested++,loadingMore});effects.forEach(fn=>fn());}
render(20,'grid');observations[0].callback([{isIntersecting:true}]);assert.equal(requested,1);assert.equal(observations[0].options.root??null,null);
render(48,'list');observations[0].callback([{isIntersecting:true}]);assert.equal(revealed,48);assert.equal(requested,1);
render(20,'shelves');assert.equal(observations[0].options.root??null,null,'every new wrapping media grid observes the viewport');observations[0].callback([{isIntersecting:true}]);assert.equal(requested,2);
render(20,'grid',true);assert.equal(observations.length,0);
console.log('Discover scroll: visible-edge paging, buffered rows, wrapping-grid viewport and loading guard passed.');
