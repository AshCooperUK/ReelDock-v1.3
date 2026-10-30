"""Pure duration facts with explicit units; never probes files or providers."""
import math
import re

MAX_SECONDS = 31_536_000
MAX_MINUTES = MAX_SECONDS / 60


def positive(value, maximum=MAX_SECONDS):
    if isinstance(value, str):
        if not re.fullmatch(r'\d+(?:\.\d+)?', value.strip()):return None
        value=float(value)
    if type(value) not in (int,float) or not 0<value<=maximum or not math.isfinite(value):return None
    return value


def clock_seconds(value):
    if not isinstance(value,str) or len(value)>40:return None
    clock=re.fullmatch(r'(\d+):(\d{2})(?::(\d{2}))?',value.strip())
    if clock and int(clock[2])<60 and (clock[3] is None or int(clock[3])<60):
        return positive(int(clock[1])*60+int(clock[2]) if clock[3] is None else int(clock[1])*3600+int(clock[2])*60+int(clock[3]))
    iso=re.fullmatch(r'PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?',value.strip())
    if iso and any(part is not None for part in iso.groups()):
        return positive(float(iso[1] or 0)*3600+float(iso[2] or 0)*60+float(iso[3] or 0))
    return None


def fields(*, seconds=None, minutes=None, clock=None, pending=False):
    """Carry known source units without treating a bare duration as minutes."""
    result={}
    seconds=positive(seconds)
    minutes=positive(minutes,MAX_MINUTES)
    if seconds is not None:result['durationSeconds']=seconds
    if minutes is not None:result['runtimeMinutes']=minutes
    if clock_seconds(clock) is not None:result['duration']=clock.strip()[:40]
    if pending:result['metadata_pending']=True
    return result
