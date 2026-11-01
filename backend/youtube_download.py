"""Scoped yt-dlp CLI adapter: carry Retry-After into the durable queue."""
import math
import sys
import time
from email.utils import parsedate_to_datetime


def retry_after(value, now=None):
    now=time.time() if now is None else now
    try:
        value=str(value).strip()
        delay=int(value) if value.isdigit() else parsedate_to_datetime(value).timestamp()-now
        return math.ceil(delay) if math.isfinite(delay) and delay>0 else 0
    except (ValueError,TypeError,OverflowError):
        return 0


def install_retry_after():
    """Stop this process before yt-dlp's internal retries ignore server advice.

    The only emitted header value is a validated duration. URLs, credentials,
    response bodies and other headers remain inside the private subprocess.
    """
    from yt_dlp import YoutubeDL
    from yt_dlp.networking.exceptions import HTTPError
    original=YoutubeDL.urlopen
    if getattr(original,'_reeldock_retry_after',False) is True:return
    def urlopen(self,request):
        try:return original(self,request)
        except HTTPError as exc:
            delay=retry_after(exc.response.headers.get('Retry-After',''))
            if delay:
                print(f'ReelDock:Retry-After: {delay}',file=sys.stderr,flush=True)
                print('ERROR: YouTube requested a delay before another request (HTTP 429).' if exc.status==429 else 'ERROR: YouTube requested a delay before another request.',file=sys.stderr,flush=True)
                raise SystemExit(75) from None
            raise
    urlopen._reeldock_retry_after=True
    YoutubeDL.urlopen=urlopen


def main():
    from .vpn.transport import install
    install()
    install_retry_after()
    import yt_dlp
    yt_dlp.main()


if __name__=='__main__':main()
