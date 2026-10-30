"""Keyless TMDB public HTML catalogue. No private endpoints or embedded API keys."""
import json
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from ..discover_sources import fetch
from .providers import initialise
from .ordering import ordered

BASE = 'https://www.themoviedb.org'
def artwork(value):
    match=re.search(r'/t/p/[^/]+(/[A-Za-z0-9_.-]+)$',value or '')
    return 'https://image.tmdb.org/t/p/w500'+match[1] if match else ''

class Cards(HTMLParser):
    def __init__(self):
        super().__init__();self.items=[];self.row=None;self.depth=0;self.date=False
    def handle_starttag(self,tag,attrs):
        d=dict(attrs)
        if tag=='div':
            if self.row is None and 'data-object-id' in d:
                self.row={};self.depth=1
            elif self.row is not None:self.depth+=1
        if self.row is None:return
        if tag=='a':
            match=re.fullmatch(r'/movie/(\d+)(?:-[^/?]+)?',d.get('href',''))
            if match:self.row['id']='tmdb:movie:'+match[1]
        if d.get('data-name'):self.row['title']=d['data-name']
        if tag=='img' and 'poster' in d.get('class',''):
            self.row.update(title=d.get('alt',''),image=artwork(d.get('src','')))
        if 'data-percent' in d:
            try:self.row['rating']=str(float(d['data-percent'])/10)
            except ValueError:pass
        if tag=='span' and 'release_date' in d.get('class',''):self.date=True
    def handle_data(self,data):
        if self.row is not None and self.date:
            for fmt in ('%d %B %Y','%b %d, %Y','%B %d, %Y','%Y-%m-%d'):
                try:
                    day=datetime.strptime(data.strip(),fmt).date().isoformat()
                    self.row.update(releaseDate=day,year=day[:4]);break
                except ValueError:pass
    def handle_endtag(self,tag):
        if tag=='span':self.date=False
        if tag=='div' and self.row is not None:
            self.depth-=1
            if self.depth==0:
                if self.row.get('id') and self.row.get('title'):
                    self.items.append(dict(kind='movie',provider='TMDB',**self.row))
                self.row=None

class Detail(HTMLParser):
    def __init__(self):
        super().__init__();self.item={};self.date=False
    def handle_starttag(self,tag,attrs):
        d=dict(attrs);name=d.get('property','')
        if tag=='meta' and name=='og:image' and self.item.get('image') and 'backdrop' not in self.item:
            self.item['backdrop']=artwork(d.get('content'))
        if tag=='meta':
            key={'og:title':'title','og:description':'description','og:image':'image'}.get(name)
            if key and key not in self.item:self.item[key]=artwork(d.get('content')) if key=='image' else d.get('content','')
        if d.get('data-percent') and 'rating' not in self.item:
            try:self.item['rating']=str(float(d['data-percent'])/10)
            except ValueError:pass
        if tag=='a':
            match=re.search(r'imdb\.com/title/(tt\d+)',d.get('href',''))
            if match:self.item['externalId']=match[1][2:]
    def handle_data(self,data):pass

def cached(store,key,loader):
    initialise(store)
    with store.connect() as db:row=db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',(key,)).fetchone()
    if row and row['updated']>time.time()-21600:return json.loads(row['body'])
    try:result=loader()
    except Exception:
        if row:return dict(json.loads(row['body']),warnings=['TMDB is unavailable; showing its saved catalogue.'])
        raise ValueError('TMDB public catalogue is temporarily unavailable.') from None
    with store.connect() as db:db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',(key,json.dumps(result),time.time()))
    return result

def movies(store,query='',offset=0,sort='source',on_partial=None,upcoming=False):
    if offset%20 or not 0<=offset<10000:raise ValueError('Invalid movie page.')
    page=offset//20+1
    path='/movie/upcoming' if upcoming else '/search/movie' if query else '/movie/top-rated' if sort.startswith('rating') else '/movie'
    params=dict(page=page)
    if query and not upcoming:params['query']=query
    elif not upcoming and sort in ('release','release_asc','year','year_asc','title','title_desc'):
        params['sort_by']={'release':'primary_release_date.desc','release_asc':'primary_release_date.asc','year':'primary_release_date.desc','year_asc':'primary_release_date.asc','title':'title.asc','title_desc':'title.desc'}[sort]
    url=BASE+path+'?'+urlencode(params)
    def load():
        parser=Cards();parser.feed(fetch(url,limit=2_000_000,timeout=8).decode('utf-8'))
        unique=list({r['id']:r for r in parser.items}.values())
        if not unique:raise ValueError('TMDB returned no readable movie cards.')
        following=offset+20 if len(unique)>=20 else None
        if on_partial:on_partial(dict(items=ordered(unique,sort),nextOffset=following))
        # Ratings come from each public title page; initial posters are already visible.
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures=[pool.submit(details,store,item) for item in unique]
            for index,future in enumerate(futures):
                try:unique[index]=future.result()
                except ValueError:pass
        return dict(items=ordered(unique,sort),nextOffset=following)
    return cached(store,'public:'+url,load)

def details(store,item):
    identity=item['id'].split(':')[-1]
    if not identity.isdigit():raise ValueError('Invalid movie identity.')
    def load():
        parser=Detail();parser.feed(fetch(BASE+'/movie/'+identity,limit=1_500_000,timeout=8).decode('utf-8'))
        if not parser.item.get('title'):raise ValueError('TMDB returned no readable title.')
        return dict(item,**parser.item)
    return cached(store,'public:movie:'+identity,load)
