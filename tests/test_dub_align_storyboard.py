"""Five-column storyboard import and library matching, independent of TTS/ffmpeg."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from dub_align_studio import storyboard
from dub_align_studio import studio_pipeline as pipeline


def workbook(path: Path, rows: list[list[str]]) -> None:
    cells = []
    for number, row in enumerate(rows, 1):
        parts = []
        for column, value in zip("ABCDE", row):
            parts.append(f'<c r="{column}{number}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        cells.append(f'<row r="{number}">{"".join(parts)}</row>')
    xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join(cells) + '</sheetData></worksheet>'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('xl/worksheets/sheet1.xml', xml)


class StoryboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='storyboard_')
        self.root = Path(self.tmp.name)
        self.sheet = self.root / 'shots.xlsx'
        workbook(self.sheet, [
            ['镜头序号', '口播文稿', '时长', '图片生成提示词', '视频生成提示词'],
            ['一', '地球在太空中旋转', '7秒', '蓝色地球在星空中', '镜头缓缓靠近地球'],
            ['二', '黑洞吞噬恒星', '4.5 秒', '黑洞和恒星', '恒星被黑洞拉伸'],
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_five_columns_preserves_excel_row_and_duration(self):
        board = storyboard.read_storyboard(self.sheet)
        self.assertEqual(len(board.shots), 2)
        self.assertEqual(board.shots[0].row, 2)
        self.assertEqual(board.shots[0].label, '一')
        self.assertEqual(board.shots[0].narration, '地球在太空中旋转')
        self.assertEqual(board.shots[0].duration, 7.0)
        self.assertEqual(board.shots[1].duration, 4.5)
        self.assertEqual(board.shots[1].video_prompt, '恒星被黑洞拉伸')
        self.assertEqual(board.text, '地球在太空中旋转\n黑洞吞噬恒星')

    def test_missing_required_narration_is_reported_with_row(self):
        workbook(self.sheet, [
            ['镜头序号', '口播文稿', '时长', '图片生成提示词', '视频生成提示词'],
            ['一', '', '7秒', '地球', '地球旋转'],
        ])
        with self.assertRaisesRegex(ValueError, '第 2 行.*口播'):
            storyboard.read_storyboard(self.sheet)

    def test_match_library_excludes_review_and_persists_editable_plan(self):
        library = self.root / 'library'
        (library / '关键词匹配库' / '地球太空').mkdir(parents=True)
        (library / '关键词匹配库' / '黑洞恒星').mkdir(parents=True)
        (library / '待人工复核').mkdir(parents=True)
        earth = library / '关键词匹配库' / '地球太空' / 'BJ1_地球太空.mp4'
        black = library / '关键词匹配库' / '黑洞恒星' / 'BJ2_黑洞吞噬恒星.mp4'
        for path in (earth, black, library / '待人工复核' / 'BJ3_地球太空.mp4'):
            path.write_bytes(b'x')
        board = storyboard.read_storyboard(self.sheet)
        plan_path = self.root / 'out' / '剪辑方案.json'
        plan = storyboard.prepare_plan(board, library, plan_path)
        self.assertEqual([Path(x['source']).name for x in plan['shots']], [earth.name, black.name])
        self.assertEqual(len(plan['shots']), 2)
        self.assertTrue(all(x['candidates'] for x in plan['shots']))
        self.assertTrue(all(x['review_required'] for x in plan['shots']))
        self.assertTrue(plan_path.exists())
        self.assertTrue((plan_path.parent / '选片复核清单.csv').exists())
        # A manual source edit must survive a repeat run with the same workbook/library.
        saved = json.loads(plan_path.read_text(encoding='utf-8'))
        saved['shots'][0]['source'] = str(black)
        plan_path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        reused = storyboard.prepare_plan(board, library, plan_path)
        self.assertEqual(reused['shots'][0]['source'], str(black))
        self.assertTrue(reused['shots'][0]['locked'])
        self.assertEqual(storyboard.plan_videos(reused), [black, black])

    def test_changed_workbook_invalidates_plan(self):
        library = self.root / 'library'
        library.mkdir()
        (library / 'BJ1_地球太空.mp4').write_bytes(b'x')
        board = storyboard.read_storyboard(self.sheet)
        path = self.root / 'out' / '剪辑方案.json'
        first = storyboard.prepare_plan(board, library, path)
        workbook(self.sheet, [
            ['镜头序号', '口播文稿', '时长', '图片生成提示词', '视频生成提示词'],
            ['一', '地球在太空中旋转', '7秒', '蓝色地球在星空中', '镜头缓缓靠近地球'],
        ])
        second = storyboard.prepare_plan(storyboard.read_storyboard(self.sheet), library, path)
        self.assertNotEqual(first['workbook_sha256'], second['workbook_sha256'])
        self.assertEqual(len(second['shots']), 1)

    def test_matching_prefers_distinct_sources_when_equivalent(self):
        library = self.root / 'library'
        library.mkdir()
        for name in ('BJ1_地球太空.mp4', 'BJ2_地球太空.mp4'):
            (library / name).write_bytes(b'x')
        workbook(self.sheet, [
            ['镜头序号', '口播文稿', '时长', '图片生成提示词', '视频生成提示词'],
            ['一', '地球太空', '2秒', '地球太空', '地球太空'],
            ['二', '地球太空', '2秒', '地球太空', '地球太空'],
        ])
        plan = storyboard.prepare_plan(storyboard.read_storyboard(self.sheet), library,
                                       self.root / 'out' / '剪辑方案.json')
        self.assertNotEqual(plan['shots'][0]['source'], plan['shots'][1]['source'])

    def test_candidate_choice_is_validated_and_persisted(self):
        library = self.root / 'library'
        library.mkdir()
        for name in ('BJ1_地球太空.mp4', 'BJ2_地球太空.mp4'):
            (library / name).write_bytes(b'x')
        path = self.root / 'out' / '剪辑方案.json'
        plan = storyboard.prepare_plan(storyboard.read_storyboard(self.sheet), library, path)
        other = plan['shots'][0]['candidates'][1]['source']
        with self.assertRaisesRegex(ValueError, '候选'):
            storyboard.choose_candidate(path, 2, str(self.root / 'not-allowed.mp4'))
        edited = storyboard.choose_candidate(path, 2, other, source_in_seconds=1.25)
        self.assertEqual(edited['shots'][0]['source'], other)
        self.assertEqual(edited['shots'][0]['source_in_seconds'], 1.25)
        self.assertTrue(edited['shots'][0]['locked'])
        with self.assertRaisesRegex(ValueError, '入点'):
            storyboard.choose_candidate(path, 2, other, source_in_seconds=-1)
        reused = storyboard.prepare_plan(storyboard.read_storyboard(self.sheet), library, path)
        self.assertEqual(reused['shots'][0]['source'], other)

    def test_changing_one_excel_row_preserves_other_locked_choice(self):
        library = self.root / 'library'
        library.mkdir()
        for name in ('BJ1_地球太空.mp4', 'BJ2_地球太空.mp4', 'BJ3_黑洞恒星.mp4'):
            (library / name).write_bytes(b'x')
        path = self.root / 'out' / '剪辑方案.json'
        first = storyboard.prepare_plan(storyboard.read_storyboard(self.sheet), library, path)
        other = first['shots'][0]['candidates'][1]['source']
        storyboard.choose_candidate(path, 2, other)
        workbook(self.sheet, [
            ['镜头序号', '口播文稿', '时长', '图片生成提示词', '视频生成提示词'],
            ['一', '地球在太空中旋转', '7秒', '蓝色地球在星空中', '镜头缓缓靠近地球'],
            ['二', '恒星坍缩形成黑洞', '4.5 秒', '黑洞和恒星', '恒星被黑洞拉伸'],
        ])
        second = storyboard.prepare_plan(storyboard.read_storyboard(self.sheet), library, path)
        self.assertEqual(second['shots'][0]['source'], other)
        self.assertTrue(second['shots'][0]['locked'])
        self.assertEqual(second['shots'][1]['narration'], '恒星坍缩形成黑洞')

    def test_dub_reuse_signature_changes_with_voice_and_speed(self):
        board = storyboard.read_storyboard(self.sheet)
        baseline = storyboard.dub_signature(board, 'mock', 'voice-a', {'speed': 1.0})
        self.assertNotEqual(baseline, storyboard.dub_signature(board, 'mock', 'voice-b', {'speed': 1.0}))
        self.assertNotEqual(baseline, storyboard.dub_signature(board, 'mock', 'voice-a', {'speed': 1.2}))

    def test_pipeline_requires_matching_imported_text_and_writes_plan(self):
        library = self.root / 'library'
        library.mkdir()
        earth = library / 'BJ1_地球太空.mp4'
        earth.write_bytes(b'x')
        board = storyboard.read_storyboard(self.sheet)
        out = self.root / 'out'
        with self.assertRaisesRegex(ValueError, '文案.*表格'):
            pipeline.select_shot_videos(library, ['改过的口播'], 'storyboard', 42, out,
                                        storyboard_path=self.sheet)
        result = pipeline.select_shot_videos(library, board.text.splitlines(), 'storyboard', 42,
                                             out, storyboard_path=self.sheet)
        self.assertEqual(len(result), 2)
        self.assertTrue((out / storyboard.PLAN_NAME).exists())

    def test_ui_exposes_dedicated_storyboard_import(self):
        from dub_align_studio import web_server
        html = (Path(web_server.__file__).parent / 'web' / 'index.html').read_text(encoding='utf-8')
        for marker in ('pickDoc(\'storyboard\')', 'storyboard_path', 'value="storyboard"',
                       'id="storyboardPath"', 'id="storyboardPreview"',
                       'previewStoryboard()', 'changeStoryboardCandidate('):
            self.assertIn(marker, html)


if __name__ == '__main__':
    unittest.main()
