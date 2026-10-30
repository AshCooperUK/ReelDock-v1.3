"""Use VAAPI only for SponsorBlock's keyframe re-encode, not its stream-copy cuts.

This adapter is scoped to this yt-dlp subprocess and the pinned dependency version.
"""
from .hardware_encoding import available_device,encoder_args


def install():
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor,FFmpegPostProcessorError
    original=FFmpegPostProcessor.force_keyframes
    def force_keyframes(self,filename,timestamps):
        device=available_device()
        run=self.run_ffmpeg
        def encode(source,target,options,**kwargs):
            if not device:
                self.to_screen('ReelDock CPU: Hardware encoder unavailable; using CPU.')
                return run(source,target,options,**kwargs)
            self.to_screen('ReelDock GPU: Encoding sponsor cuts with VAAPI hardware.')
            try:return run(source,target,[*options,*encoder_args(device)],**kwargs)
            except FFmpegPostProcessorError:
                self.to_screen('ReelDock CPU: Hardware encoding failed; retrying sponsor cuts on CPU.')
                return run(source,target,options,**kwargs)
        self.run_ffmpeg=encode
        try:return original(self,filename,timestamps)
        finally:self.run_ffmpeg=run
    FFmpegPostProcessor.force_keyframes=force_keyframes


if __name__=='__main__':
    from .vpn.transport import install as install_vpn_transport
    install_vpn_transport()
    import yt_dlp
    install()
    yt_dlp.main()
