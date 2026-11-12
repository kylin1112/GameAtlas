"""Runtime adapters for the three expert-authored CPU rendering tasks.

Task instructions and scorer sources stay untouched. Final evaluation uses the
original final scorer; online feedback keeps the original autoresearch scorer.
"""
import ast
from dataclasses import replace
import json
from pathlib import Path
import tomllib

NATIVE = 'sponza-reference-render-ladder-v2'
THREEJS = NATIVE + '-threejs'
BEAUTIFUL = 'threejs-beautiful-game-render-repair'
TASKS = {NATIVE, THREEJS, BEAUTIFUL}
RUNTIME = Path(__file__).with_name('rendering_runtime.py')


def supported(task, spec):
    return Path(task).name in TASKS and spec.get('environment', {}).get('gpus', 0) == 0


def literal(path, name):
    for node in ast.parse(Path(path).read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f'Missing task-owned constant: {name}')


def configuration(task, output, *, phase='online', stage=None):
    """Return a per-evaluation immutable config; never change a shared ledger."""
    from .evaluator import Mount
    if task.task_id not in TASKS:
        return task
    spec = tomllib.loads((task.task_dir / 'task.toml').read_text())
    task = replace(task, verifier_image=task.verifier_image or spec['environment']['docker_image'])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    data = {'task': task.task_id, 'phase': phase}
    mounts = list(task.apply_mounts)
    if task.task_id == NATIVE:
        if stage is None:
            raise ValueError('Native evaluation needs a trusted active stage')
        data['rung'] = stage.verifier_env['SPONZA_RUNG']
        mounts.append(Mount(task.apply_script, '/opt/gamebench/task_apply.py'))
        task = replace(task, apply_script=RUNTIME, score_script=RUNTIME, apply_tmp_exec=True)
        rung = output / 'current_rung.json'
        rung.write_text(json.dumps({'rung': data['rung']}))
        # Override the task's stage identity after mounting the candidate tools.
        mounts.append(Mount(rung.resolve(), '/app/workspace/tools/current_rung.json'))
    if phase == 'final':
        timeout = stage.verifier_timeout if stage else spec['verifier']['timeout_sec']
        task = replace(task, evaluation_timeout_sec=timeout,
                       apply_timeout_sec=timeout, score_timeout_sec=timeout)
        if task.task_id in {NATIVE, THREEJS}:
            if stage is None:
                raise ValueError('Stage final evaluation requires stage identity')
            data['rung'] = stage.verifier_env['SPONZA_RUNG']
            task = replace(task, score_timeout_sec=stage.verifier_timeout)
        if task.task_id == THREEJS:
            data['controls'] = literal(task.task_dir / 'tests/evaluate_rung.py', 'RUNG_CONTROLS')
            data['order'] = literal(task.task_dir / 'tests/evaluate_rung.py', 'RUNG_ORDER')
        elif task.task_id == BEAUTIFUL:
            data['cases'] = literal(task.task_dir / 'tests/evaluate_beautiful_game.py', 'CASES')
        if task.task_id != NATIVE:
            mounts.append(Mount(task.apply_script, '/opt/gamebench/task_apply.py'))
        task = replace(task, apply_script=RUNTIME, score_script=RUNTIME)
    config = output / 'rendering.json'
    config.write_text(json.dumps(data))
    mounts.append(Mount(config.resolve(), '/opt/gamebench/rendering.json'))
    return replace(task, apply_mounts=tuple(mounts),
                   evaluation_env=(('GAMEBENCH_RENDER_CONFIG', json.dumps(data)),))


def rejected_candidate_reward(instance, task, output, deadline):
    """Ask the original scorer to grade an absent/rejected candidate.

    In particular Native's stage bands must not be replaced by a synthetic zero.
    This path is only entered after a recorded candidate failure, never infra.
    """
    import math
    import uuid
    from .evaluator import CoordinatorError
    folder = Path(output) / 'rejected-candidate'
    artifact, result = folder / 'artifact', folder / 'result'
    (artifact / 'source').mkdir(parents=True)
    result.mkdir()
    task = replace(task, evaluation_deadline=deadline)
    name = 'gb-verify-' + uuid.uuid4().hex[:12]
    try:
        completed = instance._run_container(
            name=name, image=task.verifier_image,
            command=['python3', '/opt/gamebench/score.py'],
            mounts=[(task.task_dir / 'tests', '/tests', True),
                    (task.score_script, '/opt/gamebench/score.py', True),
                    (artifact, '/artifact', True), (result, '/logs/verifier', False)],
            timeout=task.score_timeout_sec, task=task, read_only=True)
        if completed.returncode:
            raise CoordinatorError('verifier', 'Rejected-candidate scorer failed')
        rewards = json.loads((result / 'reward.json').read_text())
        score = rewards.get('reward')
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            raise CoordinatorError('verifier', 'Invalid rejected-candidate reward')
        return rewards
    finally:
        instance._podman_best_effort(['rm', '-f', name], timeout=30)
