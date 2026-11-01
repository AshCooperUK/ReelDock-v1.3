"""Isolated yt-dlp metadata extraction. No media downloads or arbitrary URLs."""
import json
import sys
from yt_dlp import YoutubeDL

class Quiet:
    def debug(self,*args):pass
    def warning(self,*args):pass
    def error(self,message,*args):
        # Captured privately by the parent, which publishes fixed categories.
        sys.stderr.write('ERROR: '+str(message)[-16384:]+'\n')

def main():
    from .vpn.transport import install
    install()
    from .youtube_download import install_retry_after
    install_retry_after()
    request=json.load(sys.stdin)
    opts=dict(quiet=True,no_warnings=True,logger=Quiet(),skip_download=True,
              socket_timeout=15,retries=1,extractor_retries=1,noplaylist=request['mode']=='video',
              ignore_no_formats_error=True,cachedir=False,js_runtimes={'node':{}},extract_flat='in_playlist' if request['mode']!='video' else False,
              playliststart=request.get('start',1),playlistend=request.get('end',1))
    if request.get('cookie'):opts['cookiefile']=request['cookie']
    with YoutubeDL(opts) as ydl:
        data=ydl.extract_info(request['url'],download=False)
        fields=('id','title','channel','channel_id','uploader','description','duration','timestamp','release_timestamp',
                'upload_date','release_date','thumbnail','thumbnails','live_status','view_count','channel_follower_count','webpage_url','playlist_index')
        def pick(item):return {k:item[k] for k in fields if item and k in item}
        result=pick(data)
        if request['mode']!='video':result['entries']=[pick(x) for x in data.get('entries',[]) if x]
        json.dump(result,sys.stdout)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        sys.stderr.write('ERROR: '+str(exc)[-16384:]+'\n')
        sys.exit(1)
