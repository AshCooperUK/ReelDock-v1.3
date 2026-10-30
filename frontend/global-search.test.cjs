const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), ts = require('typescript');
const nav = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname + '/src/appNavigation.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, { exports: nav, URL, URLSearchParams, location: { search: '' } });
const base = 'http://127.0.0.1:8821/app/';
// Deep link populates the textbox value source.
assert.equal(nav.mediaSearchQuery('?view=Discover&q=dune'), 'dune');
assert.equal(nav.mediaSearchQuery('?view=Discover'), null);
// Normal search keeps unrelated params and routes to Discover.
assert.equal(nav.mediaSearchUrl(base + '?week=2026-W42&view=Guide#top', ' new text '), '/app/?week=2026-W42&view=Discover&q=new+text#top');
// Empty search with no q is a no-op; with q it removes only q and stays on Discover.
assert.equal(nav.mediaSearchUrl(base + '?view=Discover', '  '), null);
assert.equal(nav.mediaSearchUrl(base, ''), null);
assert.equal(nav.mediaSearchUrl(base + '?view=Discover&q=dune&category=x', ''), '/app/?view=Discover&category=x');
assert.equal(nav.mediaSearchUrl(base + '?view=Favourites&q=dune', '   '), '/app/?view=Discover');

// Extract the real handlers with TypeScript's AST: formatting changes must not
// replace navigation, history or textbox synchronization assertions.
const source=fs.readFileSync(__dirname+'/src/LiveDashboard.tsx','utf8'),tree=ts.createSourceFile('LiveDashboard.tsx',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
let submitNode,syncNode,searchInitializer;const synchronizedEvents=[];
function visit(node){if(ts.isFunctionDeclaration(node)&&node.name?.text==='submitSearch')submitNode=node;
 if(ts.isVariableDeclaration(node)&&node.name.getText(tree)==='sync'&&node.initializer&&ts.isArrowFunction(node.initializer)&&node.initializer.body.getText(tree).includes('setSearchText'))syncNode=node.initializer;
 if(ts.isCallExpression(node)&&node.expression.getText(tree)==='useState'&&node.arguments[0]?.getText(tree).includes('mediaSearchQuery(location.search)'))searchInitializer=node.arguments[0];
 if(ts.isCallExpression(node)&&node.expression.getText(tree)==='window.addEventListener'&&node.arguments[1]?.getText(tree)==='sync')synchronizedEvents.push(node.arguments[0].getText(tree));ts.forEachChild(node,visit);}
visit(tree);assert.ok(submitNode&&syncNode&&searchInitializer,'the textbox initializer and real handlers remain present');
assert.ok(synchronizedEvents.includes("'popstate'")&&synchronizedEvents.includes("'reeldock-media-search'"),'Back/Forward and global search events synchronize the one global textbox');
const submit=submitNode.body.statements.map(node=>node.getText(tree)).join('\n'),sync=syncNode.body.statements.map(node=>node.getText(tree)).join('\n');
function browser(href){const history=[href],state={index:0,text:'',view:nav.viewFromSearch(new URL(href).search),navOpen:true,searchOpen:true,events:[],urlSearch:''};
 const location={get href(){return history[state.index];},get search(){return new URL(this.href).search;},get pathname(){return new URL(this.href).pathname;},get hash(){return new URL(this.href).hash;}};
 const env={location,mediaSearchUrl:nav.mediaSearchUrl,mediaSearchQuery:nav.mediaSearchQuery,viewFromSearch:nav.viewFromSearch,
 history:{state:{fixture:true},pushState(_a,_b,url){history.splice(state.index+1);history.push(new URL(url,base).href);state.index++;},replaceState(_a,_b,url){history[state.index]=new URL(url,base).href;}},
 window:{dispatchEvent(event){state.events.push(event.type);vm.runInNewContext('(function(){'+sync+'})()',env);},scrollTo(){}},Event:class{constructor(type){this.type=type;}},
 setNavOpen:value=>state.navOpen=value,setSearchOpen:value=>state.searchOpen=value,setView:value=>state.view=value,setSearchText:value=>state.text=value,setUrlSearch:value=>state.urlSearch=value,event:{preventDefault(){}}};
 return{history,state,env,type(value){state.text=value;},sync(){vm.runInNewContext('(function(){'+sync+'})()',env);},submit(){env.searchText=state.text;env.view=state.view;vm.runInNewContext('(function(){'+submit+'})()',env);},back(){state.index--;this.sync();},forward(){state.index++;this.sync();}};}
const b=browser(base+'?view=Discover&q=dune');b.sync();assert.equal(b.state.text,'dune');b.type('arrival');b.submit();
assert.equal(b.state.navOpen,false,'global search closes navigation');assert.equal(b.state.searchOpen,false,'the phone search drawer closes so results are visible');assert.equal(new URL(b.history[b.state.index]).searchParams.get('q'),'arrival');assert.equal(b.state.view,'Discover');assert.equal(b.state.text,'arrival');
b.back();assert.equal(b.state.text,'dune');b.forward();assert.equal(b.state.text,'arrival');b.type('');b.submit();assert.equal(new URL(b.history[b.state.index]).searchParams.has('q'),false);assert.equal(new URL(b.history[b.state.index]).searchParams.get('view'),'Discover');assert.equal(b.state.text,'');assert.equal(b.history.length,3);
const count=b.history.length,events=b.state.events.length;b.type('');b.submit();assert.equal(b.history.length,count,'empty submit with nothing to clear adds no history');assert.equal(b.state.events.length,events);b.back();assert.equal(b.state.text,'arrival');
// From every destination, the same field searches all media; legacy page-filter
// deep links may remain but never change the header's behavior or text.
assert.doesNotMatch(source,/\bsearchScope\b|\bsetSearchScope\b|aria-label="Search scope"|Current page/,'the global bar has no scope/options interaction');
for(const view of ['Subscriptions','TV Guide','Favourites','Watchlist','Local Library','Activity','Settings','Home']){
 const current=browser(base+'?view='+encodeURIComponent(view)+'&filter=legacy-filter&date=2026-09-28');current.sync();assert.equal(current.state.text,'','page filters do not populate the global field');current.type('shared query');current.submit();const target=new URL(current.history.at(-1));assert.equal(current.state.view,'Discover',view+' uses global Discover search');assert.equal(target.searchParams.get('q'),'shared query');assert.equal(target.searchParams.has('filter'),false,'legacy page filters do not leak into federated search');assert.equal(current.state.searchOpen,false);assert.equal(current.state.navOpen,false);assert.equal(current.history.length,2,'global submission records a navigable history entry');}
console.log('One global search: no scope options, every page submits to Discover, deep links, submit/clear, Back/Forward and exact URL/query contracts passed.');
