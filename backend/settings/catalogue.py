"""Metadata pre-download is independent of video acquisition and sidecar options."""
from .schema import Field,write
FIELDS={
    'enabled':Field('catalogue_pre_download',False,message='Choose on or off.'),
    'minutes':Field('catalogue_refresh_minutes',60,(30,60,180,360,720,1440),message='Choose a supported refresh interval.'),
}
def check(store,values):pass
def after_write(db,values):
    import time
    write(db,{'catalogue_generation':str(time.time_ns())})
