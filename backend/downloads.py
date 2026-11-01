"""Persistent, single-worker manual downloads. Only completed files are published to the library."""
import json
import logging
import math
import os
import re
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from contextlib import nullcontext

from . import media_library as library
from .vpn.transport import youtube, subprocess_env


class DownloadPaused(Exception):
    """Automatic subscription work waits without losing its queue entry."""


def subscription_paused(store,job):
    if not job.get('automatic'):return False
    with store.connect() as db:
        return bool(db.execute("SELECT 1 FROM videos v JOIN download_rules r ON r.channel=v.channel WHERE v.id=? AND r.subscription_paused=1",(job['id'],)).fetchone())


class DownloadSkipped(ValueError):
    pass


class YouTubeFailure(ValueError):
    """A public error plus a private, validated provider retry deadline."""
    def __init__(self, message, retry_at=0):
        super().__init__(message)
        self.retry_at = retry_at


MAX_ATTEMPTS = 6  # The initial attempt plus five automatic retries.
RETRY_DELAYS = (30, 60, 120, 300, 600)


def provider_retry_at(output, now=None):
    """Accept only Retry-After values, never store the surrounding diagnostics."""
    now = time.time() if now is None else now
    deadline = 0
    for value in re.findall(r'(?im)^\s*(?:ReelDock:)?Retry-After\s*[:=]\s*([^\r\n]+)', output):
        try:
            value = value.strip()
            candidate = now + int(value) if value.isdigit() else parsedate_to_datetime(value).timestamp()
            if math.isfinite(candidate) and candidate > now:
                deadline = max(deadline, candidate)
        except (ValueError, TypeError, OverflowError):
            pass
    return deadline


QUALITIES = ('480', '720', '1080', '2160')
# Exclude format/fragment lists: these can exceed several MB for a single video.
TYPE_PROBE_TEMPLATE = '%(.{width,height,aspect_ratio,duration,is_live,was_live,live_status})j'
MANUAL_LIVE_FILTER = 'live_status !=? is_live & live_status !=? is_upcoming'
MANUAL_DETAILS_FILE = 'video.details.json'
MANUAL_DETAILS_LIMIT = 16_384
# Full extractor JSON includes stream fragments and captions and can exceed
# several MB. Verify only the actual completed download's required scalar facts.
MANUAL_DETAILS_TEMPLATE = 'after_move:%(.{id,duration,width,height,aspect_ratio,is_live,is_upcoming,was_live,live_status})j'


def cancelled_in(db,video_id):
    """Inspect the durable selected-video fence in the caller's transaction."""
    from .media_holds import blocked_in
    row=db.execute('SELECT state FROM downloads WHERE id=?',(video_id,)).fetchone()
    return bool(row and row[0] in ('cancelling','cancelled') or
                blocked_in(db,['video:'+video_id,'managed:video:'+video_id]))


def check_attempt(store,job,stop):
    """A stopped/cancelled request never starts another fallback process."""
    if stop.is_set():raise InterruptedError()
    with store.connect() as db:
        if cancelled_in(db,job['id']):raise ValueError('Cancelled.')
    from .acquisition import held
    from .download_control import paused
    if subscription_paused(store,job):raise DownloadPaused('Subscription paused. Download will restart when resumed.')
    if held(store) or paused(store, 'youtube'):raise DownloadPaused('Downloads paused. This video will restart when resumed.')
    from .vpn import downloads_blocked
    if downloads_blocked('youtube'):raise DownloadPaused('Waiting for the current YouTube VPN connection.')


def failure_category(output):
    """Fixed categories only: extractor diagnostics may contain private URLs."""
    # yt-dlp can warn that the signed-in cookies rotated, then report the
    # video's availability as an ERROR. Keep the explicit expiry evidence.
    if any(term in output.lower() for term in ('cookies have expired','cookies are no longer valid')):return 'expired'
    errors=[line for line in output.splitlines() if 'ERROR:' in line]
    text=('\n'.join(errors) if errors else output).lower()
    for category,needles in (
        ('geo',('not available in your country','not made this video available in your country','not available in your region','blocked in your country','geo-restricted','georestricted','geo restricted')),
        ('availability',('private video','video is private','video unavailable','video is unavailable','video is not available','video has been removed','members-only','members only','not available in your country')),
        ('verification',('not a bot','login required','sign in','sign-in','authentication required','age-restricted','age restricted','consent.youtube','consent.google')),
        ('format',('requested format','no video formats','no formats found','only images are available')),
        ('rate',('http error 429','too many requests')),
        ('network',('timed out','timeout','name resolution','getaddrinfo','certificate verify failed','certificate_verify_failed')),
        ('forbidden',('http error 403','forbidden')),
    ):
        if any(needle in text for needle in needles):return category
    return 'other'


def failed_download(store,attempts):
    authenticated=[text for authenticated,text in attempts if authenticated]
    # An anonymous verification error cannot declare a working cookie invalid.
    for text in authenticated:
        if failure_category(text)=='expired':cookie_failure(store,text)
    preferred=next((text for text in authenticated if failure_category(text)=='geo'),None)
    if preferred is None:preferred=next((text for text in authenticated if failure_category(text) in ('availability','verification','forbidden')),None)
    category=failure_category(preferred if preferred is not None else attempts[-1][1] if attempts else '')
    messages={
        'geo':'YouTube reports a country/region restriction for this video on ReelDock’s current connection. Check the VPN exit location.',
        'availability':'YouTube reports this video is unavailable, removed, restricted or inaccessible to the current account.',
        'verification':'YouTube requires a sign-in or verification check for this video. Check video access with the selected account.',
        'format':'YouTube did not offer a playable video/audio format for this selection.',
        'rate':'YouTube is limiting requests. Wait before retrying this video.',
        'network':'Could not connect to YouTube reliably. Check network access before retrying.',
        'forbidden':'YouTube rejected this request (HTTP 403). Check video access and the current network.',
    }
    raise YouTubeFailure(messages.get(category,'YouTube download failed. Check video availability and saved cookies, then retry.'),
                         max((provider_retry_at(output) for _,output in attempts),default=0))


def manual_details(store,job,folder):
    """Inspect only the actual download's private, bounded, identity-bound JSON."""
    path=folder/MANUAL_DETAILS_FILE
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(descriptor,'rb') as source:
            state=os.fstat(source.fileno())
            if not stat.S_ISREG(state.st_mode) or state.st_size>MANUAL_DETAILS_LIMIT:raise ValueError()
            raw=source.read(MANUAL_DETAILS_LIMIT+1)
        if len(raw)>MANUAL_DETAILS_LIMIT:raise ValueError()
        info=json.loads(raw)
        if not isinstance(info,dict) or info.get('id')!=job['id']:raise ValueError()
        for key in ('duration','width','height','aspect_ratio'):
            value=info.get(key)
            if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value<0):raise ValueError()
    except (OSError,TypeError,ValueError):
        raise ValueError('The downloaded video details could not be verified. Nothing was saved.') from None
    if info.get('is_live') or info.get('is_upcoming') or info.get('live_status') in ('is_live','is_upcoming'):
        raise DownloadSkipped('Waiting for the live broadcast to finish.')
    duration=info.get('duration') or 0
    with store.connect() as db:
        row=db.execute('SELECT data FROM videos WHERE id=?',(job['id'],)).fetchone()
        if row:
            video=json.loads(row[0])
            try:video['kind']=classify_video(info,video.get('date',''))
            except DownloadSkipped:pass  # Explicit manual requests need no automation classification.
            if duration:video['duration_seconds']=duration
            db.execute('UPDATE videos SET data=? WHERE id=?',(json.dumps(video),job['id']))
    return duration


def initialise(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS downloads (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, quality TEXT NOT NULL,
            state TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
            updated REAL NOT NULL)''')
        if 'phase' not in {row[1] for row in db.execute('PRAGMA table_info(downloads)')}:
            db.execute("ALTER TABLE downloads ADD COLUMN phase TEXT NOT NULL DEFAULT ''")
        if 'processing_info' not in {row[1] for row in db.execute('PRAGMA table_info(downloads)')}:
            db.execute("ALTER TABLE downloads ADD COLUMN processing_info TEXT NOT NULL DEFAULT ''")
        columns = {row[1] for row in db.execute('PRAGMA table_info(downloads)')}
        for name, declaration in (
            ('attempt_count', 'INTEGER NOT NULL DEFAULT 0'),
            ('retry_at', 'REAL NOT NULL DEFAULT 0'),
            ('retry_error', "TEXT NOT NULL DEFAULT ''"),
            ('quality_inherited', 'INTEGER NOT NULL DEFAULT 0'),
        ):
            if name not in columns:db.execute(f'ALTER TABLE downloads ADD COLUMN {name} {declaration}')
        # Safe on existing databases: IF NOT EXISTS builds each index once.
        # state/updated serves the Activity feed and failure alerts;
        # created/id serves Local Library paging.
        db.execute('CREATE INDEX IF NOT EXISTS downloads_state_updated ON downloads(state,updated)')
        db.execute('CREATE INDEX IF NOT EXISTS downloads_created ON downloads(created,id)')


def enqueue(store, video, quality, automatic=False, *, transaction=None, quality_inherited=False, restart=False):
    from .acquisition import ensure_open
    ensure_open(store)
    from .media_management.store import policy_blocks
    if quality not in QUALITIES or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video['id']):
        raise ValueError('Choose a supported download quality.')
    if video.get('state') != 'published':
        raise ValueError('Downloads are available for published videos only.')
    with (store.connect() if transaction is None else nullcontext(transaction)) as db:
        if transaction is None:db.execute('BEGIN IMMEDIATE')
        from .media_holds import blocked_in
        if blocked_in(db,['video:'+video['id'],'managed:video:'+video['id']]):
            if automatic:return
            raise ValueError('This video was cleared. Select Download or Retry explicitly to resume it.')
        if automatic and policy_blocks(store,'video:'+video['id']):return
        previous=db.execute('SELECT attempt_count FROM downloads WHERE id=?',(video['id'],)).fetchone()
        if automatic and not restart and previous and previous['attempt_count']>=MAX_ATTEMPTS:return
        db.execute('''INSERT INTO downloads(id,title,quality,state,created,updated,automatic,quality_inherited)
            VALUES (?,?,?,'queued',?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            quality=excluded.quality,automatic=excluded.automatic,quality_inherited=excluded.quality_inherited,
            state='queued',progress=0,message='',phase='',attempt_count=0,retry_at=0,retry_error='',updated=excluded.updated
            WHERE downloads.state IN ('failed','cancelled','complete','skipped','expired')''',
            (video['id'], video['title'], quality, time.time(), time.time(), int(automatic),
             int(bool(quality_inherited))))
        if not automatic and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discover_requests'").fetchone():
            db.execute('''UPDATE discover_requests SET state='queued',progress=0,message='Queued with yt-dlp.',updated=?
                WHERE kind='video' AND state IN ('failed','cancelled') AND json_extract(body,'$.videoId')=?
                AND EXISTS(SELECT 1 FROM downloads WHERE id=? AND state IN ('queued','downloading'))''',(time.time(),video['id'],video['id']))
        if not automatic:
            # Asking for a video that is already queued automatically makes it a manual
            # download, so later channel-rule changes no longer remove it.
            db.execute("UPDATE downloads SET automatic=0 WHERE id=? AND state='queued'", (video['id'],))


def update(store, video_id, state, progress=0, message=''):
    with store.connect() as db:
        db.execute('UPDATE downloads SET state=?,progress=?,message=?,updated=? WHERE id=?',
                   (state, progress, message, time.time(), video_id))


def command(video_id, quality, folder, cookie=None):
    # Prefer H.264/AAC for broad media-server compatibility; fall back to another MP4 if needed.
    fmt = (f'bv[ext=mp4][vcodec^=avc1][height<={quality}]+ba[ext=m4a]/'
           f'b[ext=mp4][height<={quality}]/bv[ext=mp4][height<={quality}]+ba[ext=m4a]')
    args = [sys.executable, '-m', 'backend.youtube_download', '--ignore-config', '--no-playlist',
            '--no-colors', '--newline', '--progress', '--progress-delta', '1',
            '--progress-template', 'download:ReelDock:%(progress.downloaded_bytes)s:%(progress.total_bytes,progress.total_bytes_estimate)s',
            '--socket-timeout', '20', '--retries', '3', '--fragment-retries', '3',
            '--js-runtimes', 'node', '--merge-output-format', 'mp4', '-f', fmt,
            '-o', str(folder / 'video.%(ext)s')]
    if cookie:
        args += ['--cookies', str(cookie)]
    return args + ['--', 'https://www.youtube.com/watch?v=' + video_id]


def classify_video(info, published):
    if info.get('is_live') or info.get('live_status') in ('is_live','is_upcoming'):
        raise DownloadSkipped('Waiting for the live broadcast to finish.')
    if info.get('was_live') or info.get('live_status')=='was_live':return 'live'
    duration=info.get('duration') or 0
    width=info.get('width') or 0; height=info.get('height') or 0
    ratio=info.get('aspect_ratio') or (width/height if width and height else 0)
    if not duration or not ratio:
        raise DownloadSkipped("Could not identify this video type, so this channel's download settings could not be checked.")
    threshold=180 if published>='2024-10-15' else 60
    return 'shorts' if ratio<=1 and duration<=threshold else 'videos'


def cookie_failure(store, output):
    lowered=output.lower()
    if any(term in lowered for term in ('cookies have expired','cookies are no longer valid','login required','sign in','sign-in','authentication required','consent.youtube','consent.google')):
        from .cookie_recovery import record, DOWNLOAD_MESSAGE
        record(store,'auth_expired' if any(term in lowered for term in ('cookies have expired','cookies are no longer valid')) else 'auth_unverified')
        raise ValueError(DOWNLOAD_MESSAGE)


@youtube
def run_download(store, media, job, stop):
    check_attempt(store,job,stop)
    from .download_options import for_channel
    with store.connect() as db:
        row=db.execute('SELECT channel FROM videos WHERE id=?',(job['id'],)).fetchone()
        if not row:row=db.execute('SELECT channel FROM media_files WHERE id=?',(job['id'],)).fetchone()
    options=for_channel(store,row['channel'] if row else '', 'youtube')
    root = Path(media).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < 1024**3:
        raise ValueError('Less than 1 GB free in the media folder. Free space, then retry.')
    # A private temporary directory isolates partial media, logs and cookie snapshots.
    # Publishing uses a rename on the same filesystem, so partial files cannot play.
    with tempfile.TemporaryDirectory(prefix='.reeldock-download-', dir=root) as directory:
        folder = Path(directory)
        cookie = store.directory / 'youtube-cookies.txt'
        snapshot = None
        from .watched_status import cookie_session_file_status
        from .cookies import allow_downloads
        if allow_downloads(store) and options['authenticated_retry'] and cookie_session_file_status(cookie)[0] == 'unchecked':
            snapshot = folder / 'cookies.txt'
            snapshot.write_bytes(cookie.read_bytes())
            os.chmod(snapshot, 0o600)
        automatic=bool(job.get('automatic'))
        from .channel_downloads import preferences, rules_apply
        with store.connect() as db:
            saved=db.execute('SELECT data FROM videos WHERE id=?',(job['id'],)).fetchone()
        video=json.loads(saved[0]) if saved else {}
        rule=preferences(store,video.get('channel',''))
        # Automatic channel selection is rechecked before transfer. An explicit
        # published-video selection keeps its own manual download intent.
        check_rules(store,job,automatic,video,rule,None,'start')
        duration=None
        # Probe type and length when a channel setting could depend on them.
        from .download_options import read as read_options
        # A selected manual download uses yt-dlp's actual extraction rather than
        # requiring a separate automation type/length inspection to succeed.
        needs_probe=automatic
        if needs_probe and rules_apply(rule):
            args=[sys.executable,'-m','backend.youtube_download','--ignore-config','--no-playlist','--js-runtimes','node','--socket-timeout','20','--retries','1','--skip-download','--print',TYPE_PROBE_TEMPLATE]
            if snapshot and options['authenticated_retry']:args+=['--cookies',str(snapshot)]
            args+=['--','https://www.youtube.com/watch?v='+job['id']]
            with (folder/'probe.json').open('w+b') as probe, (folder/'probe-error.log').open('w+b') as errors:
                attempts=[]
                check_attempt(store,job,stop)
                try:
                    result=subprocess.run(args,stdout=probe,stderr=errors,timeout=120,env=subprocess_env('youtube'))
                except subprocess.TimeoutExpired:
                    errors.write(b'\nERROR: YouTube metadata connection timed out.\n')
                    result=subprocess.CompletedProcess(args,1)
                check_attempt(store,job,stop)
                if result.returncode and '--cookies' in args:
                    errors.seek(0,2);errors.seek(max(0,errors.tell()-32768))
                    attempts.append((True,errors.read().decode('utf-8','replace')))
                    if provider_retry_at(attempts[-1][1]):failed_download(store,attempts)
                    index=args.index('--cookies');del args[index:index+2]
                    snapshot=None
                    probe.seek(0);probe.truncate();errors.seek(0);errors.truncate()
                    check_attempt(store,job,stop)
                    try:
                        result=subprocess.run(args,stdout=probe,stderr=errors,timeout=120,env=subprocess_env('youtube'))
                    except subprocess.TimeoutExpired:
                        errors.write(b'\nERROR: YouTube metadata connection timed out.\n')
                        result=subprocess.CompletedProcess(args,1)
                    check_attempt(store,job,stop)
                if result.returncode:
                    errors.seek(0,2);errors.seek(max(0,errors.tell()-32768))
                    attempts.append(('--cookies' in args,errors.read().decode('utf-8','replace')))
                    failed_download(store,attempts)
                probe.seek(0); raw=probe.read(5_000_001)
            if len(raw)>5_000_000:raise ValueError('Video metadata exceeded the inspection limit.')
            info=json.loads(raw)
            kind=classify_video(info,video.get('date',''))
            video['kind']=kind
            duration=info.get('duration') or 0
            with store.connect() as db:db.execute('UPDATE videos SET data=? WHERE id=?',(json.dumps(video),job['id']))
            check_rules(store,job,automatic,video,rule,duration,'start')
        check_attempt(store,job,stop)
        record=library.reserve(store,media,job['id'])
        args=command(job['id'],job['quality'],folder,snapshot)
        if not automatic:
            args[args.index('--'):args.index('--')]=[
                '--no-simulate','--print-to-file',MANUAL_DETAILS_TEMPLATE,
                str(folder/MANUAL_DETAILS_FILE).replace('%','%%'),
                '--match-filter',MANUAL_LIVE_FILTER]
        if options['hardware_encoding'] and options['sponsorblock']=='remove':args[args.index('backend.youtube_download')]='backend.ytdlp_runner'
        if options['sponsorblock']!='off':
            index=args.index('--')
            args[index:index]=['--sponsorblock-mark' if options['sponsorblock']=='mark' else '--sponsorblock-remove','sponsor','--embed-chapters']
            if options['sponsorblock']=='remove':
                # Stream-copy cuts can start between video keyframes and leave
                # playback out of sync until the player seeks. Re-encode at cuts.
                args[args.index('--'):args.index('--')]=['--force-keyframes-at-cuts']
        fallback=False;attempts=[];original_format=args[args.index('-f')+1]
        tried=set()
        while True:
            check_attempt(store,job,stop)
            mode=('--cookies' in args,fallback)
            if mode in tried:failed_download(store,attempts)
            tried.add(mode)
            # yt-dlp appends print-to-file output. Each attempt must prove its
            # own identity; never reuse or append facts from a failed attempt.
            if not automatic:(folder/MANUAL_DETAILS_FILE).unlink(missing_ok=True)
            code,output=run_process(store,job,stop,args,folder/'progress.log',root)
            check_attempt(store,job,stop)
            if code==0:break
            lowered=output.lower()
            attempts.append(('--cookies' in args,output))
            if provider_retry_at(output):failed_download(store,attempts)
            if not fallback and ((not automatic) or options['convert_fallback'] and failure_category(lowered)=='format'):
                args[args.index('-f')+1]=f'bv[height<={job["quality"]}]+ba/b[height<={job["quality"]}]/bv+ba/b'
                fallback=True
                continue
            if '--cookies' in args:
                index=args.index('--cookies');del args[index:index+2]
                if not automatic:
                    fallback=False;args[args.index('-f')+1]=original_format
                continue
            failed_download(store,attempts)
        if not automatic:duration=manual_details(store,job,folder)
        output=folder/'video.mp4'
        if fallback or options['force_transcode']:
            choices=[path for path in folder.glob('video.*') if path.suffix in ('.mp4','.webm','.mkv','.mov')]
            if not choices:raise ValueError('No video was produced for conversion.')
            update_progress(store,job['id'],'Converting video with FFmpeg. This can take several minutes.','encoding')
            converted=folder/'converted.mp4'
            cpu_args=['ffmpeg','-nostdin','-y','-i',str(choices[0]),'-map','0:v:0','-map','0:a?',
                '-map_metadata','0','-map_chapters','0','-vf',f'scale=-2:trunc(min(ih\\,{job["quality"]})/2)*2',
                '-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(converted)]
            from .hardware_encoding import available_device
            device=available_device() if options['hardware_encoding'] else None
            if device:
                gpu_args=cpu_args[:]
                gpu_args[1:1]=['-vaapi_device',device]
                gpu_args[gpu_args.index('-vf')+1]+=',format=nv12,hwupload'
                start=gpu_args.index('-c:v');end=gpu_args.index('-c:a')
                gpu_args[start:end]=['-c:v','h264_vaapi','-qp','22']
                update_progress(store,job['id'],'Converting video using VAAPI hardware encoding.','encoding')
                check_attempt(store,job,stop)
                code,_=run_process(store,job,stop,gpu_args,folder/'convert.log',root)
                check_attempt(store,job,stop)
                if code:update_progress(store,job['id'],'Hardware conversion failed; retrying with CPU encoding.','encoding')
            else:
                code=1
                update_progress(store,job['id'],'Converting video using CPU encoding. Hardware encoding is disabled or unavailable.','encoding')
            if code:
                check_attempt(store,job,stop)
                code,_=run_process(store,job,stop,cpu_args,folder/'convert.log',root)
                check_attempt(store,job,stop)
            if code:raise ValueError('Video conversion failed. The existing download has been kept.')
            output=converted
        if not output.is_file() or output.stat().st_size==0:raise ValueError('No playable MP4 was produced.')
        # Settings may have changed during the transfer: recheck before saving,
        # so an ineligible download never reaches, or replaces, a saved file.
        recheck_before_save(store,job,automatic,duration)
        check_attempt(store,job,stop)
        target=library.contained(media,record['path']);target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and not job.get('replace_existing'):raise ValueError('A local file already exists for this video.')
        # Clear commits its fence under the same SQLite writer lock. A late
        # transfer must not rename over the saved file after that acknowledgement.
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if stop.is_set():raise InterruptedError()
            if cancelled_in(db,job['id']):raise ValueError('Cancelled.')
            os.replace(output,target)
        check_attempt(store,job,stop)
        update_progress(store,job['id'],'Saving episode metadata and channel artwork.','metadata')
        return library.write_metadata(store,media,record)


def check_rules(store, job, automatic, video, rule, duration, stage):
    """Raise DownloadSkipped when the channel settings exclude this download.

    stage is 'start' (before any transfer) or 'save' (before the finished file
    is published). duration is None when no type probe ran.
    """
    from .channel_downloads import eligible, exclusion, rule_conflict, rules_apply, length_conflict, request_rule
    from .download_options import read
    if automatic and rule.get('subscription_paused'):raise DownloadPaused('Subscription paused. Download will restart when resumed.')
    rule=request_rule(store,video,rule,automatic)
    prefix='Not downloaded' if stage=='start' else 'Not saved'
    if not automatic:
        # Explicit Download is for this published video, independent of the
        # channel's automatic type, minimum-length and date-range selection.
        if video.get('state')!='published':raise DownloadSkipped(f'{prefix}: downloads are available for published videos only.')
        return
    if automatic and not eligible(video,rule):
        reason=exclusion(video,rule,True) or 'auto download settings no longer include this video'
        raise DownloadSkipped(f'{prefix}: {reason}.')
    reason=rule_conflict(video,rule)
    if reason:raise DownloadSkipped(f'{prefix}: {reason}.')
    if duration is not None and (automatic or rules_apply(rule)):
        reason=length_conflict(video.get('kind','unknown'),duration,rule.get('minimum_video_minutes',read(store, 'youtube')['minimum_video_minutes']))
        if reason:raise DownloadSkipped(f'{prefix}: {reason}.')


def recheck_before_save(store, job, automatic, duration):
    from .channel_downloads import preferences
    with store.connect() as db:
        state=db.execute('SELECT state FROM downloads WHERE id=?',(job['id'],)).fetchone()
        saved=db.execute('SELECT data FROM videos WHERE id=?',(job['id'],)).fetchone()
    if state and state[0]=='cancelling':raise ValueError('Cancelled.')
    video=json.loads(saved[0]) if saved else {}
    check_rules(store,job,automatic,video,preferences(store,video.get('channel','')),duration,'save')


def update_progress(store,video_id,message,phase='processing'):
    with store.connect() as db:
        db.execute("UPDATE downloads SET message=CASE WHEN attempt_count>1 THEN 'Automatic retry '||(attempt_count-1)||' of 5. '||? ELSE ? END,phase=?,updated=? WHERE id=? AND state='downloading'",(message,message,phase,time.time(),video_id))


def process_line(store,video_id,line):
    # Only publish our own descriptions, never raw logs containing URLs/cookies.
    if line.startswith('ReelDock:'):
        try:
            _,done,total=line.split(':');percent=max(0,min(99,float(done)/float(total)*100))
        except (ValueError,ZeroDivisionError):return
        with store.connect() as db:
            db.execute("UPDATE downloads SET progress=?,phase='downloading',message=CASE WHEN attempt_count>1 THEN 'Automatic retry '||(attempt_count-1)||' of 5. Downloading video and audio from YouTube.' ELSE 'Downloading video and audio from YouTube.' END,updated=? WHERE id=? AND state='downloading'",(percent,time.time(),video_id))
        return
    stages=(
        ('ReelDock GPU:', 'encoding', 'Encoding sponsor cuts using VAAPI hardware acceleration.'),
        ('ReelDock CPU:', 'encoding', 'Using CPU encoding: hardware encoding is unavailable or failed.'),
        ('Re-encoding ', 'encoding', 'Encoding video with FFmpeg for accurate sponsor cuts. This can take several minutes.'),
        ('[Merger]', 'merging', 'Merging video and audio with FFmpeg.'),
        ('Removing chapters from ', 'sponsors', 'Removing sponsor sections with FFmpeg.'),
        ('[VideoConvertor]', 'encoding', 'Converting video with FFmpeg.'),
        ('[VideoRemuxer]', 'merging', 'Repackaging video and audio with FFmpeg.'),
        ('[Metadata]', 'metadata', 'Embedding metadata and chapter markers.'),
        ('[Fixup', 'processing', 'Repairing media timestamps and container with FFmpeg.'),
    )
    for marker,phase,message in stages:
        if marker in line:
            update_progress(store,video_id,message,phase)
            return


def ffmpeg_progress(path):
    """Read the last complete machine-readable FFmpeg progress report."""
    try:
        with path.open('rb') as reader:
            reader.seek(max(0,path.stat().st_size-8192))
            raw=reader.read(8192).decode('utf-8',errors='replace')
    except OSError:return None
    blocks=re.split(r'progress=(?:continue|end)\r?\n',raw)
    if len(blocks)<2:return None
    values=dict(line.split('=',1) for line in blocks[-2].splitlines() if '=' in line)
    timestamp=values.get('out_time','')
    if not re.fullmatch(r'\d{2,}:\d{2}:\d{2}(?:\.\d+)?',timestamp):return None
    speed=values.get('speed','').strip()
    if not re.fullmatch(r'\d+(?:\.\d+)?x',speed):speed='not available yet'
    return timestamp.split('.')[0],speed,(timestamp,values.get('frame'),values.get('total_size'))


@youtube
def run_process(store,job,stop,args,log_path,root):
    check_attempt(store,job,stop)
    args=list(args)
    progress_path=log_path.with_suffix('.ffmpeg-progress')
    progress_path.unlink(missing_ok=True)
    progress_args=['-progress',str(progress_path),'-stats_period','2']
    if '-m' in args and any(module in args for module in ('yt_dlp','backend.youtube_download','backend.ytdlp_runner')):
        args[args.index('--'):args.index('--')]=['--postprocessor-args','ffmpeg:'+shlex.join(progress_args)]
    elif Path(args[0]).stem=='ffmpeg':args[1:1]=progress_args
    if '-m' in args and any(module in args for module in ('yt_dlp','backend.youtube_download','backend.ytdlp_runner')):
        update_progress(store,job['id'],'Checking video availability and selecting streams.','preparing')
    with log_path.open('w+',encoding='utf-8') as log:
        proc=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT,start_new_session=os.name!='nt',env=subprocess_env('youtube'))
        start=time.monotonic();position=0;pending='';last_report=None;last_advance=start;last_info='';suspended=False;pause_started=0
        from .download_control import paused
        try:
            while proc.poll() is None:
                if stop.wait(1):raise InterruptedError()
                with store.connect() as db:
                    if cancelled_in(db,job['id']):raise ValueError('Cancelled.')
                if subscription_paused(store,job):raise DownloadPaused('Subscription paused. Download will restart when resumed.')
                should_pause=paused(store, 'youtube')
                if should_pause and not suspended:
                    os.killpg(proc.pid,signal.SIGSTOP)
                    suspended=True;pause_started=time.monotonic()
                    update_progress(store,job['id'],'Paused by the global download control.','paused')
                elif suspended and not should_pause:
                    os.killpg(proc.pid,signal.SIGCONT)
                    suspended=False;elapsed=time.monotonic()-pause_started;start+=elapsed;last_advance+=elapsed
                    update_progress(store,job['id'],'Resuming download.','downloading')
                if suspended:continue
                if time.monotonic()-start>6*3600:raise ValueError('Download exceeded six hours.')
                if shutil.disk_usage(root).free<256*1024**2:raise ValueError('Media storage is nearly full.')
                with log_path.open(encoding='utf-8',errors='replace') as reader:
                    reader.seek(position);lines=reader.read(65536);position=reader.tell()
                pending+=lines.replace('\r','\n')
                parts=pending.split('\n');pending=parts.pop()[-65536:]
                for line in parts:process_line(store,job['id'],line)
                report=ffmpeg_progress(progress_path)
                if report:
                    processed,speed,signature=report
                    if signature!=last_report:last_advance=time.monotonic();last_report=signature
                    stalled=int(time.monotonic()-last_advance)
                    info=f'FFmpeg processed {processed} of video · Speed {speed} · Process running for {int((time.monotonic()-start)/60)} min.'
                    if stalled>=120:info+=f' No reported progress for {stalled//60} min; the process is still running. You can cancel and retry.'
                    if info!=last_info:
                        with store.connect() as db:db.execute("UPDATE downloads SET processing_info=?,updated=? WHERE id=? AND state='downloading'",(info,time.time(),job['id']))
                        last_info=info

            with store.connect() as db:
                if cancelled_in(db,job['id']):raise ValueError('Cancelled.')
            with log_path.open(encoding='utf-8',errors='replace') as reader:
                reader.seek(max(0,log_path.stat().st_size-65536));output=reader.read()
            for line in output.splitlines():process_line(store,job['id'],line)
            return proc.returncode,output
        finally:
            if proc.poll() is None:
                if suspended:os.killpg(proc.pid,signal.SIGCONT)
                if os.name=='nt':proc.terminate()
                else:os.killpg(proc.pid,signal.SIGTERM)
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name=='nt':proc.kill()
                    else:os.killpg(proc.pid,signal.SIGKILL)
                    proc.wait()


def recover(store):
    from .acquisition import settle_after_restart
    if settle_after_restart(store):
        return
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute("SELECT id,state FROM downloads WHERE state IN ('queued','downloading','cancelling')").fetchall():
            if cancelled_in(db,row['id']):
                db.execute("UPDATE downloads SET state='cancelled',message='Cancelled. Automatic retries remain stopped.' WHERE id=?",(row['id'],))
            elif row['state']=='downloading':
                db.execute("UPDATE downloads SET state='queued',progress=0,message='Resuming after restart' WHERE id=?",(row['id'],))
        db.execute("UPDATE channel_tasks SET state='failed',message='Interrupted by restart; retry this action.' WHERE state='running'")
        db.execute("UPDATE downloads SET state='cancelled',message='Cancelled' WHERE state='cancelling'")


def tick(store, media, stop, runner=run_download):
    from .download_options import read
    from .acquisition import held
    if stop.is_set():return
    options=read(store, 'youtube')
    if options['pause_downloads'] or held(store):return
    from .vpn import downloads_blocked
    if downloads_blocked('youtube'):return
    if options['sleep_interval'] and time.time()<float(store.get('download_finished_at','0'))+options['sleep_interval']:return
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        from .media_holds import blocked_in
        rows=db.execute("SELECT d.* FROM downloads d WHERE d.state='queued' AND d.retry_at<=? AND (d.automatic=0 OR NOT EXISTS (SELECT 1 FROM videos v JOIN download_rules r ON r.channel=v.channel WHERE v.id=d.id AND r.subscription_paused=1)) ORDER BY d.created,d.id LIMIT 128",(time.time(),)).fetchall()
        job=None
        for row in rows:
            if blocked_in(db,['video:'+row['id'],'managed:video:'+row['id']]):
                db.execute("UPDATE downloads SET state='cancelled',message='Cleared; automatic retries stopped.',updated=? WHERE id=?",(time.time(),row['id']))
                continue
            job=dict(row)
            job['retry_exhausted']=job['attempt_count']>=MAX_ATTEMPTS
            if not job['retry_exhausted']:job['attempt_count']+=1
            if job.get('quality_inherited'):
                saved=db.execute('SELECT channel FROM videos WHERE id=?',(job['id'],)).fetchone()
                from .channel_downloads import preferences
                job['quality']=preferences(store,saved['channel'] if saved else '')['quality']
            message='Checking the final attempt’s completed file.' if job['retry_exhausted'] else 'Preparing download.' if job['attempt_count']==1 else f'Automatic retry {job["attempt_count"]-1} of 5. Preparing download.'
            db.execute("UPDATE downloads SET state='downloading',quality=?,phase='preparing',processing_info='',message=?,attempt_count=?,retry_at=0,updated=? WHERE id=?", (job['quality'],message,job['attempt_count'],time.time(), job['id']))
            break
        if job is None:return
    try:
        # A crash after the atomic rename but before saving completion is recoverable.
        target = library.local_file(store,media,job['id'])
        from .discover_publication import request_for_video, planned_path
        request=request_for_video(store,job['id'])
        if target and request:
            with store.connect() as db:record=db.execute('SELECT path FROM media_files WHERE id=?',(job['id'],)).fetchone()
            if not record or record['path']!=planned_path(store,media,request,target.suffix):target=None
        notice=''
        if not target or job.get('replace_existing'):
            if job['retry_exhausted']:
                raise YouTubeFailure(job['retry_error'] or 'The final download attempt was interrupted.')
            active = runner
            from .discover_youtube import is_discover, run as run_discover
            if is_discover(store, job['id']):
                active = run_discover
            notice=active(store, media, job, stop) or ''
        else:
            with store.connect() as db:
                record=db.execute('SELECT * FROM media_files WHERE id=?',(job['id'],)).fetchone()
            if record:
                check_attempt(store,job,stop)
                notice=library.write_metadata(store,media,dict(record))
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if cancelled_in(db,job['id']):raise ValueError('Cancelled.')
            db.execute("UPDATE downloads SET state='complete',progress=100,message=?,replace_existing=0,retry_at=0,retry_error='',updated=? WHERE id=?",
                       ('Saved on NAS'+(' · '+notice if notice else ''),time.time(),job['id']))
        from .view_media.local import invalidate
        invalidate(media)
        if store.get('emby_auto_refresh')=='true':
            with store.connect() as db:row=db.execute('SELECT channel FROM videos WHERE id=?',(job['id'],)).fetchone()
            if row:
                from .emby import queue_refresh
                queue_refresh(store,row[0])
    except DownloadPaused as exc:
        # A concurrent Cancel all must win over returning this job to the queue.
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT state FROM downloads WHERE id=?',(job['id'],)).fetchone()
            cancelling=cancelled_in(db,job['id'])
            if current and current[0] in ('downloading','cancelling'):
                db.execute('UPDATE downloads SET state=?,message=?,attempt_count=MAX(0,attempt_count-?),updated=? WHERE id=?',
                           ('cancelled' if cancelling else 'queued',
                            'Cancelled' if cancelling else str(exc) or 'Downloads paused. This video will restart when resumed.',
                            int(not job['retry_exhausted']),time.time(),job['id']))
        if cancelling:keep_replaced_file(store,media,job,'Cancelled')
    except DownloadSkipped as exc:
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cancelled=cancelled_in(db,job['id'])
            reason='Cancelled' if cancelled else str(exc)
            db.execute('UPDATE downloads SET state=?,progress=0,message=?,updated=? WHERE id=?',
                       ('cancelled' if cancelled else 'skipped',reason,time.time(),job['id']))
        keep_replaced_file(store,media,job,reason)
    except InterruptedError:
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cancelled=cancelled_in(db,job['id'])
            db.execute('UPDATE downloads SET state=?,message=?,updated=? WHERE id=?',
                       ('cancelled' if cancelled else 'queued','Cancelled' if cancelled else 'Resuming after restart',time.time(),job['id']))
        if cancelled:keep_replaced_file(store,media,job,'Cancelled')
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'Download interrupted. Check media folder access and free space, then retry.'
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT state,message FROM downloads WHERE id=?', (job['id'],)).fetchone()
            cancelled = cancelled_in(db,job['id'])
            # Keep the reason when channel settings stopped the download.
            reason = current[1] if cancelled and current and current[1].startswith('Cancelled: ') else 'Cancelled'
            retry=not cancelled and current and current[0]=='downloading' and job['attempt_count']<MAX_ATTEMPTS
            deadline=max(time.time()+RETRY_DELAYS[job['attempt_count']-1],getattr(exc,'retry_at',0)) if retry else 0
            status=f'Automatic retry {job["attempt_count"]} of 5 scheduled. Waiting before trying YouTube again.' if retry else message+' Five automatic retries have been used. Select Retry to start again.'
            db.execute('UPDATE downloads SET state=?,progress=0,message=?,phase=?,retry_at=?,retry_error=?,updated=? WHERE id=?',
                       ('cancelled' if cancelled else 'queued' if retry else 'failed',reason if cancelled else status,
                        'retry_wait' if retry else '',deadline,'' if cancelled else message,time.time(),job['id']))
        if cancelled:keep_replaced_file(store,media,job,reason)

    finally:
        if not stop.is_set():store.set('download_finished_at',time.time())


def keep_replaced_file(store, media, job, reason):
    """A stopped replacement leaves the saved file untouched: restore 'complete'."""
    if not job.get('replace_existing') or not library.local_file(store,media,job['id']):return False
    with store.connect() as db:
        db.execute("UPDATE downloads SET state='complete',progress=100,replace_existing=0,message=?,updated=? WHERE id=?",
                   (f'Replacement stopped ({reason.rstrip(".")}). The saved file was kept.',time.time(),job['id']))
    return True


def loop(store, media, stop):
    while not stop.is_set():
        paused = True
        with store.background_operation():
            if store.reset_pending():
                paused = True
            else:
                paused = False
                try:
                    from .channel_downloads import tick as channel_tick
                    from .storage_layout import path
                    current=path(store,'youtube',media)
                    library.metadata_batch(store,current)
                    channel_tick(store,current)
                    tick(store, current, stop)
                except Exception:
                    logging.exception('Download queue paused; retrying')
        if paused:
            stop.wait(1)
            continue
        stop.wait(2)
