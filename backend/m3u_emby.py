"""Opt-in creation and additive maintenance of dedicated Emby M3U libraries."""
import json
import time
from pathlib import PurePosixPath
from .emby import api, queue_refresh
from .activity import record


def settings(store):
    from .storage_layout import fixed
    if fixed(store):
        root=getattr(store,'emby_media_root','')
        return dict(auto_libraries=store.get('m3u_emby_auto')=='true',
                    emby_folders={k:root+suffix if root else '' for k,suffix in [('streams',''),('movies','/Movies'),('series','/TV Shows')]},
                    library_status=store.get('m3u_emby_status','Automatic library setup is off.'))
    youtube=store.get('emby_root').rstrip('/')
    root=str(PurePosixPath(youtube).parent) if youtube.endswith('/YouTube') else ''
    defaults={key:root+'/'+suffix if root else '' for key,suffix in [('streams','M3U'),('movies','Movies'),('series','TV Shows')]}
    return dict(auto_libraries=store.get('m3u_emby_auto')=='true',emby_folders=json.loads(store.get('m3u_emby_folders',json.dumps(defaults))),
                library_status=store.get('m3u_emby_status','Automatic library setup is off.'))


def validate_paths(paths):
    if not isinstance(paths,dict) or set(paths)!={'streams','movies','series'}:raise ValueError('Enter the three folders as Emby sees them.')
    for value in paths.values():
        if not isinstance(value,str) or not value.startswith('/') or value=='/' or '..' in value.split('/') or '\\' in value or any(ord(c)<32 for c in value):
            raise ValueError('Enter absolute folders as Emby sees them.')
    values=[PurePosixPath(p) for p in paths.values()]
    if any(a.is_relative_to(b) or b.is_relative_to(a) for i,a in enumerate(values) for b in values[i+1:]):raise ValueError('Use separate Emby folders for streams, movies and TV shows.')


def reconcile(store):
    config=settings(store)
    if not config['auto_libraries']:return
    if not store.get('emby_url') or not store.get('emby_key'):
        store.set('m3u_emby_status','Waiting for an Emby connection.');return
    with store.connect() as db:sources=db.execute("SELECT folder FROM m3u_sources WHERE state NOT IN ('running','queued')").fetchall()
    if not sources:
        store.set('m3u_emby_status','Waiting for an imported M3U playlist.');return
    from .storage_layout import fixed
    paths=config['emby_folders']
    if fixed(store):
        if not paths['streams']: raise ValueError('Set REELDOCK_EMBY_ROOT in Docker Compose.')
    else: validate_paths(paths)
    folders=api(store,'Library/VirtualFolders')
    ownership=json.loads(store.get('m3u_emby_owned','{}'));server=store.get('emby_url')
    owned=ownership.setdefault(server,{})
    changed=False
    for name,kind,key,sub,types in [('ReelDock Movies','movies','movies','Movie_VOD',['Movie']),('ReelDock TV Shows','tvshows','series','TV_VOD',['Series','Season','Episode'])]:
        wanted=[paths[key].rstrip('/')]
        if fixed(store): wanted.append(str(PurePosixPath(paths['streams'])/('VOD Movies' if key=='movies' else 'VOD TV Shows')))
        for source in ([] if fixed(store) else sources):
            folder=source['folder']
            if store.get('storage_base'):
                from .storage_layout import names as folder_names
                sub=folder_names(store)[key]
            if not folder or '/' in folder or '\\' in folder or folder in ('.','..'):raise ValueError('The playlist folder is invalid.')
            wanted.append(str(PurePosixPath(paths['streams'])/folder/sub))
        found=[f for f in folders if f.get('Name')==name]
        if len(found)>1:raise ValueError('More than one Emby library has the name '+name+'. Rename the duplicate first.')
        if found:
            library=found[0];identity=str(library.get('ItemId') or library.get('Id') or '')
            if library.get('CollectionType')!=kind:raise ValueError(name+' already exists with a different media type.')
            if owned.get(name)!=identity:
                # Adopt the exact local-metadata libraries created during initial setup only.
                options=library.get('LibraryOptions',{});local=options.get('TypeOptions',[])
                if not identity or set(library.get('Locations',[]))!=set(wanted) or not local or any(t.get('MetadataFetchers') or t.get('ImageFetchers') for t in local):
                    raise ValueError(name+' already exists with different settings. It has been left unchanged.')
                owned[name]=identity;store.set('m3u_emby_owned',json.dumps(ownership))
            for path in wanted:
                if path not in library.get('Locations',[]):
                    api(store,'Library/VirtualFolders/Paths','POST',body={'Id':identity,'Path':path,'PathInfo':{'Path':path},'RefreshLibrary':False});changed=True
        else:
            options=dict(ContentType=kind,PathInfos=[{'Path':p} for p in wanted],DisabledLocalMetadataReaders=[],MetadataSavers=[],SaveLocalMetadata=False,EnableRealtimeMonitor=False,IgnoreHiddenFiles=True,AutomaticRefreshIntervalDays=0,EnableChapterImageExtraction=False,ExtractChapterImagesDuringLibraryScan=False,EnableMultiVersionByMetadata=True,EnableAutomaticSeriesGrouping=True,TypeOptions=[dict(Type=t,MetadataFetchers=[],ImageFetchers=[]) for t in types])
            api(store,'Library/VirtualFolders','POST',body={'Name':name,'CollectionType':kind,'Paths':wanted,'RefreshLibrary':False,'LibraryOptions':options})
            created=next(f for f in api(store,'Library/VirtualFolders') if f.get('Name')==name)
            owned[name]=str(created.get('ItemId') or created.get('Id') or '')
            store.set('m3u_emby_owned',json.dumps(ownership));changed=True
    if changed:queue_refresh(store,'m3u-libraries')
    store.set('m3u_emby_status','ReelDock Movies and ReelDock TV Shows are connected. New playlist folders are added automatically.')


def tick(store):
    if store.get('m3u_emby_auto')!='true' or float(store.get('m3u_emby_check','0'))>time.time():return
    store.set('m3u_emby_check',str(time.time()+60))
    try:reconcile(store)
    except Exception as exc:
        message=str(exc) if isinstance(exc,ValueError) else 'Emby library setup could not finish. Check the Emby folder paths, connection and existing library names; retrying automatically.'
        store.set('m3u_emby_status',message)
        record(store,'m3u-emby-libraries','Emby M3U libraries','error',message)
    else:
        record(store,'m3u-emby-libraries','Emby M3U libraries','complete',store.get('m3u_emby_status'))
