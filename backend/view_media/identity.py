"""One catalogue card per verified identity; release choices stay server-side."""
import re

def valid_card(item):
    """Zero is an indexer placeholder, never an IMDb/TVDB identity."""
    action=item.get('action',{})
    if action.get('type') in ('local','vod','youtube'):return True
    raw=action.get('item',{}) if action.get('type')=='discover' else item
    external=str(raw.get('externalId') or '')
    match=re.fullmatch(r'(?:movie|show):(\d+)',str(raw.get('id') or ''))
    return not ((external.isdigit() and int(external)==0) or (match and int(match[1])==0))

def movie_identity(item):
    external=str(item.get('externalId') or '').removeprefix('tt')
    if item.get('kind')=='movie' and re.fullmatch(r'\d{1,12}',external):
        return 'movie:'+str(int(external))
    return str(item['id'])


def unique_cards(items):
    result={}
    for item in items:
        if not valid_card(item):continue
        key=item['id']
        old=result.get(key)
        if old:
            # Prefer verified metadata over indexer guesses while retaining NZB order.
            chosen=item if item.get('metadataVerified') and not old.get('metadataVerified') else old
            merged={**old,**{k:v for k,v in chosen.items() if v not in ('',None)}}
            merged['added']=max(old.get('added') or 0,item.get('added') or 0) or None
            merged['releaseCount']=old.get('releaseCount',0)+item.get('releaseCount',0)
            result[key]=merged
        else:result[key]=item
    return list(result.values())
