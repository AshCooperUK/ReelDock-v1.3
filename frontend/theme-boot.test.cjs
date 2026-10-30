const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const boot=fs.readFileSync(__dirname+'/public/assets/theme-boot.js','utf8');
const appearance={};let saved=null,blocked=false;
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/app/appearance.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:appearance,localStorage:{getItem:key=>{assert.equal(key,'reeldock-appearance-v13');if(blocked)throw Error('blocked');return saved;},setItem:(key,value)=>{if(blocked)throw Error('blocked');assert.equal(key,'reeldock-appearance-v13');saved=value;}}});
function run(raw,deny=false){const attributes={},style={};vm.runInNewContext(boot,{document:{documentElement:{setAttribute:(k,v)=>attributes[k]=v,style}},localStorage:{getItem:()=>{if(deny)throw Error('blocked');return raw;}}});return{attributes,style};}
assert.equal(appearance.ACCENTS.length,12);assert.equal(new Set(appearance.ACCENTS.map(x=>x.value)).size,12);
for(const scheme of ['light','dark'])for(const accent of appearance.ACCENTS){
 const value={scheme,accent:accent.value};saved=JSON.stringify(value);const output=run(saved);
 assert.equal(output.attributes['data-mantine-color-scheme'],scheme);assert.equal(output.style.colorScheme,scheme);
 assert.equal(JSON.stringify(appearance.readAppearance()),JSON.stringify(value));
 assert.equal(JSON.stringify(appearance.saveAppearance(value)),JSON.stringify(value));
}
for(const background of [true,false]){const value={scheme:'light',accent:'teal',background};saved=JSON.stringify(value);assert.equal(JSON.stringify(appearance.readAppearance()),JSON.stringify(value));assert.equal(JSON.stringify(appearance.saveAppearance(value)),JSON.stringify(value));assert.equal(run(saved).style.colorScheme,'light');}
for(const raw of [null,'bad JSON','{}','{"scheme":"auto","accent":"blue"}','{"scheme":"light","accent":"unknown"}','{"scheme":"dark","accent":"url(https://invalid)"}']){
 assert.equal(run(raw).style.colorScheme,'dark');assert.equal(JSON.stringify(appearance.parseAppearance(raw)),JSON.stringify({scheme:'dark',accent:'blue'}));
}
assert.equal(run(null,true).style.colorScheme,'dark');blocked=true;assert.equal(appearance.readAppearance().scheme,'dark');assert.equal(appearance.saveAppearance({scheme:'light',accent:'violet'}).scheme,'light','storage denial still permits this visit');
const html=fs.readFileSync(__dirname+'/index.html','utf8');assert.ok(html.indexOf('/preview/assets/theme-boot.js')<html.indexOf('type="module"'));assert.ok(!/<script>[^<]/.test(html),'same-origin external script preserves CSP');
console.log('All24 light/dark stock themes restore before paint; invalid or blocked storage defaults safely.');
