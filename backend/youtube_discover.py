"""Reusable public YouTube discovery and local, explainable channel recommendations.

No account data is sent to a recommendation service. Only generic topic queries
are sent to public YouTube search. Subscription profiles stay in this process.
"""
import json
import re
import threading
import time
from collections import Counter
from . import discover_sources as sources

PAGE = 24
FEEDS = {'related':'Videos for you','popular':'Daily music · Kworb', 'all-time':'All-time videos · YouTubers.me',
         'channels':'Popular channels · Communitrics', 'rising':'Channel momentum · Communitrics',
         'topics':'Topic videos · YouTube search', 'recommended':'Channels for you'}
TOPICS = {
    'music':('Music', 'music song singer concert guitar piano'),
    'gaming':('Gaming', 'gaming gamer gameplay minecraft playstation xbox nintendo'),
    'technology':('Technology', 'technology tech computer linux software coding programming hardware'),
    'science':('Science', 'science physics astronomy space biology chemistry'),
    'education':('Education', 'education learning tutorial history mathematics language'),
    'travel':('Travel', 'travel camping hiking adventure tourism'),
    'food':('Food & cooking', 'food cooking recipe chef baking kitchen'),
    'sports':('Sports', 'sport sports football soccer basketball tennis racing'),
    'fitness':('Fitness', 'fitness workout yoga exercise training'),
    'film':('Film & television', 'movie film cinema television trailer filmmaking'),
    'comedy':('Comedy', 'comedy comedian funny humour humor'),
    'crafts':('DIY & crafts', 'woodworking crafts diy restoration maker sewing'),
    'nature':('Nature & animals', 'nature wildlife animals gardening birds'),
    'news':('News', 'news journalism politics current affairs'),
}
_CACHE={}
_LOCK=threading.Lock()
_SEARCH=threading.BoundedSemaphore(2)


def options():
    return dict(feeds=[{'id':k,'name':v} for k,v in FEEDS.items()],
                categories=[{'id':'all','name':'All topics'}]+[{'id':k,'name':v[0]} for k,v in TOPICS.items()])


def topics(text):
    words=set(re.findall(r'[a-z]+',str(text).lower()))
    return {key for key,(_,terms) in TOPICS.items() if words.intersection(terms.split())}


def _search(query, offset=0, dates=False):
    key=(query,offset,dates)
    with _LOCK:
        cached=_CACHE.get(key)
        if cached and time.time()-cached[0]<1800:return cached[1]
    if not _SEARCH.acquire(blocking=False):raise ValueError('YouTube discovery is busy. Try again shortly.')
    try:
        result=sources.search_youtube(query,offset,dates=True) if dates else sources.search_youtube(query,offset)
        with _LOCK:
            if len(_CACHE)>=128:_CACHE.pop(next(iter(_CACHE)))
            _CACHE[key]=(time.time(),result)
        return result
    finally:_SEARCH.release()


def _profile(store):
    with store.connect() as db:
        channels=db.execute('SELECT id,data FROM channels WHERE active=1 ORDER BY id').fetchall()
        videos=db.execute('SELECT v.data FROM videos v JOIN channels c ON c.id=v.channel WHERE c.active=1 ORDER BY v.published DESC LIMIT 500').fetchall()
    counts=Counter()
    for row in channels:
        data=json.loads(row['data']);counts.update(topics(str(data.get('name',''))+' '+str(data.get('description',''))))
    for row in videos:
        data=json.loads(row['data']);counts.update(topics(str(data.get('title',''))+' '+str(data.get('description',''))[:500]))
    return {row['id'] for row in channels},counts


def recommendations(store,category='all'):
    subscribed,profile=_profile(store)
    selected=[category] if category!='all' else [key for key,_ in profile.most_common(2)]
    chart=sources.load_chart(store,'channels')
    candidates={item['channelId']:dict(item) for item in chart.get('items',[]) if item.get('channelId') not in subscribed}
    warnings=[]
    if chart.get('stale'):warnings.append('Popular-channel discovery is using an older snapshot because its refresh failed.')
    for topic in selected:
        try:
            for video in _search(TOPICS[topic][0]+' channels').get('items',[]):
                identity=video.get('channelId')
                if not identity or identity in subscribed:continue
                candidate=candidates.setdefault(identity,dict(id='youtube:channel:'+identity,channelId=identity,
                    title=video.get('channel') or identity,kind='channel',image='',provider='YouTube',public=True,
                    url='https://www.youtube.com/channel/'+identity,sourceUrl='https://www.youtube.com/'))
                candidate.setdefault('matchedTopics',[])
                candidate['matchedTopics']=sorted(set(candidate['matchedTopics'])|{topic})
        except ValueError:warnings.append('Topic search is temporarily unavailable; available channel suggestions are retained.')
    scored=[];variety=[]
    for item in candidates.values():
        matched=topics(item.get('title','')+' '+item.get('description',''))|set(item.get('matchedTopics',[]))
        if category!='all' and category not in matched:continue
        score=sum(profile[t] for t in matched)
        if category!='all':score+=int(category in matched)
        if score:
            names=[TOPICS[t][0] for t in sorted(matched) if profile[t] or category==t]
            item['description']='Suggested for '+', '.join(names)+'. Topic similarity inferred from subscriptions and public search; not YouTube personal recommendations.'
            scored.append((score,item))
        else:
            item['description']='For variety: a popular channel from Communitrics. No subscription similarity established.'
            variety.append(item)
    scored.sort(key=lambda pair:(-pair[0],pair[1]['id']))
    similar=[item for _,item in scored];mixed=[]
    if similar:variety=variety[:(len(similar)+2)//3]
    elif profile and category=='all':
        variety=variety[:6]
        warnings.append('No close topic matches are available yet. These suggestions are for variety.')
    # Three related suggestions followed by one broader suggestion when available.
    while similar or variety:
        mixed.extend(similar[:3]);similar=similar[3:]
        if variety:mixed.append(variety.pop(0))
    if not profile and category=='all':warnings.append('Not enough topic information in your subscriptions yet. Showing popular channels for variety; choose a category to explore.')
    return mixed,warnings


def related_videos(store,category,offset):
    subscribed,profile=_profile(store)
    selected=[category] if category!='all' else [key for key,_ in profile.most_common(3)]
    if not selected:selected=['technology','science','crafts']
    page=offset//PAGE*20
    related=[];seen=set();more=False;warnings=[]
    for topic in selected:
        try:
            result=_search(TOPICS[topic][0],page)
            more=more or result.get('nextOffset') is not None
            for item in result.get('items',[]):
                if item.get('kind')!='video' or item['id'] in seen:continue
                seen.add(item['id']);related.append(dict(item))
        except ValueError:warnings.append('Some video suggestions could not be refreshed.')
    # Subscription-related topics lead; a broader topic supplies a small amount of variety.
    if category=='all' and selected:
        other=next((key for key in ('science','crafts','travel','technology') if key not in selected),'education')
        try:
            variety=_search(TOPICS[other][0],page).get('items',[])
            additions=[v for v in variety if v.get('kind')=='video' and v['id'] not in seen][:6]
            for i,v in enumerate(additions):related.insert(min(3+i*4,len(related)),dict(v))
        except ValueError:pass
    return dict(items=related[:PAGE],nextOffset=offset+PAGE if more else None,label='Videos for you',warnings=warnings,
                freshness={'source':'ReelDock','scope':'Based on subscription topics, with some variety.'})

def browse(store,feed='recommended',category='all',query='',offset=0,sort='source',search_dates=False):
    from .view_media.ordering import ordered
    if feed=='related' and not query:
        if category not in ('all',*TOPICS) or type(offset) is not int or offset<0 or offset%PAGE:raise ValueError('Invalid video discovery page.')
        return dict(related_videos(store,category,offset),**options(),feed=feed,category=category)
    if feed not in FEEDS or category not in ('all',*TOPICS):raise ValueError('Choose a valid YouTube discovery feed and category.')
    if category!='all' and feed!='recommended':feed='topics'
    search_mode=bool(query) or feed=='topics'
    step=20 if search_mode else PAGE
    if type(offset) is not int or not 0<=offset<=250000 or offset%step:raise ValueError('Invalid search page.')
    info=options();info.update(feed=feed,category=category)
    if search_mode:
        if not query and category=='all':return dict(items=[],nextOffset=None,warnings=['Choose a topic to explore public YouTube search.'],**info)
        term=' '.join(part for part in (query,TOPICS[category][0] if category!='all' else '') if part)[:160]
        result=_search(term,offset,dates=True) if search_dates else _search(term,offset)
        return dict(result,**info,label='Public YouTube topic search',warnings=[],freshness={'source':'YouTube search','scope':'Search relevance, not a trending chart. Cached for up to 30 minutes.'})
    if feed=='recommended':
        rows,warnings=recommendations(store,category)
        freshness={'source':'ReelDock recommendations','scope':'Mostly similar topics, with some popular channels for variety. Already-followed channels are excluded.'}
    else:
        chart=sources.load_chart(store,feed);rows=chart.get('items',[])
        stale=bool(chart.get('stale') or chart.get('seed') or chart.get('error'))
        freshness={key:chart.get(key) for key in ('source','sourceUrl','updatedAt','fetchedAt','scope')}
        freshness['stale']=stale
        warnings=['Showing an older saved chart. This source could not be refreshed; ReelDock will retry automatically.'] if stale else []
    rows=ordered(rows,sort)
    return dict(items=rows[offset:offset+PAGE],nextOffset=offset+PAGE if offset+PAGE<len(rows) else None,
                label=FEEDS[feed],warnings=warnings,freshness=freshness,**info)


def public_channel(store,channel):
    """Read discovery identity without adding it to subscriptions or the Guide."""
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',channel):raise ValueError('Invalid channel ID.')
    candidates=[]
    for feed in ('channels','rising'):
        candidates.extend(sources.load_chart(store,feed).get('items',[]))
    with _LOCK:
        for _,result in _CACHE.values():candidates.extend(result.get('items',[]))
    item=next((i for i in candidates if i.get('channelId')==channel),{})
    return dict(id=channel,name=item.get('channel') or item.get('title') or '',
                avatar=item.get('image','') if item.get('kind')=='channel' else '',
                subscribers=item.get('subscribers'),total_views=item.get('views') if item.get('kind')=='channel' else None,
                public=True,can_manage_youtube=False)

_PUBLIC_CACHE={}
_PUBLIC_SLOTS=threading.BoundedSemaphore(2)


def public_channel_details(store,channel):
    from html.parser import HTMLParser
    base=public_channel(store,channel)
    with _LOCK:
        cached=_PUBLIC_CACHE.get(channel)
        if cached and cached[0]>time.time():return dict(base,**cached[1])
    class Metadata(HTMLParser):
        def __init__(self):super().__init__();self.tags={}
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs)
            if tag=='meta' and attrs.get('property') in ('og:image','og:title','og:description'):
                self.tags[attrs['property']]=attrs.get('content','')
    result={};ttl=60
    try:
        with _PUBLIC_SLOTS:
            parser=Metadata();parser.feed(sources.fetch('https://www.youtube.com/channel/'+channel+'/about',limit=4_000_000,timeout=8).decode('utf-8','replace'))
        avatar=parser.tags.get('og:image','')
        if avatar.startswith(('https://yt3.ggpht.com/','https://yt3.googleusercontent.com/')):result['avatar']=avatar
        name=parser.tags.get('og:title','')
        if name and name!='YouTube':result['name']=name[:200]
        if parser.tags.get('og:description'):result['description']=parser.tags['og:description'][:2000]
        if result:ttl=86400
    except (ValueError,OSError):pass
    with _LOCK:
        if len(_PUBLIC_CACHE)>=512:_PUBLIC_CACHE.pop(next(iter(_PUBLIC_CACHE)))
        _PUBLIC_CACHE[channel]=(time.time()+ttl,result)
    return dict(base,**result)
