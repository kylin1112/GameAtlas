"""Isolated adapter: capture candidate frames, then run unmodified task metrics.

Apply has candidate code but no GT. Score has GT but only reads inert candidate
frames/source; candidate render entrypoints are intercepted, never executed.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def load_apply():
    spec = importlib.util.spec_from_file_location('task_apply', '/opt/gamebench/task_apply.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply(config):
    m = load_apply()
    artifact = Path('/artifact')
    if config['task'] == 'threejs-beautiful-game-render-repair':
        m.SEARCH_CASES = tuple(config['cases'])
        m.main()
        source = artifact / 'source/src'
        source.mkdir(parents=True)
        shutil.copy2('/app/workspace/src/scene.js', source / 'scene.js')
    elif config['task'].endswith('-threejs'):
        targets = json.loads(m.TARGETS.read_text())
        original_render = m.render_case
        first_capture = True
        def render_case(camera, out_dir, force_bake):
            nonlocal first_capture
            bake = first_capture
            first_capture = False
            return original_render(camera, out_dir, bake)
        m.render_case = render_case
        captured = {}
        for rung in dict.fromkeys([config['rung'], config['order'][-1]]):
            key = json.dumps(config['controls'][rung], sort_keys=True)
            if key in captured:
                shutil.copytree(captured[key], artifact / rung)
                continue
            data = json.loads(json.dumps(targets))
            for case in data['autoresearch_cases']:
                case['camera'].update(config['controls'][rung])
            path = Path('/tmp') / (rung + '.json')
            path.write_text(json.dumps(data))
            m.TARGETS = path
            m.ARTIFACT = artifact / rung
            m.ARTIFACT.mkdir()
            m.main()
            captured[key] = artifact / rung
    else:
        # Native tools compile under WORKSPACE/build. Keep the source mounts
        # read-only and compile a copy in disposable, GT-free scratch space.
        with tempfile.TemporaryDirectory(prefix='gamebench-native-') as folder:
            workspace = Path(folder)
            for name in ['src', 'tools']:
                shutil.copytree(Path('/app/workspace') / name, workspace / name)
            for name in ['assets', 'third_party']:
                (workspace / name).symlink_to(Path('/app/workspace') / name, target_is_directory=True)
            m.WORKSPACE = workspace
            m.main()


def score(config):
    sys.path.insert(0, '/tests')
    artifact = Path('/artifact')
    if config['task'] == 'sponza-reference-render-ladder-v2':
        import evaluate_rung
        # Stage identity is coordinator-owned, never taken from candidate data.
        evaluate_rung.read_rung = lambda workspace: config['rung']
        evaluate_rung.main(artifact=artifact)
    elif config['task'].endswith('-threejs'):
        import evaluate_rung as m
        e = m.evaluator
        # Only src is editable in this task. Keep trusted renderer/harness files
        # from the image; copy inert source to a temporary inspection workspace.
        workspace = Path('/tmp/candidate-inspection')
        workspace.mkdir()
        source = artifact / config['rung'] / 'source/src'
        if source.is_dir():
            shutil.copytree(source, workspace / 'src')
        else:
            (workspace / 'src').mkdir()
        shutil.copy2('/app/workspace/render_main.js', workspace / 'render_main.js')
        shutil.copytree('/app/workspace/tools', workspace / 'tools')
        e.WORKSPACE = workspace
        cases = json.loads(Path('/tests/private_targets.json').read_text())['autoresearch_cases']
        def read_capture(script, out_dir, camera, force_bake=False):
            rung, name = out_dir.parent.name, out_dir.name
            index = next(i for i, c in enumerate(cases) if c['name'] == name)
            folder = artifact / rung / 'cases' / str(index)
            return e.load_rgb(folder / 'beauty.png'), json.loads((folder / 'render_summary.json').read_text()), 0.0
        e.render_with = read_capture
        os.environ['SPONZA_RUNG'] = config['rung']
        m.main()
    else:
        import evaluate_beautiful_game as e
        original = e.run_render
        count = 0
        def render(root, case, out):
            nonlocal count
            count += 1
            if count <= len(e.CASES):
                # First loop is candidate; second loop executes trusted GT only.
                shutil.copy2(artifact / 'frames' / (case + '.png'), out)
            else:
                original(root, case, out)
        e.run_render = render
        # Original verifier copies only this inert file; run_render is guarded.
        workspace = Path('/tmp/candidate-inspection')
        workspace.mkdir()
        source = artifact / 'source/src'
        if source.is_dir():
            shutil.copytree(source, workspace / 'src')
        else:
            (workspace / 'src').mkdir()
        for name in ['assets', 'vendor']:
            (workspace / name).symlink_to(Path('/app/workspace') / name, target_is_directory=True)
        e.WORKSPACE = workspace
        e.main()


if __name__ == '__main__':
    config = json.loads(os.environ['GAMEBENCH_RENDER_CONFIG'])
    if Path(sys.argv[0]).name == 'apply.py':
        apply(config)
    else:
        score(config)
