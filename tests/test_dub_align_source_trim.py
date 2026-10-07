"""Storyboard-selected in-points reach ffmpeg without changing legacy zero-offset rendering."""

import unittest
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

from dub_align_studio import render_b
from dub_align_studio.render_b import RenderConfig
from dub_align_studio.timing import LineTiming


class SourceTrimTests(unittest.TestCase):
    def test_segment_seeks_before_input(self):
        with patch.object(render_b, '_has_audio_stream', return_value=False), \
                patch.object(render_b, '_run') as run:
            render_b._render_silent_segment(RenderConfig(width=320, height=180),
                                             Path('source.mp4'), 'fps=30', 30, Path('out.mp4'),
                                             source_in_seconds=2.5)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('-ss'):command.index('-ss') + 4],
                         ['-ss', '2.500', '-i', 'source.mp4'])

    def test_negative_in_point_rejected(self):
        with self.assertRaisesRegex(ValueError, '入点'):
            render_b._render_silent_segment(RenderConfig(width=320, height=180),
                                             Path('source.mp4'), 'fps=30', 30, Path('out.mp4'),
                                             source_in_seconds=-1)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), '需要 ffmpeg/ffprobe')
    def test_render_records_source_in_point(self):
        with tempfile.TemporaryDirectory(prefix='source_trim_') as temp:
            root = Path(temp)
            audio = root / 'master.wav'
            with wave.open(str(audio), 'wb') as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(8000)
                handle.writeframes(b'\0\0' * 8000)
            source = root / 'source.mp4'
            subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i',
                            'testsrc=size=320x180:rate=30:duration=3',
                            '-pix_fmt', 'yuv420p', str(source)], check=True, capture_output=True)
            result = render_b.render_b(audio, [LineTiming(1, '一句', 1.0)], [source],
                                       root / 'film.mp4', config=RenderConfig(width=320, height=180),
                                       source_offsets=[1.5])
            self.assertTrue(result.output_path.is_file())
            self.assertEqual(result.shots[0].source_in_seconds, 1.5)


if __name__ == '__main__':
    unittest.main()
