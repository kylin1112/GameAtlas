import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gameatlasharness.run import load_config
from gameatlasharness.environment import fingerprint, prepare, task_images


class ImagePreparationTests(unittest.TestCase):
    def test_default_collection_includes_all_published_tasks(self):
        root = Path(__file__).resolve().parents[2]
        if not (root / 'tasks').is_dir():
            self.skipTest('Download task packages before checking collection discovery')
        config = json.loads((root / 'gameatlasharness/model.example.json').read_text())
        config.pop('tasks', None)
        config['tasks_dir'] = str(root / 'tasks')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.json'
            path.write_text(json.dumps(config))
            discovered = load_config(path)['tasks']
        expected = sorted(task.parent.name for task in (root / 'tasks').glob('*/task.toml'))
        self.assertEqual(discovered, expected)

    def test_preparation_uses_task_contexts_and_reuses_shared_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, mode in [('one', 'separate'), ('two', 'shared')]:
                task = root / 'tasks' / name
                contexts = ['environment', 'tests'] if mode == 'separate' else ['environment']
                for context in contexts:
                    folder = task / context
                    folder.mkdir(parents=True)
                    (folder / 'Dockerfile').write_text('FROM scratch\n')
                (task / 'task.toml').write_text(
                    f'[environment]\n[verifier]\nenvironment_mode="{mode}"\n[verifier.environment]\n'
                )
            with patch('gameatlasharness.environment.Path.home', return_value=root), \
                 patch('gameatlasharness.environment.docker') as docker, \
                 patch('gameatlasharness.environment.subprocess.run') as build:
                docker.side_effect = lambda *args, **kwargs: type('Result', (), {'returncode': int(args[0] == 'image')})()
                prepare({'runtime_volume': 'fixture', 'tasks_dir': root / 'tasks', 'tasks': ['one', 'two']})
            self.assertEqual(
                [Path(call.args[0][-1]).relative_to(root / 'tasks').as_posix() for call in build.call_args_list],
                ['one/environment', 'one/tests', 'two/environment'],
            )

    def test_asset_changes_invalidate_tag_and_lfs_pointers_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)
            asset = task / 'input'
            asset.write_bytes(b'first')
            first = task_images(task, {}, fingerprint(task))
            asset.write_bytes(b'second')
            self.assertNotEqual(first, task_images(task, {}, fingerprint(task)))
            asset.write_text('version https://git-lfs.github.com/spec/v1\noid sha256:0123456789\nsize 10000\n')
            with self.assertRaisesRegex(ValueError, 'git lfs pull'):
                fingerprint(task)
