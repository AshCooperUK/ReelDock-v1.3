"""Probe an accessible VAAPI encoder before choosing hardware encoding."""
import os
import subprocess
import time
from functools import lru_cache
from pathlib import Path


def encoder_args(device):
    return ['-vaapi_device',device,'-vf','format=nv12,hwupload','-c:v','h264_vaapi','-qp','22']


def available_device():
    return _probe(os.environ.get('ReelDock_VAAPI_DEVICE',''),int(time.time()//300))


@lru_cache(maxsize=4)
def _probe(configured,period):
    devices=[Path(configured)] if configured else sorted(Path('/dev/dri').glob('renderD*'))
    for device in devices:
        if not device.exists() or not os.access(device,os.R_OK|os.W_OK):continue
        try:
            result=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin',
                '-f','lavfi','-i','color=size=128x128:rate=25','-frames:v','3',
                *encoder_args(str(device)),'-force_key_frames','0,0.04','-f','null','-'],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
            if result.returncode==0:return str(device)
        except (OSError,subprocess.TimeoutExpired):pass
    return None
