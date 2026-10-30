const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
function compile(file,imports={}){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:n=>n==='react/jsx-runtime'?require(n):imports[n]||new Proxy({},{get:()=>()=>null})});return out;}
const {retargetLocalCard:retarget}=compile('viewMedia/localRename.ts');
const before='a'.repeat(32),after='b'.repeat(32),other='c'.repeat(32),second='d'.repeat(32);
const movie={id:'local:'+before,title:'E.T. the Extra-Terrestrial',kind:'movies',action:{type:'local',id:before},image:'/api/view-media/local/'+before+'/art',backdrop:'/api/view-media/local/'+before+'/backdrop',description:'Verified catalogue metadata',year:'1982'};
const unrelated={...movie,id:'local:'+other,action:{type:'local',id:other},image:'https://example.test/poster.jpg'};
const changes=[{from:before,to:after}];
let updated=retarget(movie,changes);assert.equal(updated.action.id,after);assert.equal(updated.id,'local:'+after);assert.equal(updated.image,'/api/view-media/local/'+after+'/art');assert.equal(updated.backdrop,'/api/view-media/local/'+after+'/backdrop');assert.equal(updated.title,movie.title);assert.equal(updated.year,'1982');assert.equal(updated.description,movie.description);assert.equal(movie.action.id,before,'input remains untouched');
assert.equal(retarget(unrelated,changes).action.id,other);assert.equal(retarget(unrelated,changes).image,unrelated.image);
const group={id:'group-one',title:'A show',kind:'shows',action:{type:'local-group',id:'group-one'},image:movie.image,children:[movie,unrelated]};updated=retarget(group,changes);assert.equal(updated.id,group.id);assert.equal(updated.action,group.action);assert.equal(updated.children[0].action.id,after);assert.equal(updated.children[1].action,unrelated.action);assert.equal(updated.image,'/api/view-media/local/'+after+'/art','group artwork follows renamed child');
assert.equal(retarget(movie,[{from:before,to:'bad'}]),movie);assert.equal(retarget(null,changes),null);
const youtube={...unrelated,action:{type:'youtube',id:before},backdrop:'https://example.test/banner.jpg'};assert.equal(retarget(youtube,changes),youtube,'provider IDs never follow path hashes');
const part={...movie,id:'local:'+other,action:{type:'local',id:other}};const multipart={...group,children:[movie,part]};updated=retarget(multipart,[...changes,{from:other,to:second}]);assert.deepEqual(Array.from(updated.children,x=>x.action.id),[after,second],'every split part uses its own server mapping');
// No player component exists: a renamed card only retargets its identity, art and managed lookups.
assert.ok(!fs.existsSync(__dirname+'/src/VideoPlayer.tsx')&&!fs.existsSync(__dirname+'/src/StreamVideo.tsx'),'playback components are removed');
console.log('Local rename: exact immutable mapping, nested/group artwork, split parts, strict IDs/provider separation passed.');
