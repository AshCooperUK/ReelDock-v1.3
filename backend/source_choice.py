"""Preserve a failed selected release as a decision, never an unreleased title."""
import json
import time

from . import media_holds
from .discover_logic import redact, shared_failure


def body(row):
    try:value=json.loads(dict(row).get('body') or '{}')
    except (ValueError,TypeError):return {}
    return value if isinstance(value,dict) else {}


def manual(row):
    row=dict(row);value=body(row)
    return bool(row.get('manual') and row.get('kind') in ('movie','episode') and
                row.get('engine','') in ('','nzb') and not value.get('plan') and not value.get('movieAutomatic'))


def local_failure(message):
    text=str(message or '').lower()
    if any(word in text for word in ('password','permission','disk','storage','connection','authentication','quota','429','credentials','timed out','timeout')):return False
    if any(word in text for word in ('article','incomplete','did not contain','not a rar','compressed')):return True
    return not shared_failure(text) and any(word in text for word in ('no usable release','no release matched','size range','size-filtered'))


def eligibility(db,row):
    """Read-only legacy compatibility, also used while holding the selection fence."""
    row=dict(row)
    if not manual(row) or row['state'] not in ('failed','waiting'):return None
    if media_holds.blocked_in(db,media_holds.request_keys_in(db,row)):return None
    saved=body(row);marker=saved.get('sourceChoice')
    if isinstance(marker,dict) and marker.get('eligible') is True:
        return marker
    # A failed pre-check used to be mislabelled waiting while still pinned to
    # the exact failed release. Do not reinterpret an ordinary future request.
    selected=saved.get('managedReleaseId') or saved.get('manualReleaseId')
    if not selected:return None
    failed=db.execute("SELECT id,message FROM discover_attempts WHERE request_id=? AND candidate_id=? AND state='failed' ORDER BY id DESC LIMIT 1",(row['id'],selected)).fetchone()
    if not failed or not local_failure(failed['message']):return None
    return dict(eligible=True,attemptKey=f"{row['id']}:{failed['id']}:{row.get('stop_generation',0)}",reason=redact(failed['message'])[:500])


def public(db,row):
    marker=eligibility(db,row)
    if not marker:return None
    return dict(requestId=row['id'],attemptKey=marker['attemptKey'],eligible=True,reason=marker.get('reason','The selected release failed.'))


def fail(store,row,message,*,candidate_id=None):
    """Atomically record candidate-local failure, preserving holds and attempts."""
    if not manual(row) or not local_failure(message):return False
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT * FROM discover_requests WHERE id=?',(row['id'],)).fetchone()
        if not current or current['state'] in ('cancelled','cancelling','complete') or media_holds.blocked_in(db,media_holds.request_keys_in(db,current)):return False
        current=dict(current);saved=body(current)
        latest=db.execute("SELECT * FROM discover_attempts WHERE request_id=? AND source='nzb' AND candidate_id!='' ORDER BY id DESC LIMIT 1",(row['id'],)).fetchone()
        selected=candidate_id or saved.get('managedReleaseId') or saved.get('manualReleaseId') or (latest['candidate_id'] if latest else '')
        if not selected:return False
        candidate=db.execute('SELECT * FROM discover_candidates WHERE id=?',(selected,)).fetchone()
        if not candidate:return False
        # Native completion failures used to leave the last attempt as queued.
        if not latest or latest['candidate_id']!=selected or latest['state']!='failed':
            db.execute('INSERT INTO discover_attempts(request_id,candidate_id,source,quality,state,message,created) VALUES(?,?,?,?,?,?,?)',
                       (row['id'],selected,'nzb',candidate['quality'],'failed',redact(message)[:500],time.time()))
            attempt=db.execute('SELECT last_insert_rowid()').fetchone()[0]
        else:attempt=latest['id']
        marker=dict(eligible=True,attemptKey=f"{row['id']}:{attempt}:{current.get('stop_generation',0)}",reason=redact(message)[:500])
        saved['sourceChoice']=marker
        db.execute("UPDATE discover_requests SET state='failed',message=?,body=?,updated=? WHERE id=?",
                   ('The selected source failed. Choose another source. '+marker['reason'],json.dumps(saved),time.time(),row['id']))
    return True


def restore(store,row):
    with store.connect() as db:marker=eligibility(db,row)
    return bool(marker and fail(store,row,marker['reason']))


def select(app,store,row,candidate,attempt_key):
    from .acquisition import ensure_open,file_held
    ensure_open(store)
    from .source_alternatives import consume_approval
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if file_held(store.directory) or db.execute("SELECT 1 FROM settings WHERE key IN ('acquisition_hold','reset_in_progress') AND value='1'").fetchone():
            raise ValueError('Downloads are stopped. Resume downloads first.')
        current=db.execute('SELECT * FROM discover_requests WHERE id=?',(row['id'],)).fetchone()
        if not current or not eligibility(db,current):raise ValueError('This selection is no longer awaiting another source.')
        candidate=consume_approval(db,row['id'],candidate['id'],attempt_key)
        clash=db.execute("SELECT id FROM discover_requests WHERE episode_key=? AND id!=? AND state NOT IN ('failed','cancelled')",(current['episode_key'],row['id'])).fetchone() if current['episode_key'] else None
        if clash:raise ValueError('This media already has an active request.')
        saved=body(current);old=saved.pop('sourceChoice',None)
        history=saved.get('sourceChoiceHistory') or []
        saved['sourceChoiceHistory']=(history[-4:]+[old]) if isinstance(history,list) and old else []
        saved['managedReleaseId']=candidate['id']
        if saved.get('manualReleaseId'):
            from .discover_logic import release_pairs
            pairs=release_pairs(candidate['title'])
            if len(pairs)!=1:raise ValueError('The selected episode mapping is no longer available.')
            saved.update(manualReleaseId=candidate['id'],releaseEpisode=dict(season=pairs[0][0],number=pairs[0][1]))
        # Waiting is a durable dispatch state: a rejected in-process schedule
        # or a restart is reclaimed by the ordinary guarded worker loop.
        db.execute("UPDATE discover_requests SET body=?,state='waiting',engine='',engine_id='',progress=0,message='Selected replacement queued for checking.',stop_generation=stop_generation+1,updated=? WHERE id=?",
                   (json.dumps(saved),time.time(),row['id']))
        updated=dict(db.execute('SELECT * FROM discover_requests WHERE id=?',(row['id'],)).fetchone())
    # Provider work belongs outside the writer transaction and HTTP response.
    from .discovery_engine import schedule
    from .discover_acquire import resolve_release
    schedule(app,store,'source-choice:'+row['id'],lambda:resolve_release(app,store,updated,[dict(candidate,externalId=candidate['external_id'])]))
    return dict(id=row['id'],state='waiting')
