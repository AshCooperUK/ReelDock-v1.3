"""Resume only work known not to have reached YouTube after session recovery."""
import hashlib
import time

DOWNLOAD_MESSAGE = 'YouTube cookies need attention. This download will retry after fresh cookies pass the session check.'


def record(store, state):
    if state in ('auth_required', 'auth_expired', 'auth_unverified'):
        if store.get('youtube_cookie_issue') != state:
            store.set('youtube_cookie_issue_id', str(time.time_ns()))
        store.set('youtube_cookie_issue', state)
    elif state == 'active':
        store.set('youtube_cookie_issue', '')


def warning(store):
    from .watched_status import cookie_session_file_status
    target = store.directory / 'youtube-cookies.txt'
    state = store.get('youtube_cookie_issue')
    if target.exists():
        file_state, _ = cookie_session_file_status(target)
        if file_state in ('auth_required', 'auth_expired'):
            state = file_state
    if not state and store.get('emby_watched_paused'):
        from .youtube_matching import source_scope
        scope = source_scope(store)
        with store.connect() as db:
            row = db.execute("SELECT state FROM emby_watched WHERE scope=? AND state IN ('auth_required','auth_expired','auth_unverified') LIMIT 1", (scope,)).fetchone()
        state = row[0] if row else ''
    if not state:
        return None
    messages = {
        'auth_expired': 'Your YouTube cookies have expired. Upload fresh cookies to resume affected work.',
        'auth_required': 'Signed-in YouTube cookies are missing or invalid. Upload fresh cookies.',
        'auth_unverified': 'YouTube could not confirm your cookies. Check account consent and upload fresh cookies.',
    }
    if state not in messages:
        return None
    stamp = str(target.stat().st_mtime_ns) if target.exists() else 'missing'
    return {'id': state + ':' + stamp + ':' + store.get('youtube_cookie_issue_id', ''), 'message': messages[state]}


def resume(store):
    from .youtube_matching import source_scope
    from .media_holds import blocked_in
    scope = source_scope(store)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT id FROM emby_watched WHERE scope=? AND state IN ('auth_required','auth_expired','auth_unverified','network_error','rate_limited')", (scope,)).fetchall()
        for row in rows:
            if blocked_in(db,['video:'+row[0],'managed:video:'+row[0]]):continue
            db.execute("UPDATE emby_watched SET state='retry',message='Session restored; watched check queued automatically.' WHERE scope=? AND id=?", (scope,row[0]))
            db.execute('INSERT OR REPLACE INTO emby_attempts VALUES (?,?,?,?,0)', (scope,row[0],time.time(),0))
        from .downloads import MAX_ATTEMPTS
        for row in db.execute("SELECT id FROM downloads WHERE state='failed' AND message=? AND attempt_count<?",(DOWNLOAD_MESSAGE,MAX_ATTEMPTS)).fetchall():
            if not blocked_in(db,['video:'+row[0],'managed:video:'+row[0]]):
                db.execute("UPDATE downloads SET state='queued',progress=0,message='Cookies restored; download queued automatically.',updated=? WHERE id=?",(time.time(),row[0]))
    store.set('emby_watched_paused', '')
    store.set('emby_next_check', '0')
    record(store, 'active')
