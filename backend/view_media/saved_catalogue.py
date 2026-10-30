"""Persist public media cards; requests read SQLite while the worker refreshes.

This is observed metadata, not a mirror of every provider's archive. No video
downloads, credentials, grab links or stream URLs belong in this index.
"""
import hashlib
import json
import re
import time
from datetime import datetime,timezone
from . import adapters,global_search
from .ordering import ordered
from .movie_sections import unreleased
from .identity import valid_card,movie_identity
from .providers import initialise as initialise_cache

PAGES=3
DISPLAY_LIMIT=1000

def enabled(store):
    from ..settings.catalogue import FIELDS
    return FIELDS['enabled'].read(store)

def interval(store):
    from ..settings.catalogue import FIELDS
    return FIELDS['minutes'].read(store)*60

def initialise(store):
    initialise_cache(store)
    with store.connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS saved_media_catalogue (
          signature TEXT NOT NULL,origin TEXT NOT NULL,id TEXT NOT NULL,kind TEXT NOT NULL,
          title TEXT NOT NULL,body TEXT NOT NULL,updated REAL NOT NULL,
          PRIMARY KEY(signature,origin,id));
          CREATE INDEX IF NOT EXISTS saved_media_kind ON saved_media_catalogue(signature,kind);
          CREATE TABLE IF NOT EXISTS saved_media_lists (
          signature TEXT NOT NULL,media TEXT NOT NULL,collection TEXT NOT NULL,
          body TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(signature,media,collection));''')

def signature(store):
    from ..discovery_engine import browse_cache_key
    return hashlib.sha256(browse_cache_key(dict(media='all',q='',sort='source',offset=0,collection='',category='all',cursor=None,**{'global':True}),store).encode()).hexdigest()

def state(store,key):
    with store.connect() as db:row=db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',('saved:state:'+key,)).fetchone()
    return (json.loads(row['body']),row['updated']) if row else ({},0)

def save_state(store,key,value):
    with store.connect() as db:db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',('saved:state:'+key,json.dumps(value),time.time()))

def valid(store,sig,generation):
    return enabled(store) and not store.reset_pending() and signature(store)==sig and store.get('catalogue_generation','0')==generation

def _records(sig,origin,items):
    now=time.time()
    for item in items:
        if not isinstance(item,dict) or not item.get('id') or not item.get('title') or not isinstance(item.get('action'),dict) or not valid_card(item):continue
        # Only existing public card fields may enter the searchable index.
        allowed=('id','title','kind','image','backdrop','year','description','rating','date','dateApproximate','added','releaseDate','lastEpisodeReleased','channel','channelId','action','available','releaseCount','metadataVerified','_entry','durationSeconds','runtimeMinutes','duration','metadata_pending','count','children','season','number')
        item={key:value for key,value in item.items() if key in allowed}
        action=item['action'];action_type=action.get('type')
        if action_type=='discover':
            from ..discover_store import clean_item
            try:item['action']=dict(type='discover',item=clean_item(action['item']))
            except (ValueError,KeyError,TypeError):continue
        elif action_type in ('local','youtube'):
            if not isinstance(action.get('id'),str):continue
            item['action']=dict(type=action_type,id=action['id'])
        elif action_type=='local-group':
            if origin!='Local Library' or item.get('kind') not in ('show','shows') or not re.fullmatch(r'group-[0-9a-f]{24}',str(action.get('id') or '')):continue
            children=item.get('children')
            if not isinstance(children,list):continue
            children=[json.loads(record[5]) for child in children if isinstance(child,dict)
                      and isinstance(child.get('action'),dict) and child['action'].get('type')=='local'
                      and re.fullmatch(r'[0-9a-f]{32}',str(child['action'].get('id') or ''))
                      for record in _records(sig,origin,[{key:value for key,value in child.items() if key!='_entry'}])]
            if not children:continue
            item.update(action=dict(type='local-group',id=action['id']),children=children,count=len(children))
            item.pop('_entry',None)
        elif action_type=='vod':
            entry=action.get('item') or {}
            item['action']=dict(type='vod',source=action.get('source',''),item={key:entry.get(key,'') for key in ('title','path','kind')})
        else:continue
        if action_type!='local-group':
            item.pop('count',None);item.pop('children',None)
        for field in ('season','number'):
            if type(item.get(field)) is not int or not 0<=item[field]<=9999:item.pop(field,None)
        if '_entry' in item:item['_entry']={key:item['_entry'].get(key,'') for key in ('source','path','kind')}
        from ..media_duration import fields as duration_fields
        seconds=item.pop('durationSeconds',None);minutes=item.pop('runtimeMinutes',None);clock=item.pop('duration',None)
        pending=item.pop('metadata_pending',False)
        item.update(duration_fields(seconds=seconds if type(seconds) in (int,float) else None,
                                    minutes=minutes if type(minutes) in (int,float) else None,clock=clock,pending=pending is True))
        kind={'movies':'movie','shows':'show','youtube':'video','series':'show','tv':'show'}.get(item.get('kind'),item.get('kind'))
        item.update(kind=kind,origin=origin)
        yield (sig,origin,item['id'],kind,(item['title']+' '+item.get('channel','')).casefold(),json.dumps(item),now)

def remember(store,sig,origin,items,replace=False):
    rows=list(_records(sig,origin,items))
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if replace:db.execute('DELETE FROM saved_media_catalogue WHERE signature=? AND origin=?',(sig,origin))
        db.executemany('''INSERT INTO saved_media_catalogue VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(signature,origin,id) DO UPDATE SET kind=excluded.kind,
          title=CASE WHEN instr(saved_media_catalogue.title,excluded.title)>0 THEN saved_media_catalogue.title ELSE saved_media_catalogue.title||' '||excluded.title END,
          body=json_patch(saved_media_catalogue.body,excluded.body),updated=excluded.updated''',rows)

def remember_search(store,sig,items,query=''):
    for origin in ('NZB','VOD','Local Library','YouTube'):
        rows=[item for item in items if item.get('origin')==origin]
        remember(store,sig,origin,rows)
        # Release filenames and provider aliases can match words absent from the
        # canonical display title. Retain that observed match across refreshes.
        if query:
            with store.connect() as db:db.executemany('UPDATE saved_media_catalogue SET title=title||? WHERE signature=? AND origin=? AND id=? AND instr(title,?)=0',
                [(' '+query.casefold(),sig,origin,item['id'],query.casefold()) for item in rows])

def list_result(store,sig,media,collection,result):
    items=global_search.merge(result.get('items',[]))
    if collection=='premiering':items=[item for item in items if unreleased(item)]
    result=dict(result,items=items,nextOffset=None,nextCursor=None,completeSet=True)
    result.pop('preparing',None)
    with store.connect() as db:db.execute('INSERT OR REPLACE INTO saved_media_lists VALUES(?,?,?,?,?)',(sig,media,collection,json.dumps(result),time.time()))
    remember(store,sig,'Catalogue' if collection=='premiering' else 'NZB',items)

def seed(store,sig):
    """Reuse current-account snapshots immediately, without provider requests."""
    from ..discovery_engine import browse_cache_key
    with store.connect() as db:
        rows=db.execute("SELECT key,body FROM view_media_cache WHERE key GLOB 'engine:nzb-browse:*' OR key GLOB 'engine:global-search:*' ORDER BY updated DESC LIMIT 1000").fetchall()
    lists={}
    for row in rows:
        try:
            raw=row['key'][row['key'].index(':{')+1:];args=json.loads(raw)
            if browse_cache_key(args,store)!=row['key']:continue
            result=json.loads(row['body'])
            if args.get('global'):remember_search(store,sig,result.get('items',[]),args.get('q',''));continue
            media,collection=args['media'],args.get('collection') or 'all'
            if media not in ('movies','shows') or args.get('q'):continue
            lists.setdefault((media,collection),[]).append((args.get('offset',0),result))
        except (ValueError,KeyError,TypeError):continue
    for (media,collection),pages in lists.items():
        pages.sort(key=lambda page:page[0]);result=dict(pages[0][1],items=global_search.merge([item for _,page in pages for item in page.get('items',[])]))
        list_result(store,sig,media,collection,result)


def observations(app,store,sig,media='all',limit=1000):
    """Reuse current saved observations without starting a scan/provider or writing SQLite."""
    from ..discovery_engine import browse_cache_key
    from .. import discover_sources
    from ..media_duration import fields as duration_fields
    kinds={'movies':{'movie'},'shows':{'show'},'youtube':{'video','channel'}}.get(media)
    groups={origin:{} for origin in ('NZB','VOD','YouTube','Local Library','Catalogue')}
    priority={origin:[] for origin in groups};preferred={origin:set() for origin in groups}
    stamps=[];warming=False;limited=False
    def decoded(value):
        try:return json.loads(value)
        except (ValueError,TypeError):return None
    def add(origin,items,overwrite=False):
        if origin not in groups:return
        for candidate in items:
            try:
                for record in _records(sig,origin,[candidate]):
                    item=json.loads(record[5])
                    if not kinds or item['kind'] in kinds:
                        key=movie_identity(item['action']['item']) if item['kind']=='movie' and item['action']['type']=='discover' else item['id']
                        if overwrite or key not in groups[origin]:groups[origin][key]=item
                        if overwrite and key not in preferred[origin]:priority[origin].append(key);preferred[origin].add(key)
            except (ValueError,TypeError,KeyError,AttributeError):continue
    try:indexers=discover_sources.indexers_for(None,store)
    except ValueError:indexers=[]
    with store.connect() as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Existing saved observations remain useful; authoritative current
        # records below replace duplicates without requiring the worker switch.
        if 'saved_media_catalogue' in tables:
            for origin in groups:
                where='signature=? AND origin=?';params=[sig,origin]
                # Search persists file leaves. Browsing rebuilds current show
                # parents below, so old observed episodes cannot leak beside them.
                if origin=='Local Library':where+=" AND kind!='show'"
                if kinds:where+=' AND kind IN ('+','.join('?' for _ in kinds)+')';params.extend(sorted(kinds))
                rows=db.execute('SELECT body,updated FROM saved_media_catalogue WHERE '+where+' ORDER BY updated DESC,id LIMIT ?',(*params,limit+1)).fetchall()
                limited|=len(rows)>limit
                add(origin,[decoded(r['body']) for r in rows[:limit]])
                stamps.extend(r['updated'] for r in rows[:limit])
        prefixes=set()
        for args in (dict(media='movies',q='',sort='source',offset=0,collection='all',category='all',cursor=None),
                     dict(media='movies',q='',sort='source',offset=0,collection='trending',category='all',cursor=None),
                     dict(media='all',q='',sort='source',offset=0,collection='',category='all',cursor=None,**{'global':True})):
            prefixes.add(browse_cache_key(args,store,indexers).split(':{',1)[0]+':')
        frames=db.execute('SELECT key,length(body) AS bytes,updated FROM view_media_cache WHERE '+ ' OR '.join('(key>=? AND key<?)' for _ in prefixes)+' ORDER BY updated DESC LIMIT 51',
                          [value for prefix in sorted(prefixes) for value in (prefix,prefix+'\uffff')]).fetchall() if 'view_media_cache' in tables else []
        limited|=len(frames)>50
        remaining=16_000_000;seen_frames=set()
        for row in frames[:50]:
            try:
                if row['bytes']>remaining:limited=True;continue
                args=json.loads(row['key'][row['key'].index(':{')+1:])
                if browse_cache_key(args,store,indexers)!=row['key']:continue
                remaining-=row['bytes'];body=db.execute('SELECT body FROM view_media_cache WHERE key=?',(row['key'],)).fetchone()
                result=json.loads(body['body']);items=result.get('items',[])
                if not isinstance(items,list):continue
                # Global frames may contain private-source observations. Those
                # are rebuilt below from current source records instead.
                if args.get('global'):items=[i for i in items if isinstance(i,dict) and i.get('origin')=='NZB']
                fresh=[]
                for item in items[:limit]:
                    if not isinstance(item,dict):continue
                    key=item.get('id')
                    if key not in seen_frames:fresh.append(item);seen_frames.add(key)
                add('NZB',fresh,overwrite=True);limited|=len(items)>limit
                stamps.append(row['updated']);warming|=bool(result.get('preparing') and row['updated']>time.time()-120)
            except (ValueError,TypeError,KeyError,AttributeError):continue
        if media in ('all','youtube'):
            rows=db.execute("SELECT v.id,v.channel,v.published,v.data,c.data AS channel_data FROM videos v JOIN channels c ON c.id=v.channel WHERE c.active=1 ORDER BY v.published DESC,v.id LIMIT ?",(limit+1,)).fetchall()
            limited|=len(rows)>limit;items=[]
            for row in rows[:limit]:
                video=decoded(row['data']);channel=decoded(row['channel_data'])
                if not isinstance(video,dict) or not isinstance(channel,dict):continue
                raw=dict(id='youtube:'+row['id'],kind='video',title=video.get('title') or 'Untitled',image=video.get('thumbnail') or '',channel=channel.get('name') or '',channelId=row['channel'],published=row['published'],description=video.get('description') or '',
                         **duration_fields(seconds=video.get('duration_seconds'),clock=video.get('duration'),pending=video.get('metadata_pending')))
                items.append(adapters.card(raw,dict(type='youtube',id=row['id'])))
            add('YouTube',items,overwrite=True)
        if media in ('all','movies','shows'):
            raw_kinds=('movies',) if media=='movies' else ('series','tv') if media=='shows' else ('movies','series','tv')
            rows=db.execute('''SELECT f.source,f.path,f.title,f.kind,s.name,t.data AS metadata,c.added
                FROM m3u_files f JOIN m3u_sources s ON s.id=f.source
                LEFT JOIN m3u_catalog_entries c ON c.source=f.source AND c.path=f.path
                LEFT JOIN m3u_catalog_titles t ON t.key=c.key
                WHERE COALESCE(json_extract(s.options,'$.enabled'),1)=1 AND f.kind IN ('''+','.join('?' for _ in raw_kinds)+''')
                AND lower(f.path) NOT LIKE '%sample%' AND lower(f.title) NOT LIKE '%sample%'
                ORDER BY c.added DESC,f.source,f.path LIMIT ?''',(*raw_kinds,limit+1)).fetchall()
            limited|=len(rows)>limit;items=[];files={}
            identities=list({(row['source'],row['path']) for row in rows[:limit]})
            for start in range(0,len(identities),400):
                batch=identities[start:start+400]
                for entry in db.execute('SELECT source,path,data FROM m3u_file_info WHERE (source,path) IN (VALUES '+','.join('(?,?)' for _ in batch)+')',[v for pair in batch for v in pair]):
                    try:files[(entry['source'],entry['path'])]=json.loads(entry['data'])
                    except (TypeError,ValueError):pass
            for row in rows[:limit]:
                data=decoded(row['metadata'] or '{}');data=data if isinstance(data,dict) else {}
                facts=files.get((row['source'],row['path']),{});facts=facts if isinstance(facts,dict) else {}
                length=duration_fields(minutes=data.get('runtimeMinutes') if row['kind']=='movies' else None)
                length.update(duration_fields(seconds=facts.get('durationSeconds'),minutes=facts.get('runtimeMinutes')))
                raw=dict(id=row['source']+':'+row['path'],title=data.get('title') if data.get('locked') and data.get('title') else row['title'],kind='movie' if row['kind']=='movies' else 'show',image=data.get('artwork') or '',description=data.get('description') or '',rating=data.get('rating') or '',added=row['added'],
                         **length)
                items.append(adapters.card(raw,dict(type='vod',source=row['source'],item=dict(title=row['title'],path=row['path'],kind=row['kind']))))
            add('VOD',items,overwrite=True)
        known=db.execute("SELECT m.path,d.updated AS stamp FROM media_files m JOIN downloads d ON d.id=m.id WHERE d.state='complete' UNION ALL SELECT destination AS path,updated AS stamp FROM m3u_downloads WHERE state='complete' UNION ALL SELECT o.path,r.updated AS stamp FROM discover_owned o JOIN discover_requests r ON r.id=o.request_id WHERE o.state='complete' ORDER BY stamp DESC LIMIT 251").fetchall()
    local=adapters.local.known_snapshot(app.config['MEDIA_DIR'],[row['path'] for row in known if row['path']],limit=limit+1,kinds={'movies':{'movies'},'shows':{'shows'},'youtube':{'youtube'}}.get(media),group_shows=True)
    if local:
        warming|=bool(local.get('scanning'));items=adapters.local.library_dates(store,local,app.config['MEDIA_DIR'])
        parents=adapters.local.grouped([item for item in items if item['kind']=='shows'],local,app.config['MEDIA_DIR'])
        adapters.local.register_group_art(app.config['MEDIA_DIR'],local,parents)
        by_child={child['id']:parent for parent in parents for child in parent.get('children',[parent])}
        seen=set();grouped=[]
        for item in items:
            card=by_child.get(item['id'],item)
            if card['id'] not in seen:grouped.append(card);seen.add(card['id'])
        items=grouped
        add('Local Library',[dict(item,kind={'movies':'movie','shows':'show','youtube':'video'}.get(item['kind'],item['kind'])) for item in items[:limit]],overwrite=True)
        limited|=len(items)>limit or bool(local.get('groupLimited'))
    # Deduplicate each source, then bound the union fairly so one large channel
    # catalogue cannot exclude every saved NZB/VOD/local observation.
    groups={origin:[items[key] for key in priority[origin]]+[item for key,item in items.items() if key not in preferred[origin]] for origin,items in groups.items()}
    counts={origin:len(items) for origin,items in groups.items()};selected=[]
    for index in range(max(counts.values(),default=0)):
        for items in groups.values():
            if index<len(items):selected.append(items[index])
        if len(selected)>=limit:break
    cards=global_search.merge(selected[:limit]);limited|=sum(counts.values())>limit
    return dict(items=cards,counts=counts,warming=warming,limited=limited,updated=max(stamps,default=0),indexed=bool(stamps),backgroundEnabled=enabled(store),localKnown=local is not None)

def refresh_local(app,store,sig,generation):
    from .download_sources import catalogue
    started=time.time()
    for media in ('movies','shows'):
        offset=0
        while valid(store,sig,generation):
            page=catalogue(store,media,offset=offset,limit=1000,include_entry=True)
            vod=[]
            for raw in page['items']:
                entry=raw.pop('_entry');item=adapters.card(raw,dict(type='discover',item=raw))
                if raw['kind']=='show':item['action']=dict(type='vod',source=entry['source'],item=dict(title=raw['title'],path=entry['path'],kind=entry['kind']))
                item['_entry']=entry;vod.append(item)
            if not valid(store,sig,generation):return False
            remember(store,sig,'VOD',vod)
            if page['nextOffset'] is None:break
            offset=page['nextOffset']
    if not valid(store,sig,generation):return False
    with store.connect() as db:db.execute("DELETE FROM saved_media_catalogue WHERE signature=? AND origin='VOD' AND updated<?",(sig,started))
    after=''
    while valid(store,sig,generation):
        youtube=[]
        with store.connect() as db:rows=db.execute('SELECT v.id,v.channel,v.published,v.data,c.data AS channel_data FROM videos v JOIN channels c ON c.id=v.channel WHERE c.active=1 AND v.id>? ORDER BY v.id LIMIT 1000',(after,)).fetchall()
        for row in rows:
            video=json.loads(row['data']);channel=json.loads(row['channel_data'])
            item=dict(id='youtube:'+row['id'],kind='video',title=video.get('title') or 'Untitled',image=video.get('thumbnail') or '',channel=channel.get('name') or '',channelId=row['channel'],published=row['published'],description=video.get('description') or '')
            youtube.append(adapters.card(item,dict(type='youtube',id=row['id'])))
        # Public search observations remain useful even when not subscribed.
        if not valid(store,sig,generation):return False
        remember(store,sig,'YouTube',youtube)
        if not rows:break
        after=rows[-1]['id']
    page=adapters.local.inventory(app.config['MEDIA_DIR'])
    local=[dict(item,kind={'movies':'movie','shows':'show','youtube':'video'}.get(item['kind'],item['kind'])) for item in adapters.local.library_dates(store,page,app.config['MEDIA_DIR'])]
    if valid(store,sig,generation):remember(store,sig,'Local Library',local,replace=not page['scanning'])
    return not page['scanning']

def targets():
    from .movie_sections import SECTIONS as movies
    from .show_sections import SECTIONS as shows
    return [(media,section) for section in ('premiering','trending','new','all','top','recent','endorsed') for media,sections in [('movies',movies),('shows',shows)] if section in dict(sections)]

def crawl_key(sig,generation,media):
    return 'all-pages:'+sig+':'+generation+':'+media

def tv_retry_prefix(sig,generation):
    return 'saved:state:tv-metadata:'+sig+':'+generation+':'

def queue_tv_metadata(store,sig,generation,identities):
    now=time.time()
    with store.connect() as db:
        db.executemany('INSERT OR IGNORE INTO view_media_cache VALUES(?,?,?)',
            [(tv_retry_prefix(sig,generation)+identity,json.dumps(dict(tvdb=identity,retryAt=now+60)),now)
             for identity in set(identities) if re.fullmatch(r'[1-9][0-9]{0,11}',identity)])

def retry_tv_metadata(store,sig,generation):
    """Retry optional metadata independently; unavailable mappings are normal."""
    prefix=tv_retry_prefix(sig,generation)
    with store.connect() as db:
        pending=db.execute("SELECT key,body FROM view_media_cache WHERE key LIKE ? AND CAST(json_extract(body,'$.retryAt') AS REAL)<=? ORDER BY updated LIMIT 1",(prefix+'%',time.time())).fetchone()
    if not pending or not valid(store,sig,generation):return
    from .show_metadata import details
    job=json.loads(pending['body']);identity=job['tvdb']
    try:metadata=details(store,identity)
    except (ValueError,TypeError,KeyError):
        if valid(store,sig,generation):
            with store.connect() as db:db.execute('UPDATE view_media_cache SET body=?,updated=? WHERE key=?',
                (json.dumps(dict(tvdb=identity,retryAt=time.time()+max(900,interval(store)))),time.time(),pending['key']))
        return
    if not valid(store,sig,generation):return
    with store.connect() as db:rows=db.execute("SELECT body FROM saved_media_catalogue WHERE signature=? AND origin='NZB' AND kind='show' AND json_extract(body,'$.action.item.externalId')=?",(sig,identity)).fetchall()
    items=[]
    for row in rows:
        item=json.loads(row['body']);raw=item['action']['item']
        fields={key:value for key,value in metadata.items() if key not in ('id','kind','externalId') and value not in ('',None)}
        fields['lastEpisodeReleased']=metadata.get('lastEpisodeReleased','')
        item.update(fields);raw.update(fields);items.append(item)
    remember(store,sig,'NZB',items)
    with store.connect() as db:db.execute('DELETE FROM view_media_cache WHERE key=?',(pending['key'],))

def refresh_all(app,store,sig,generation,media):
    """Checkpoint work in small batches, without limiting the catalogue size."""
    key=crawl_key(sig,generation,media)
    progress,_=state(store,key)
    if progress.get('done'):progress={}
    cursor=progress.get('cursor');offset=progress.get('offset',0)
    for _ in range(PAGES):
        if not valid(store,sig,generation):return
        try:
            result=adapters.discover(app,store,dict(media=media,collection='all',sort='source',q='',offset=offset,cursor=cursor,category='all'))
        except Exception as exc:
            from ..discover_indexer_rate import limited_indexers,limit_message
            names=limited_indexers(exc)
            warnings=[limit_message(names)+' Checks will resume automatically.' if names else 'The NZB catalogue check could not finish. Saved titles are retained; checks will resume automatically.']
            if valid(store,sig,generation):save_state(store,key,dict(cursor=cursor,offset=offset,done=False,warnings=warnings,retryAt=max(time.time()+60,getattr(exc,'retry_at',0) or 0)))
            return
        if not valid(store,sig,generation):return
        remember(store,sig,'NZB',result['items'])
        if media=='shows':queue_tv_metadata(store,sig,generation,result.get('metadataPending',[]))
        warnings=list(result.get('sourceWarnings',result.get('warnings',[])))
        # A partially successful federated page must be retried at the same
        # offset, so the unavailable indexer's titles are not skipped.
        if result.get('sourceIncomplete',result.get('incomplete')) or warnings:
            save_state(store,key,dict(cursor=cursor,offset=offset,done=False,warnings=warnings,retryAt=max(time.time()+60,result.get('retryAt') or 0,result.get('sourceRetryAt') or 0)))
            return
        following=json.loads(result['nextCursor']) if result.get('nextCursor') else None
        if following is not None and (set(following)!={'nzb'} or type(following['nzb']) is not int or following['nzb']<=(cursor or {}).get('nzb',0)):
            save_state(store,key,dict(cursor=cursor,offset=offset,done=False,warnings=['The NZB catalogue did not advance. Saved titles are retained; checks will retry automatically.'],retryAt=time.time()+60))
            return
        cursor=following;offset+=1
        save_state(store,key,dict(cursor=cursor,offset=offset,done=cursor is None,warnings=[],
                                  catalogueSource=result.get('catalogueSource',''),scope=result.get('scope','')))
        if cursor is None:return

def refresh_list(app,store,sig,generation,media,collection):
    if collection=='all':return refresh_all(app,store,sig,generation,media)
    items=[];warnings=[];cursor=None;incomplete=False;result={}
    for batch in range(PAGES):
        if not valid(store,sig,generation):return
        result=adapters.discover(app,store,dict(media=media,collection=collection,sort='source',q='',offset=batch,cursor=cursor,category='all'))
        items.extend(result['items']);warnings.extend(result.get('warnings',[]));incomplete|=bool(result.get('incomplete'))
        cursor=json.loads(result['nextCursor']) if result.get('nextCursor') else None
        if not cursor or incomplete:break
    if not valid(store,sig,generation):return
    # A failed refresh must retain previously saved, verified titles.
    if incomplete:
        with store.connect() as db:old=db.execute('SELECT body FROM saved_media_lists WHERE signature=? AND media=? AND collection=?',(sig,media,collection)).fetchone()
        if old:items.extend(json.loads(old['body'])['items'])
    list_result(store,sig,media,collection,dict(result,items=global_search.merge(items),warnings=list(dict.fromkeys(warnings)),incomplete=incomplete))
    return result

def tick(app,store):
    if not enabled(store) or store.reset_pending() or not store.get('setup_complete'):return
    initialise(store);sig=signature(store);generation=store.get('catalogue_generation','0')
    key=sig+':'+generation
    bootstrap,stamp=state(store,'local:'+key)
    if stamp+interval(store)<=time.time():
        save_state(store,'activity:'+sig,dict(stage='Indexing saved server records'))
        if not stamp:
            with store.connect() as db:known=db.execute('SELECT 1 FROM saved_media_catalogue WHERE signature=? LIMIT 1',(sig,)).fetchone()
            if not known:seed(store,sig)
        complete=refresh_local(app,store,sig,generation)
        if valid(store,sig,generation):
            save_state(store,'local:'+key,dict(ready=complete))
            if not complete:
                with store.connect() as db:db.execute('UPDATE view_media_cache SET updated=? WHERE key=?',(time.time()-interval(store)+30,'saved:state:local:'+key))
    retry_tv_metadata(store,sig,generation)
    due=[]
    for media,collection in targets():
        checked,stamp=state(store,'list:'+key+':'+media+':'+collection)
        if collection=='all':
            progress,_=state(store,crawl_key(sig,generation,media))
            if progress.get('retryAt',0)>time.time():continue
            if progress.get('done') and stamp+interval(store)>time.time():continue
        else:
            from .indexer_catalogue import POLICY
            with store.connect() as db:policy=db.execute('SELECT json_extract(body,\'$.listPolicy\') FROM saved_media_lists WHERE signature=? AND media=? AND collection=?',(sig,media,collection)).fetchone()
            if policy and policy[0]==POLICY and stamp+interval(store)>time.time() and not (checked.get('retryAt') and checked['retryAt']<=time.time()):continue
            if not policy and stamp+interval(store)>time.time():continue
        due.append((stamp,media,collection))
    # Round-robin unfinished catalogues and other due lists; one long archive
    # must not starve TV or the small named lists.
    if due:
        _,media,collection=min(due)
        save_state(store,'activity:'+sig,dict(stage='Refreshing '+media+' · '+collection))
        result=None
        try:result=refresh_list(app,store,sig,generation,media,collection)
        except Exception:
            if valid(store,sig,generation):save_state(store,'error:'+sig,dict(message='Some catalogue sources could not refresh. Saved results are retained; provider limits are respected.'))
        if valid(store,sig,generation):save_state(store,'list:'+key+':'+media+':'+collection,dict(checked=True,retryAt=result.get('indexerRetryAt') if isinstance(result,dict) else None))
        return
    save_state(store,'activity:'+sig,dict(stage='Saved catalogue ready'))

def loop(app,store,stop):
    while not stop.is_set():
        try:tick(app,store)
        except Exception:app.logger.warning('Saved metadata catalogue refresh will retry.',exc_info=False)
        stop.wait(15)

def status(store):
    initialise(store);sig=signature(store)
    with store.connect() as db:
        counts={row['kind']:row['count'] for row in db.execute('SELECT kind,count(DISTINCT id) AS count FROM saved_media_catalogue WHERE signature=? GROUP BY kind',(sig,))}
        stamp=db.execute('SELECT max(updated) FROM saved_media_catalogue WHERE signature=?',(sig,)).fetchone()[0]
    progress,_=state(store,'activity:'+sig);error,_=state(store,'error:'+sig)
    return dict(enabled=enabled(store),counts=counts,updatedAt=datetime.fromtimestamp(stamp,timezone.utc).isoformat() if stamp else None,
        stage=progress.get('stage') or 'Waiting for the background worker',message=error.get('message',''),
        scope='Saved observations and configured catalogues. View All continues through available NZB pages in the background without a title cap. Other named lists check three batches per refresh. VOD, local files and subscriptions use existing server records; YouTube also includes previously found results.')

def _cards(store,sig,query='',origin=None,kind=None,vod_source=None,subscriptions=False,limit=DISPLAY_LIMIT):
    where='signature=?';params=[sig]
    if query:where+=' AND title LIKE ? ESCAPE \"!\"';params.append('%'+query.casefold().replace('!','!!').replace('%','!%').replace('_','!_')+'%')
    if origin:where+=' AND origin=?';params.append(origin)
    if kind:where+=' AND kind=?';params.append(kind)
    if vod_source is not None:where+=" AND json_extract(body,'$._entry.source')=?";params.append(vod_source)
    if subscriptions:where+=" AND EXISTS(SELECT 1 FROM videos v JOIN channels c ON c.id=v.channel WHERE c.active=1 AND v.id=json_extract(saved_media_catalogue.body,'$.action.id'))"
    sql='SELECT body FROM saved_media_catalogue WHERE '+where+' ORDER BY title,id'
    if limit is not None:sql+=' LIMIT ?';params.append(limit)
    with store.connect() as db:rows=db.execute(sql,params).fetchall()
    return [item for row in rows if valid_card(item:=json.loads(row['body']))]

def browse(app,store,args,source='discover'):
    initialise(store);sig=signature(store);media=args['media'];collection=args.get('collection') or 'all'
    if source=='vod':
        # VOD already lives in SQLite. Preserve its native per-file actions,
        # grouping and player payload instead of reconstructing them from search.
        # Paginate: shipping the whole playlist freezes the browser.
        result=adapters.vod(app,store,dict(args,q=args.get('q',''),collection=args.get('collection',''),offset=args.get('offset') or 0,_limit=adapters.PAGE))
        return dict(result,completeSet=result.get('nextOffset') is None,nextCursor=None,loading=False,refreshing=False,
            freshness=dict(source='Saved server catalogue',scope='Saved VOD files from the selected playlist, loaded as you scroll.'))
    if source=='discover':
        if collection=='all':
            progress,stamp=state(store,crawl_key(sig,store.get('catalogue_generation','0'),media))
            result=dict(items=_cards(store,sig,origin='NZB',kind='movie' if media=='movies' else 'show',limit=None),label='View All Movies' if media=='movies' else 'View All TV Shows',warnings=progress.get('warnings',[]))
            scope=str(len(result['items']))+' saved titles; they load as you scroll. '+('The last background scan reached the end of the available catalogue pages.' if progress.get('done') else 'Background indexing is still collecting available catalogue pages.')
            if progress.get('scope'):scope+=' '+progress['scope']
            if media=='shows':
                with store.connect() as db:pending=db.execute('SELECT count(*) FROM view_media_cache WHERE key LIKE ?',(tv_retry_prefix(sig,store.get('catalogue_generation','0'))+'%',)).fetchone()[0]
                if pending:scope+=' Extra TV dates or artwork are unavailable for '+str(pending)+(' show.' if pending==1 else ' shows.')+' Known dates are kept; unknown dates sort last. Metadata retries automatically.'
            warming=False
        else:
            with store.connect() as db:row=db.execute('SELECT body,updated FROM saved_media_lists WHERE signature=? AND media=? AND collection=?',(sig,media,collection)).fetchone()
            result=json.loads(row['body']) if row else dict(items=[],label='Saved catalogue',warnings=[])
            if collection=='premiering':result['items']=[item for item in result['items'] if unreleased(item)]
            if result.get('listPolicy'):
                scope='Saved list: '+str(len(result['items']))+' titles. '+result.get('scope','')
                if not result.get('indexerComplete'):scope+=' TMDB fallback checks up to three batches per refresh.'
            else:scope='Saved list: '+str(len(result['items']))+' titles; they load as you scroll. Background refresh checks up to three provider batches per list.'
            stamp=row['updated'] if row else 0
            warming=not row
    else:
        origin={'vod':'VOD','local':'Local Library','subscriptions':'YouTube'}[source]
        collections=[]
        if source=='vod':
            with store.connect() as db:collections=[dict(row) for row in db.execute("SELECT id,name FROM m3u_sources WHERE coalesce(json_extract(options,'$.enabled'),1)=1 ORDER BY name")]
            collection=args.get('collection') or (collections[0]['id'] if collections else '')
            if collection and collection not in {row['id'] for row in collections}:raise ValueError('Choose an available VOD source.')
        items=_cards(store,sig,origin=origin,kind={'movies':'movie','shows':'show','youtube':'video'}.get(media,media),vod_source=collection if source=='vod' else None,subscriptions=source=='subscriptions',limit=None)
        if source=='vod':
            for item in items:
                entry=item['_entry'];item['action']=dict(type='vod',source=entry['source'],item=dict(title=item['title'],path=entry['path'],kind=entry['kind']))
        result=dict(items=items,label='Saved catalogue',collections=collections);scope=str(len(items))+' saved titles; they load as you scroll.' if media in ('movies','shows') else 'Saved results load as you scroll. Narrow your search for other titles.';stamp=0
    error=state(store,'error:'+sig)[0].get('message','')
    if error and not result['items']:result['warnings']=[*result.get('warnings',[]),error]
    bootstrap,_=state(store,'local:'+sig+':'+store.get('catalogue_generation','0'))
    warming=warming if source=='discover' else not bootstrap.get('ready') and not result['items']
    items=ordered(result['items'],args['sort']);offset=args.get('offset') or 0;page=items[offset:offset+adapters.PAGE]
    return dict(result,items=page,collection=collection,completeSet=offset+adapters.PAGE>=len(items),nextOffset=offset+adapters.PAGE if offset+adapters.PAGE<len(items) else None,nextCursor=None,
        loading=bool(warming and not error),refreshing=False,freshness=dict(source='Saved server catalogue',fetchedAt=datetime.fromtimestamp(stamp,timezone.utc).isoformat() if stamp else None,scope=scope))

def search(app,store,args):
    from .saved_search import search as saved_search
    return saved_search(app,store,args)
