import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from backend import hardware_encoding,ytdlp_runner
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor,FFmpegPostProcessorError


class HardwareTests(unittest.TestCase):
    def test_probe_requires_successful_real_encoder_test(self):
        hardware_encoding._probe.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            device=Path(directory)/'renderD128';device.touch()
            with patch('backend.hardware_encoding.subprocess.run',return_value=Mock(returncode=0)) as run:
                self.assertEqual(hardware_encoding._probe(str(device),1),str(device))
                self.assertIn('h264_vaapi',run.call_args.args[0])
                self.assertIn('-force_key_frames',run.call_args.args[0])
            with patch('backend.hardware_encoding.subprocess.run',return_value=Mock(returncode=1)):
                self.assertIsNone(hardware_encoding._probe(str(device),2))
        hardware_encoding._probe.cache_clear()

    def test_sponsor_encode_uses_gpu_but_other_operations_are_unchanged(self):
        original=FFmpegPostProcessor.force_keyframes
        with patch.object(FFmpegPostProcessor,'force_keyframes',original),patch('backend.ytdlp_runner.available_device',return_value='/dev/dri/renderD128'):
            ytdlp_runner.install()
            processor=FFmpegPostProcessor();processor.to_screen=Mock();run=Mock();processor.run_ffmpeg=run
            result=processor.force_keyframes('video.mp4',[0,1.25,4.0])
            options=run.call_args.args[2]
            self.assertIn('h264_vaapi',options);self.assertIn('1.250000,4.000000',options)
            self.assertIn('keyframes.temp',result)
            self.assertIs(processor.run_ffmpeg,run)

    def test_sponsor_gpu_failure_retries_same_source_on_cpu(self):
        original=FFmpegPostProcessor.force_keyframes
        with patch.object(FFmpegPostProcessor,'force_keyframes',original),patch('backend.ytdlp_runner.available_device',return_value='/dev/dri/renderD128'):
            ytdlp_runner.install()
            processor=FFmpegPostProcessor();processor.to_screen=Mock();run=Mock(side_effect=[FFmpegPostProcessorError('GPU busy'),None]);processor.run_ffmpeg=run
            processor.force_keyframes('video.mp4',[1.25])
            self.assertEqual(run.call_count,2)
            first,second=run.call_args_list
            self.assertEqual(first.args[:2],second.args[:2])
            self.assertIn('h264_vaapi',first.args[2]);self.assertNotIn('h264_vaapi',second.args[2])
            self.assertIn('-force_key_frames',second.args[2]);self.assertIs(processor.run_ffmpeg,run)

    def test_no_device_keeps_cpu_sponsor_encoding(self):
        original=FFmpegPostProcessor.force_keyframes
        with patch.object(FFmpegPostProcessor,'force_keyframes',original),patch('backend.ytdlp_runner.available_device',return_value=None):
            ytdlp_runner.install()
            processor=FFmpegPostProcessor();processor.to_screen=Mock();processor.run_ffmpeg=Mock()
            processor.force_keyframes('video.mp4',[1.25])
            self.assertNotIn('h264_vaapi',processor.run_ffmpeg.call_args.args[2])
