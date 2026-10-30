const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),M=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const postcss=require(require.resolve('postcss',{paths:[path.dirname(require.resolve('vite'))]}));
const root=path.join(__dirname,'src'),css=postcss.parse(fs.readFileSync(path.join(root,'components/MediaSurfaces.module.css'),'utf8'));
const rules=[];css.walkRules(rule=>rules.push(rule));
const ancestors=node=>{const values=[];for(let parent=node.parent;parent;parent=parent.parent)if(parent.type==='atrule')values.push(parent);return values;};
const decl=(rule,name)=>rule.nodes?.find(node=>node.type==='decl'&&node.prop===name);
const ruleFor=(selector,condition)=>rules.find(rule=>rule.selectors.includes(selector)&&condition(rule));
const base=ruleFor('.surface.surface',rule=>rule.parent.type==='root');
const support=ruleFor('.surface.surface',rule=>ancestors(rule).some(parent=>parent.name==='supports'));
const header=ruleFor('.surface.bannerInformation',rule=>ancestors(rule).some(parent=>parent.name==='supports'));
const preview=ruleFor('.surface.preview',rule=>ancestors(rule).some(parent=>parent.name==='supports'));
assert.ok(base&&support&&header&&preview,'all media surfaces share one scoped finish with two bounded blur exceptions');
assert.equal(decl(base,'background').value,'var(--mantine-color-body)','unsupported browsers retain an opaque readable theme surface');
assert.equal(decl(base,'--mantine-color-anchor').value,'var(--mantine-color-text)','text links remain readable across every stock accent');
for(const rule of [support,header,preview]){
 const condition=ancestors(rule).find(parent=>parent.name==='supports').params;
 assert.match(condition,/color-mix/);assert.match(condition,/backdrop-filter/);assert.match(condition,/-webkit-backdrop-filter/);
 assert.match(decl(rule,'background').value,/var\(--mantine-color-body\)/);
}
assert.ok(!decl(base,'backdrop-filter')&&!decl(support,'backdrop-filter'),'large media grids create no blur layers');
assert.match(decl(header,'backdrop-filter').value,/blur\(12px\)/);
assert.match(decl(preview,'backdrop-filter').value,/blur\(8px\)/);
for(const rule of rules){
 assert.ok(rule.selectors.every(selector=>/\.(?:surface|bannerInformation|preview|tile)\b/.test(selector)),'feature styles never reskin settings/activity/global Paper');
 for(const node of rule.nodes||[])if(node.type==='decl'){
  assert.ok(!['opacity','filter','animation','transform','will-change'].includes(node.prop),'foreground content and compositor lifetime must remain unchanged: '+node.prop);
  assert.ok(!node.prop.startsWith('--mantine-primary-')&&!/^--mantine-color-(?:blue|cyan|teal|green|lime|yellow|orange|red|pink|grape|violet|indigo)-/.test(node.prop),'stock accent palettes remain untouched');
 }
}
const underline=rules.find(rule=>rule.selector.includes('.mantine-Anchor-root'));
assert.match(decl(underline,'text-decoration-line').value,/underline/,'neutral links retain an explicit non-color affordance');
const focus=rules.find(rule=>rule.selector.includes(':focus-visible'));
assert.ok(focus&&focus.selector.includes('.surface'));
const focusNeutral=decl(focus,'outline-color')||decl(focus,'outline');
assert.match(focusNeutral.value,/var\(--mantine-color-text\)/);
assert.match(decl(focus,'outline-offset').value,/^[2-9]px$/);
const reduced=ruleFor('.surface.surface',rule=>ancestors(rule).some(parent=>parent.name==='media'&&parent.params.includes('prefers-reduced-transparency')));
assert.ok(reduced);assert.match(ancestors(reduced).find(parent=>parent.name==='media').params,/prefers-contrast:\s*more/);
assert.equal(decl(reduced,'background').value,'var(--mantine-color-body)');assert.equal(decl(reduced,'backdrop-filter').value,'none');
assert.ok(reduced.selector.includes('[data-selected')&&reduced.selector.includes('[data-media-active]'),'selected surfaces honor reduced transparency too');
const forced=ruleFor('.surface.surface',rule=>ancestors(rule).some(parent=>parent.name==='media'&&parent.params.includes('forced-colors')));
for(const [name,value] of Object.entries({background:'Canvas',color:'CanvasText','--mantine-color-dimmed':'CanvasText','--mantine-color-anchor':'LinkText','backdrop-filter':'none','forced-color-adjust':'auto'}))assert.equal(decl(forced,name).value,value,'forced colors retain the system '+name);
assert.ok(forced.selector.includes(':global([data-mantine-color-scheme])'),'forced-color muted text also wins over the dark-scheme rule');

// Mantine Card adds a later background-color, which overrides an equal-specificity
// gradient shorthand. Compare against the real installed Paper/Card selectors,
// including their zero-specificity :where(theme) wrappers, not a fake library.
const nativeCss=postcss.parse(fs.readFileSync(path.join(__dirname,'node_modules/@mantine/core/styles.css'),'utf8'));
const nativeClasses=new Set();
for(const Type of [M.Paper,M.Card]){
 const html=renderToStaticMarkup(React.createElement(M.MantineProvider,null,React.createElement(Type,{'data-native-background':true},'Fixture')));
 const tag=html.match(/<div[^>]*data-native-background[^>]*>/)?.[0];assert.ok(tag);
 for(const name of tag.match(/class="([^"]+)"/)[1].split(' '))nativeClasses.add(name);
}
const specificity=selector=>{
 const simplified=selector.replace(/:where\([^)]*\)/g,'');
 return [(simplified.match(/#[\w-]+/g)||[]).length,
  (simplified.match(/\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+/g)||[]).length];
};
const beats=(left,right)=>left[0]>right[0]||left[0]===right[0]&&left[1]>right[1];
let nativeBackgroundRules=0;
nativeCss.walkRules(rule=>{
 const background=decl(rule,'background-color')||decl(rule,'background');if(!background)return;
 for(const selector of rule.selectors){
  if(![...nativeClasses].some(name=>selector.includes('.'+name)))continue;
  nativeBackgroundRules++;
  assert.ok(!background.important,'a new framework !important background needs an explicit cascade review');
  for(const own of [base,support,header,preview,reduced,forced])
   assert.ok(beats(specificity(own.selectors[0]),specificity(selector)),`${own.selectors[0]} must beat later Mantine ${selector}`);
 }
});
assert.ok(nativeBackgroundRules>=3,'real Paper and both themed Card backgrounds participate in the regression');

// Calculate real CSS compositing, with installed stock Mantine colors. Blur
// cannot improve a completely white/black banner, so these remain conservative.
const rgb=value=>{let hex=value.replace('#','');if(hex.length===3)hex=[...hex].map(part=>part+part).join('');assert.equal(hex.length,6);return hex.match(/[a-f\d]{2}/gi).map(part=>parseInt(part,16)/255);};
const luminance=color=>color.map(c=>c<=.04045?c/12.92:((c+.055)/1.055)**2.4).reduce((sum,c,index)=>sum+c*[.2126,.7152,.0722][index],0);
const contrast=(a,b)=>{const [low,high]=[luminance(a),luminance(b)].sort((a,b)=>a-b);return(high+.05)/(low+.05);};
const token=value=>{const match=value.match(/var\(--mantine-color-([a-z]+)-(\d)\)/);assert.ok(match,value);return M.DEFAULT_THEME.colors[match[1]][Number(match[2])];};
const darkRule=rules.find(rule=>rule.selector.includes("data-mantine-color-scheme='dark'")&&decl(rule,'--mantine-color-dimmed'));
const muted={light:token(decl(base,'--mantine-color-dimmed').value),dark:token(decl(darkRule,'--mantine-color-dimmed').value)};
const accentNames=['blue','cyan','teal','green','lime','yellow','orange','red','pink','grape','violet','indigo'];
for(const accent of accentNames)for(const scheme of ['light','dark'])for(const rule of [support,header,preview]){
 const body=rgb(scheme==='light'?M.DEFAULT_THEME.white:M.DEFAULT_THEME.colors.dark[7]);
 const text=rgb(scheme==='light'?M.DEFAULT_THEME.black:M.DEFAULT_THEME.colors.dark[0]);
 const alphas=[...decl(rule,'background').value.matchAll(/var\(--mantine-color-body\)\s+(\d+(?:\.\d+)?)%/g)].map(match=>Number(match[1])/100);
 assert.ok(alphas.length&&alphas.every(alpha=>alpha>=.86&&alpha<1),'translucency stays visible and within the readable design bounds');
 for(const alpha of alphas)for(const behind of ['#000000','#ffffff','#ff0000','#00ff00','#0000ff','#ffff00','#00ffff','#ff00ff']){
  const background=rgb(behind).map((component,index)=>component*(1-alpha)+body[index]*alpha);
  for(const [label,color]of[['body',text],['dimmed',rgb(muted[scheme])],['neutral link',text]])
   assert.ok(contrast(color,background)>=4.5,`${accent}/${scheme}/${rule.selector} ${label} contrast over ${behind} at ${alpha}`);
 }
}

const classes={surface:'fixture-surface',tile:'fixture-tile',bannerInformation:'fixture-banner',preview:'fixture-preview'};
function load(file,imports={}){
 const output={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(root,file),'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,
  {exports:output,require:name=>name.endsWith('.module.css')?{__esModule:true,default:classes}:imports[name]||require(name)});return output;
}
const {MediaCard}=load('components/browse.tsx',{'../LiveArtwork':{default:()=>null},'./SelectField':{SelectField:()=>null}});
let clicked=0;const reference={current:null},art=React.createElement('img',{src:'/fixture-poster',alt:'Fixture poster'});
const props={art,title:'Readable media',meta:'Source and year',status:'Available',length:'42m',actions:React.createElement(M.ActionIcon,{'aria-label':'Fixture action',disabled:true},'♥'),children:React.createElement('span',{'data-extra-content':true},'Extra content'),active:true,ref:reference,className:'caller-class','aria-selected':true,onClick:()=>clicked++};
for(const layout of ['grid','list']){
 const tree=MediaCard({...props,layout});
 assert.equal(tree.props.component,'li');assert.equal(tree.props['data-media-surface'],'tile');assert.equal(tree.props['data-media-active'],true);
 assert.ok(tree.props.className.includes(classes.surface)&&tree.props.className.includes(classes.tile)&&tree.props.className.includes('caller-class'));
 assert.equal(tree.props.ref,reference);assert.equal(tree.props.style.minWidth,0);assert.equal(tree.props.style.opacity,undefined);
 assert.equal(tree.props.children.props.style.flexDirection,layout==='list'?'row':'column');
 tree.props.onClick();
 if(tree.props.style.outline)assert.ok(focusNeutral.important,'neutral keyboard focus must override an existing inline selected outline');
 for(const scheme of ['light','dark'])for(const accent of accentNames){
  const html=renderToStaticMarkup(React.createElement(M.MantineProvider,{forceColorScheme:scheme,theme:{primaryColor:accent}},React.createElement('ul',null,React.createElement(MediaCard,{...props,layout}))));
  assert.match(html,/data-media-surface="tile"/);assert.match(html,/fixture-surface fixture-tile caller-class/);
  assert.equal((html.match(/<img\b/g)||[]).length,1);assert.match(html,/aria-label="Fixture action"[^>]*disabled=""/);
  assert.match(html,/aria-selected="true"/);assert.match(html,/data-extra-content="true"/);assert.match(html,/Length 42m/);
 }
}
assert.equal(clicked,2,'shared surface styling preserves caller event handlers');

// Verify the non-shared entry points attach the same finish to their actual
// media/preview nodes rather than applying glass to all Paper or form surfaces.
for(const file of ['ChannelViews.tsx','viewMedia/ViewMedia.tsx','HoverCard.tsx','ActivityMedia.tsx','DownloadActivityRow.tsx','FutureDownloadTiles.tsx','Guide.tsx','collections/Favourites.tsx','MediaManagement.tsx']){
 const source=fs.readFileSync(path.join(root,file),'utf8'),tree=ts.createSourceFile(file,source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 const surfaces=[];
 function visit(node){
  if(ts.isJsxOpeningElement(node)||ts.isJsxSelfClosingElement(node)){
   const attributes=node.attributes.properties;
   if(attributes.some(attribute=>ts.isJsxAttribute(attribute)&&attribute.name.getText(tree)==='data-media-surface')){
    const className=attributes.find(attribute=>ts.isJsxAttribute(attribute)&&attribute.name.getText(tree)==='className');
    assert.ok(className&&className.getText(tree).includes('surfaces.surface'),file+' marks its actual framework surface');surfaces.push(node);
   }
  }
  ts.forEachChild(node,visit);
 }
 visit(tree);assert.ok(surfaces.length,file+' has shared tile or preview styling');
}
console.log('Media surfaces: scoped opaque/translucent/high-contrast fallbacks, no grid blur or faded foreground, 12-palette worst-art readability, and actual card/ref/action/layout contracts passed.');
