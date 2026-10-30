"""Stable ordering with unknown metadata last in either direction."""
def ordered(items, order):
    if order == 'source': return list(items)
    field = {'title':'title','title_desc':'title','new':'added','oldest':'added','year':'year','year_asc':'year','rating':'rating','rating_asc':'rating','release':'releaseDate','release_asc':'releaseDate','episode_released':'lastEpisodeReleased','episode_released_asc':'lastEpisodeReleased','episode_added':'added','episode_added_asc':'added'}[order]
    reverse = order in ('title_desc','new','year','rating','release','episode_released','episode_added')
    def value(item):
        raw=item.get(field)
        if field=='title':return str(raw or '').casefold()
        if field in ('releaseDate','lastEpisodeReleased'):
            if field=='releaseDate' and not raw:raw=item.get('year')
            return str(raw) if raw else None
        try:return float(raw) if raw not in (None,'') else None
        except (TypeError,ValueError):return None
    known=[i for i in items if value(i) is not None]
    unknown=[i for i in items if value(i) is None]
    return sorted(known,key=lambda i:(value(i),str(i['id'])),reverse=reverse)+unknown
