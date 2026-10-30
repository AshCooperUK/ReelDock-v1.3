"""Small owned clients for documented media-server HTTP APIs (no SDK dependency)."""
import json
from urllib.parse import quote
import requests


class Client:
    def __init__(self, player, config):
        self.player, self.config = player, config

    def request(self, path, method='GET', params=None, body=None):
        headers={'Accept':'application/json'}
        auth=None
        if self.player=='plex':
            headers.update({'X-Plex-Token':self.config['secret'], 'X-Plex-Product':'ReelDock', 'X-Plex-Client-Identifier':'reeldock-media-players'})
        elif self.player=='jellyfin':
            headers['X-Emby-Token']=self.config['secret']
        else:
            auth=(self.config.get('username',''),self.config['secret'])
        try:
            with requests.request(method,self.config['url']+'/'+path,headers=headers,auth=auth,
                                  params=params,json=body,timeout=(5,20),allow_redirects=False,stream=True) as response:
                if response.status_code>=300:
                    raise ValueError('The server rejected the request. Check the address, credentials and permissions.')
                data=bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data)>5_000_000:raise ValueError('The media server response exceeded its size limit.')
                return json.loads(data) if data else {}
        except (requests.RequestException,ValueError,TypeError) as exc:
            if isinstance(exc,ValueError) and str(exc).startswith(('The server','The media')):raise
            raise ValueError('Could not read the media server. Check its connection and credentials.') from None

    def rpc(self, method, params=None):
        data=self.request('jsonrpc','POST',body={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
        if not isinstance(data,dict) or data.get('id')!=1 or 'error' in data or 'result' not in data:
            raise ValueError('Kodi rejected the request. Check JSON-RPC permissions and server version.')
        return data['result']

    def connect(self):
        if self.player=='jellyfin':
            info=self.request('System/Info')
            users=self.request('Users')
            if not isinstance(info,dict) or not info.get('Id') or not isinstance(users,list):raise ValueError('Invalid Jellyfin response.')
            return {'name':str(info.get('ServerName') or 'Jellyfin')[:150],
                    'users':[{'id':str(u['Id']),'name':str(u.get('Name','User'))[:150]} for u in users if isinstance(u,dict) and u.get('Id')]}
        if self.player=='plex':
            response=self.request('library/sections')
            if not isinstance(response,dict) or not isinstance(response.get('MediaContainer'),dict):raise ValueError('Invalid Plex library response.')
            libraries=response['MediaContainer'].get('Directory') or []
            if not isinstance(libraries,list):raise ValueError('Invalid Plex library response.')
            return {'name':'Plex','libraries':[{'id':str(x['key']),'name':str(x.get('title','Library'))[:150], 'type':x.get('type')} for x in libraries if x.get('type') in ('movie','show')]}
        info=self.rpc('Application.GetProperties',{'properties':['name','version']})
        if not isinstance(info,dict) or not info.get('name'):raise ValueError('Invalid Kodi response.')
        return {'name':str(info['name'])[:150]}

    def scan(self, checkpoint=lambda:None):
        checkpoint()
        if self.player=='jellyfin':self.request('Library/Refresh','POST')
        elif self.player=='kodi':
            if self.rpc('VideoLibrary.Scan')!='OK':raise ValueError('Kodi did not accept the library scan.')
        else:
            libraries=self.connect()['libraries']
            selected=self.config.get('library','')
            if selected: libraries=[x for x in libraries if x['id']==selected]
            if not libraries:raise ValueError('No accessible movie or TV libraries to scan.')
            for library in libraries:
                checkpoint()
                self.request('library/sections/'+quote(library['id'],safe='')+'/refresh')

    def items(self, checkpoint=lambda:None):
        """Yield normalized movie/episode state, with bounded pagination."""
        if self.player=='jellyfin':
            user=self.config.get('user','')
            if not user:raise ValueError('Choose a Jellyfin user before synchronising.')
            for start in range(0,100000,200):
                checkpoint()
                data=self.request('Users/'+quote(user,safe='')+'/Items',params={'Recursive':'true','IncludeItemTypes':'Movie,Episode,Video','EnableUserData':'true','Fields':'ProviderIds,Path','StartIndex':start,'Limit':200})
                if not isinstance(data,dict) or type(data.get('TotalRecordCount')) is not int or not isinstance(data.get('Items'),list):
                    raise ValueError('Invalid Jellyfin library response.')
                rows=data['Items']
                for item in rows:
                    state=item.get('UserData') or {}
                    yield {'id':str(item['Id']),'title':str(item.get('Name',''))[:300], 'watched':bool(state.get('Played')), 'favourite':bool(state.get('IsFavorite')), 'provider_ids':item.get('ProviderIds') or {}, 'paths':[item.get('Path','')]}
                if not rows or start+len(rows)>=data.get('TotalRecordCount',start+len(rows)):return
        elif self.player=='plex':
            libraries=self.connect()['libraries']
            selected=self.config.get('library','')
            if selected:
                libraries=[x for x in libraries if x['id']==selected]
                if not libraries:raise ValueError('The selected Plex library is no longer accessible.')
            for library in libraries:
                for start in range(0,100000,200):
                    checkpoint()
                    data=self.request('library/sections/'+quote(library['id'],safe='')+'/all',params={'type':4 if library['type']=='show' else 1,'includeGuids':1,'X-Plex-Container-Start':start,'X-Plex-Container-Size':200}).get('MediaContainer',{})
                    if not isinstance(data,dict) or not any(type(data.get(key)) is int for key in ('totalSize','size')) or not isinstance(data.get('Metadata',[]),list):
                        raise ValueError('Invalid Plex library response.')
                    rows=data.get('Metadata') or []
                    for item in rows:
                        yield {'id':str(item['ratingKey']),'title':str(item.get('title',''))[:300], 'watched':bool(item.get('viewCount',0)), 'favourite':None, 'provider_ids':{'youtube':str(g.get('id',''))[10:] for g in item.get('Guid',[]) if str(g.get('id','')).startswith('youtube://')}, 'paths':[p.get('file','') for m in item.get('Media',[]) for p in m.get('Part',[])]}
                    if not rows or start+len(rows)>=data.get('totalSize',start+len(rows)):break
                else:raise ValueError('Plex library exceeded the 100,000 item scan limit.')
            return
        else:
            for kind in ('Movies','Episodes'):
                key=kind.lower()
                for start in range(0,100000,200):
                    checkpoint()
                    data=self.rpc('VideoLibrary.Get'+kind,{'properties':['title','playcount','file','uniqueid'], 'limits':{'start':start,'end':start+200},'sort':{'method':'title','order':'ascending'}})
                    if not isinstance(data,dict) or type(data.get('limits',{}).get('total')) is not int or not isinstance(data.get(key,[]),list):
                        raise ValueError('Invalid Kodi library response.')
                    rows=data.get(key,[])
                    for item in rows:
                        identity=str(item['movieid' if kind=='Movies' else 'episodeid'])
                        yield {'id':key+':'+identity,'title':str(item.get('title') or item.get('label',''))[:300], 'watched':bool(item.get('playcount',0)), 'favourite':None, 'provider_ids':item.get('uniqueid') or {}, 'paths':[item.get('file','')]}
                    if not rows or start+len(rows)>=data.get('limits',{}).get('total',start+len(rows)):break
                else:raise ValueError('Kodi library exceeded the 100,000 item scan limit.')
            return
        raise ValueError('Jellyfin library exceeded the 100,000 item scan limit.')
