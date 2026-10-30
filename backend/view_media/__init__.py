"""Authenticated universal browsing contract; adapters own source-specific retrieval."""
import json
from flask import jsonify, request, send_file
from .adapters import PAGE, REGISTRY
from .local import media_file


def register(app, store, access):
    @app.get('/api/view-media/catalogue-status')
    def saved_catalogue_status():
        denied=access(local_action=True)
        if denied:return denied
        from .saved_catalogue import status
        return jsonify(status(store))

    @app.get('/api/view-media/sources')
    def view_media_sources():
        denied=access(local_action=True)
        if denied:return denied
        from .saved_search import ALL_SOURCE
        items=[dict(id=key,**{k:v for k,v in value.items() if k!='browse'}) for key,value in REGISTRY.items()]
        for item in items:
            if item['id']=='subscriptions':item['label']='YouTube'
        return jsonify(items=[ALL_SOURCE,*items])

    @app.get('/api/view-media/search')
    def view_media_global_search():
        denied=access(local_action=True)
        if denied:return denied
        from ..discover import _limited
        if not _limited(app,'view_media_hits',60):return jsonify(error='Too many media requests. Try again shortly.'),429
        try:
            from .search_snapshot import validate_cursor
            from ..discovery_engine import cached_browse
            query=request.args.get('q','').strip()[:160]
            if len(query)<2:raise ValueError('Enter at least two characters to search.')
            sort=request.args.get('sort','release');offset=int(request.args.get('offset','0'));cursor=None
            if sort not in REGISTRY['discover']['sorts'] or not 0<=offset<=250000:raise ValueError('Choose valid search filters.')
            raw=request.args.get('cursor','')
            if len(raw)>200:raise ValueError('Invalid search page.')
            cursor=validate_cursor(raw,offset,sort)
            grouped=request.args.get('grouped','')
            if grouped not in ('','1') or (grouped and (cursor or offset)):raise ValueError('Invalid search page.')
            args=dict(media='all',q=query,sort=sort,offset=offset,collection='',category='all',cursor=cursor,grouped=bool(grouped),**{'global':True})
            from . import saved_catalogue
            if saved_catalogue.enabled(store):
                fresh=request.args.get('fresh','')
                if fresh not in ('','1'):raise ValueError('Invalid search refresh.')
                result=saved_catalogue.search(app,store,dict(args,fresh=fresh=='1'))
            else:result=cached_browse(app,store,args,prefetch=False)
            return jsonify(source='global',media='all',**result)
        except ValueError as exc:return jsonify(error=str(exc)),400
        except Exception:
            app.logger.warning('Global media search could not be loaded',exc_info=False)
            return jsonify(error='Global media search could not finish. Try again shortly.'),502

    @app.get('/api/view-media')
    def view_media_browse():
        denied=access(local_action=True)
        if denied:return denied
        from ..discover import _limited
        if not _limited(app,'view_media_hits',60): return jsonify(error='Too many media requests. Try again shortly.'),429
        try:
            source=request.args.get('source','discover')
            if source=='all':
                from .saved_search import ALL_SOURCE,browse_saved
                media=request.args.get('media','all');sort=request.args.get('sort','new');offset=int(request.args.get('offset','0'))
                if (media not in ALL_SOURCE['media'] or sort not in ALL_SOURCE['sorts'] or not 0<=offset<=250000
                    or request.args.get('q','').strip() or request.args.get('cursor','')):
                    raise ValueError('Choose valid saved catalogue filters. Use Search to find other titles.')
                return jsonify(source='all',media=media,**browse_saved(app,store,dict(media=media,sort=sort,offset=offset)))
            if source not in REGISTRY:raise ValueError('Choose an available media source.')
            adapter=REGISTRY[source]
            args=dict(media=request.args.get('media',adapter['media'][0]),sort=request.args.get('sort',adapter['sorts'][0]),
                      q=request.args.get('q','').strip()[:160],category=request.args.get('category','all')[:40],offset=int(request.args.get('offset','0')),collection=request.args.get('collection','')[:100],cursor=None)
            if source=='subscriptions':
                show_shorts=request.args.getlist('show_shorts')
                if show_shorts and (len(show_shorts)!=1 or show_shorts[0] not in ('0','1')):
                    raise ValueError('Choose a valid Show Shorts filter.')
                args['show_shorts']=show_shorts==['1']
            if source=='discover' and args['media']=='movies':
                args['collection']=args['collection'] or 'all'
                from .movie_sections import SECTIONS
                if args['collection'] not in dict(SECTIONS):raise ValueError('Choose a valid movie section.')
                if 'sort' not in request.args:args['sort']='release'
            if source=='discover' and args['media']=='shows':
                args['collection']=args['collection'] or 'all'
                from .show_sections import SECTIONS,SORTS
                if args['collection'] not in dict(SECTIONS):raise ValueError('Choose a valid TV section.')
                if 'sort' not in request.args:args['sort']='release'
                if args['sort'] not in dict(SORTS):raise ValueError('Choose a valid TV sort order.')
            raw=request.args.get('cursor','')
            if raw:
                if len(raw)>200:raise ValueError('Invalid search page.')
                cursor=json.loads(raw)
                # Chart browsing uses the shared card page size; YouTube search uses 20.
                youtube_page_size=20 if args['q'] or args['collection']=='topics' or (args['category']!='all' and args['collection'] not in ('recommended','related')) else PAGE
                if not isinstance(cursor,dict) or not cursor or any(k not in ('catalogue','vod','nzb','local','youtube','movies','shows') or type(v) is not int or not 0<=v<=250000 or v % ({'youtube':youtube_page_size,'vod':24,'local':24,'nzb':100}.get(k,20 if args['media'] in ('movies','shows') else 24)) for k,v in cursor.items()):raise ValueError('Invalid search page.')
                if source=='discover':
                    expected=('movies' if args['media']=='movies' else 'shows') if args['collection']!='all' else 'nzb'
                    if set(cursor)!={expected}:raise ValueError('Invalid NZB search page.')
                args['cursor']=cursor
            if args['media'] not in adapter['media'] or args['sort'] not in adapter['sorts'] or not 0<=args['offset']<=250000:
                raise ValueError('Choose valid media filters.')
            from . import saved_catalogue
            # Native records keep local changes truthful and apply the YouTube
            # display filter before pagination, independent of catalogue ticks.
            if saved_catalogue.enabled(store) and not args['q'] and source not in ('local','subscriptions'):
                result=saved_catalogue.browse(app,store,args,source)
            elif source=='discover' and request.args.get('engine')=='1':
                from ..discovery_engine import cached_browse
                result=cached_browse(app,store,args)
            else:
                if source=='local' and request.args.get('refresh')=='1':
                    from .local import refresh
                    refresh(app.config['MEDIA_DIR'])
                result=adapter['browse'](app,store,args)
            return jsonify(source=source,media=args['media'],**result)
        except ValueError as exc:return jsonify(error=str(exc)),400
        except Exception:
            app.logger.warning('View Media source could not be loaded',exc_info=False)
            return jsonify(error='This source could not be loaded. Try again shortly.'),502

    @app.get('/api/youtube-discover/channel/<channel>/uploads')
    def youtube_public_uploads(channel):
        denied=access(local_action=True)
        if denied:return denied
        from ..discover import _limited
        if not _limited(app,'view_media_hits',60):return jsonify(error='Try again shortly.'),429
        from ..discover_sources import uploads
        try:return jsonify(uploads(channel))
        except ValueError:return jsonify(error='Public uploads are temporarily unavailable. Try again shortly.'),502

    @app.get('/api/youtube-discover')
    def youtube_discover():
        denied=access(local_action=True)
        if denied:return denied
        from ..discover import _limited
        if not _limited(app,'view_media_hits',60):return jsonify(error='Try again shortly.'),429
        from ..youtube_discover import browse
        try:
            return jsonify(browse(store,feed=request.args.get('feed','recommended'),category=request.args.get('category','all'),
                                  query=request.args.get('q','').strip()[:160],offset=int(request.args.get('offset','0'))))
        except ValueError as exc:return jsonify(error=str(exc)),400
        except Exception:return jsonify(error='YouTube discovery is temporarily unavailable.'),502

    @app.get('/api/view-media/download-sources')
    def view_media_download_sources():
        denied=access(local_action=True)
        if denied:return denied
        from ..discover import _limited
        if not _limited(app,'view_media_source_hits',60):return jsonify(error='Try again shortly.'),429
        from .download_sources import choices
        item={key:request.args.get(key,'')[:200] for key in ('id','kind','externalId','imdbId','title','year')}
        if item['kind'] not in ('movie','show'):return jsonify(error='Choose a movie or show.'),400
        return jsonify(choices(app,store,item))

    @app.get('/api/view-media/movie-info')
    def view_media_movie_info():
        denied=access(local_action=True)
        if denied:return denied
        from ..discover import _limited
        if not _limited(app,'view_media_details_hits',60):return jsonify(error='Try again shortly.'),429
        try:
            from .movie_info import details
            return jsonify(details(store,request.args.get('id','')[:200],request.args.get('imdb','')[:30]))
        except ValueError as exc:return jsonify(error=str(exc)),400
        except Exception:return jsonify(error='Extra movie information is temporarily unavailable.'),502

    @app.get('/api/view-media/show-info')
    def view_media_show_info():
        denied=access(local_action=True)
        if denied:return denied
        from ..discover import _limited
        if not _limited(app,'view_media_details_hits',60):return jsonify(error='Try again shortly.'),429
        try:
            from .show_info import details
            return jsonify(details(store,request.args.get('id','')[:200],request.args.get('tvdb','')[:30],request.args.get('imdb','')[:30]))
        except ValueError as exc:return jsonify(error=str(exc)),400
        except Exception:return jsonify(error='Extra TV show information is temporarily unavailable.'),502

    @app.get('/api/view-media/details')
    def view_media_details():
        denied=access(local_action=True)
        if denied:return denied
        from ..discover import _limited
        if not _limited(app,'view_media_details_hits',60):return jsonify(error='Try again shortly.'),429
        try:
            from .providers import details
            return jsonify(item=details(store,request.args.get('id','')))
        except ValueError as exc:return jsonify(error=str(exc)),400
        except Exception:return jsonify(error='Catalogue details are temporarily unavailable.'),502

    @app.get('/api/view-media/local/<identity>/parent')
    def view_media_local_parent(identity):
        denied=access(local_action=True)
        if denied:return denied
        from ..discover import _limited
        if not _limited(app,'view_media_hits',60):return jsonify(error='Too many media requests. Try again shortly.'),429
        from .local_parent import resolve
        try:result=resolve(app,store,identity)
        except Exception:
            app.logger.warning('Local parent could not be resolved',exc_info=False)
            return jsonify(error='Local media is unavailable.'),404
        if result is None:return jsonify(error='Local media is unavailable.'),404
        return jsonify(result)

    @app.get('/api/view-media/local/<identity>/<part>')
    def view_media_local_file(identity,part):
        denied=access(local_action=True)
        if denied:return denied
        if part not in ('art','backdrop'):return jsonify(error='Not found.'),404
        try:path=media_file(app.config['MEDIA_DIR'],identity,part)
        except ValueError:return jsonify(error='Local media is unavailable.'),404
        return send_file(path,conditional=True)
