"""ISS-208：生产 CLI 完整合成 app 回滚；不运行安装插件/GUI/真实扫描。"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import pytest

from fathom import db, upgrade

REPO = Path(__file__).resolve().parents[1]


def write_app(app, version):
    shell = app / 'Contents/MacOS/fathom-desktop'
    shell.parent.mkdir(parents=True, exist_ok=True)
    shell.write_text('synthetic shell ' + version)
    shell.chmod(0o755)
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
        'CFBundleIdentifier': 'com.maoscripts.fathom',
        'CFBundleExecutable': 'fathom-desktop',
        'CFBundleShortVersionString': version,
    }))
    resource = app / 'Contents/Resources/frontend/index.html'
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text('synthetic resource ' + version)
    helper = app / 'Contents/Resources/helper/fathom-helper'
    helper.mkdir(parents=True, exist_ok=True)
    entry = helper / 'fathom-helper'
    entry.write_text('#!/usr/bin/env python3\nimport json,sys\nassert sys.argv[1:]==["--version"]\nprint(json.dumps('+repr({'service':'fathom','protocol_version':1,'version':version})+'))\n')
    entry.chmod(0o755)
    return helper


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


@pytest.fixture
def bundle(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    paths = upgrade.UpgradePaths(runtime, runtime/'data/fathom.db',
        runtime/'data/fathom.db.scan.lock', runtime/'helper-instance.json',
        runtime/'data/fathom.db.upgrade-journal.json')
    conn = db.connect(paths.db_path)
    conn.close()
    app = tmp_path / 'installed/Fathom.app'
    helper = write_app(app, '0.4.2')
    return paths, app, helper


def cli(paths, *args):
    env = {k:v for k,v in os.environ.items() if not k.startswith('FATHOM_')}
    scan = paths.runtime_dir / 'synthetic-scan'
    scan.mkdir(exist_ok=True)
    env.update(PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE='1',
               FATHOM_SCAN_ROOT=str(scan), FATHOM_RESOURCE_DIR=str(REPO), FATHOM_PORT='51981')
    proc = subprocess.run([sys.executable, '-B', '-m', 'fathom', '--runtime-dir',
        str(paths.runtime_dir), '--runtime-mode', 'release', *args], cwd=REPO,
        env=env, text=True, capture_output=True, timeout=30)
    payload = json.loads(proc.stdout.splitlines()[-1])
    return proc.returncode, payload


def prepare(paths, helper):
    code, result = cli(paths, 'upgrade-prepare', '--from', '0.4.2', '--to', '0.4.3',
                       '--helper-dir', str(helper), '--expected-helper-pid', '0')
    assert code == 0 and result['ok'], result
    return upgrade.read_restore_manifest(paths)


def test_complete_replacement_real_cli_restores_every_component(bundle):
    paths, app, helper = bundle
    old = hashes(app)
    manifest = prepare(paths, helper)
    assert manifest['manifest_version'] == 2
    shutil.rmtree(app)
    write_app(app, '0.4.3')
    (app/'Contents/Resources/new-only').write_text('new-only')
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 0 and result['ok'], result
    assert hashes(app) == old
    assert not paths.journal_path.exists()
    assert (paths.runtime_dir/'upgrade-restore/manifest.json').exists()


def test_same_size_material_corruption_preserves_install_and_journal(bundle):
    paths, app, helper = bundle
    prepare(paths, helper)
    write_app(app, '0.4.3')
    installed = hashes(app)
    source = paths.runtime_dir/'upgrade-restore/app/Contents/MacOS/fathom-desktop'
    source.write_bytes(b'X' * source.stat().st_size)
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 1 and result['kind'] == 'restore_failed'
    assert paths.journal_path.exists() and hashes(app) == installed
    assert source.exists()


def test_legacy_helper_only_app_material_refuses_false_success(bundle):
    paths, app, helper = bundle
    manifest = prepare(paths, helper)
    area = paths.runtime_dir/'upgrade-restore'
    shutil.copytree(area/'app/Contents/Resources/helper/fathom-helper', area/'helper')
    for key in ['manifest_version','restore_scope','app_dir','app_fingerprint']:
        manifest.pop(key)
    (area/'manifest.json').write_text(json.dumps(manifest))
    shutil.rmtree(area/'app')
    write_app(app, '0.4.3')
    installed = hashes(app)
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 1 and result['kind'] == 'restore_failed'
    assert 'DMG' in result['error'] and paths.journal_path.exists()
    assert hashes(app) == installed


def test_complete_app_budget_counts_non_helper_resources(bundle):
    paths, app, helper = bundle
    (app/'Contents/Resources/large').write_bytes(b'x' * 4096)
    backup = paths.db_path
    helper_bytes = upgrade._tree_stats(helper)[1]
    budget = int((helper_bytes + backup.stat().st_size) * 2) + 1
    with pytest.raises(upgrade.UpgradeRefused, match='磁盘空间不足'):
        upgrade.save_old_bundle(paths, helper_dir=helper, backup_path=backup,
             from_version='0.4.2', to_version='0.4.3', txn_id='a'*32,
             disk_free=lambda: budget)
    assert not (paths.runtime_dir/'upgrade-restore').exists()


@pytest.mark.parametrize('case', ['wrong_id', 'wrong_layout', 'external_link', 'runtime_in_app'])
def test_app_boundary_refused(bundle, case):
    paths, app, helper = bundle
    if case == 'wrong_id':
        info = app/'Contents/Info.plist'
        value = plistlib.loads(info.read_bytes()); value['CFBundleIdentifier'] = 'other.app'
        info.write_bytes(plistlib.dumps(value))
    elif case == 'wrong_layout':
        helper = app/'arbitrary/helper'
        helper.mkdir(parents=True); (helper/'fathom-helper').write_text('bad')
    elif case == 'external_link':
        (app/'Contents/Resources/outside').symlink_to(paths.db_path)
    else:
        paths = upgrade.UpgradePaths(app/'runtime', paths.db_path, paths.lock_path,
                                     paths.instance_path, paths.journal_path)
    with pytest.raises((upgrade.RestoreFailed, OSError)):
        upgrade.save_old_bundle(paths, helper_dir=helper, backup_path=None,
             from_version='0.4.2', to_version='0.4.3', txn_id='a'*32)


def test_transaction_mismatch_preserved(bundle):
    paths, app, helper = bundle
    prepare(paths, helper)
    journal = json.loads(paths.journal_path.read_text()); journal['txn_id'] = 'b'*32
    paths.journal_path.write_text(json.dumps(journal))
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 1 and result['journal_preserved']
    assert json.loads(paths.journal_path.read_text()) == journal


def test_interrupted_swap_can_resume(bundle):
    paths, app, helper = bundle
    old = hashes(app)
    manifest = prepare(paths, helper)
    write_app(app, '0.4.3')
    trash = app.with_name(app.name+'.pre-restore-'+manifest['txn_id'])
    app.rename(trash)  # interruption after moving current installation aside
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 0 and result['ok'], result
    assert hashes(app) == old and not trash.exists()


def test_frozen_helper_relocated_before_replacing_app(bundle, monkeypatch):
    paths, app, helper = bundle
    prepare(paths, helper)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(helper/'fathom-helper'))
    class ExecRequested(Exception):
        pass
    calls = []
    def execute(entry, argv, env):
        assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        calls.append((entry, argv))
        raise ExecRequested()
    monkeypatch.setattr(os, 'execve', execute)
    with pytest.raises(ExecRequested):
        upgrade.relocate_rollback_helper(paths)
    executable, argv = calls[0]
    assert Path(executable).is_relative_to(paths.runtime_dir)
    assert hashes(Path(executable).parent) == hashes(helper)
    assert argv[1:] == sys.argv[1:]
    assert paths.journal_path.exists()


def test_missing_saved_area_does_not_clear_journal(bundle):
    paths, app, helper = bundle
    prepare(paths, helper)
    shutil.rmtree(paths.runtime_dir/'upgrade-restore')
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 1 and result['journal_preserved'] and paths.journal_path.exists()


def test_corrupt_new_install_can_be_recovered(bundle):
    paths, app, helper = bundle
    old = hashes(app)
    prepare(paths, helper)
    write_app(app, '0.4.3')
    (app/'Contents/Info.plist').write_bytes(b'broken plist')
    (app/'Contents/MacOS/fathom-desktop').unlink()
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 0 and result['ok'], result
    assert hashes(app) == old


def test_manifest_retarget_to_another_owned_app_is_rejected(bundle):
    paths, app, helper = bundle
    manifest = prepare(paths, helper)
    other = app.parent/'Other.app'
    other_helper = write_app(other, '0.4.3')
    before = hashes(other)
    manifest.update(app_dir=str(other), helper_dir=str(other_helper))
    (paths.runtime_dir/'upgrade-restore/manifest.json').write_text(json.dumps(manifest))
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 1 and result['journal_preserved']
    assert hashes(other) == before


def test_internal_symlinks_and_permissions_restored(bundle):
    paths, app, helper = bundle
    resource = app/'Contents/Resources/frontend/index.html'
    resource.chmod(0o640)
    link = app/'Contents/Resources/front-link'
    link.symlink_to('frontend/index.html')
    old = upgrade._bundle_fingerprint(app)
    prepare(paths, helper)
    shutil.rmtree(app)
    write_app(app, '0.4.3')
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 0 and result['ok'], result
    assert upgrade._bundle_fingerprint(app) == old


def test_unknown_manifest_version_fail_closed(bundle):
    paths, app, helper = bundle
    manifest = prepare(paths, helper)
    manifest['manifest_version'] = 999
    (paths.runtime_dir/'upgrade-restore/manifest.json').write_text(json.dumps(manifest))
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 1 and result['journal_preserved']


def test_restore_copy_failure_keeps_original_and_can_retry(bundle, monkeypatch):
    paths, app, helper = bundle
    old = hashes(app)
    prepare(paths, helper)
    write_app(app, '0.4.3')
    installed = hashes(app)
    copytree = shutil.copytree
    def failed_copy(source, target, *args, **kwargs):
        if '.restore-stage-' in str(target):
            Path(target).mkdir()
            (Path(target)/'partial').write_text('partial')
            raise OSError('synthetic interrupted copy')
        return copytree(source, target, *args, **kwargs)
    monkeypatch.setattr(shutil, 'copytree', failed_copy)
    result = upgrade.UpgradeCoordinator(paths).rollback(reason='test', own_journal_only=False)
    assert not result['ok'] and paths.journal_path.exists()
    assert hashes(app) == installed
    monkeypatch.setattr(shutil, 'copytree', copytree)
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 0 and result['ok'] and hashes(app) == old


def test_restore_install_volume_budget_preserves_original(bundle, monkeypatch):
    paths, app, helper = bundle
    prepare(paths, helper)
    write_app(app, '0.4.3')
    installed = hashes(app)
    monkeypatch.setattr(upgrade, '_disk_free_bytes', lambda _: 0)
    result = upgrade.UpgradeCoordinator(paths).rollback(reason='test', own_journal_only=False)
    assert not result['ok'] and paths.journal_path.exists()
    assert hashes(app) == installed


def test_relocation_rejects_symlinked_restore_area(bundle, monkeypatch):
    paths, app, helper = bundle
    prepare(paths, helper)
    area = paths.runtime_dir/'upgrade-restore'
    outside = paths.runtime_dir.parent/'outside-area'
    area.rename(outside)
    area.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(helper/'fathom-helper'))
    with pytest.raises(upgrade.RestoreFailed, match='符号链接'):
        upgrade.relocate_rollback_helper(paths)
    assert not (outside/'recovery-helper').exists()
    assert paths.journal_path.exists()


@pytest.mark.parametrize('case', ['app-in-restore', 'helper-in-restore', 'runtime-in-app', 'app-in-runtime'])
def test_prepare_overlapping_source_preserves_everything(bundle, case):
    paths, app, helper = bundle
    area = paths.runtime_dir/'upgrade-restore'
    if case == 'runtime-in-app':
        runtime = app/'private-runtime'
        runtime.mkdir()
        paths = upgrade.UpgradePaths(runtime, runtime/'data/fathom.db',
            runtime/'data/fathom.db.scan.lock', runtime/'helper-instance.json',
            runtime/'data/fathom.db.upgrade-journal.json')
        area = runtime/'upgrade-restore'
        area.mkdir()
    else:
        area.mkdir()
        if case in ('app-in-restore', 'app-in-runtime'):
            target = (area if case == 'app-in-restore' else paths.runtime_dir)/'Fathom.app'
            app.rename(target)
            app = target
            helper = app/'Contents/Resources/helper/fathom-helper'
        else:
            target = area/'standalone-helper'
            helper.rename(target)
            helper = target
    marker = area/'existing-material'
    marker.write_text('must survive refused prepare')
    source = app if case != 'helper-in-restore' else helper
    before = hashes(source)
    code, result = cli(paths, 'upgrade-prepare', '--from', '0.4.2', '--to', '0.4.3',
                       '--helper-dir', str(helper), '--expected-helper-pid', '0')
    assert code == 1 and result['kind'] == 'invalid_restore_paths', result
    assert hashes(source) == before and marker.read_text() == 'must survive refused prepare'
    assert not paths.journal_path.exists()


def test_standalone_external_relative_link_keeps_legacy_copy_contract(bundle):
    paths, app, original_helper = bundle
    helper = app.parent/'standalone-helper'
    shutil.copytree(original_helper, helper)
    shared = helper.parent/'shared'
    shared.mkdir()
    (shared/'resource').write_text('legacy shared bytes')
    (helper/'resource').symlink_to('../shared/resource')
    code, result = cli(paths, 'upgrade-prepare', '--from', '0.4.2', '--to', '0.4.3',
                       '--helper-dir', str(helper), '--expected-helper-pid', '0')
    assert code == 0 and result['ok'], result
    saved = paths.runtime_dir/'upgrade-restore/helper/resource'
    assert saved.is_file() and not saved.is_symlink()
    assert saved.read_text() == 'legacy shared bytes'
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 0 and result['ok'], result
    assert (helper/'resource').read_text() == 'legacy shared bytes'
    assert not paths.journal_path.exists()


@pytest.mark.parametrize('kind', ['dangling', 'external'])
def test_untrusted_new_app_link_does_not_block_complete_restore(bundle, kind):
    paths, app, helper = bundle
    old = hashes(app)
    prepare(paths, helper)
    write_app(app, '0.4.3')
    outside = app.parent/'outside-resource'
    outside.write_text('outside must stay unchanged')
    (app/'Contents/Resources/bad-link').symlink_to(
        'missing-resource' if kind == 'dangling' else str(outside))
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 0 and result['ok'], result
    assert hashes(app) == old and outside.read_text() == 'outside must stay unchanged'
    assert not paths.journal_path.exists()


@pytest.mark.parametrize('field', ['db_path', 'lock_path', 'instance_path', 'journal_path'])
def test_prepare_protects_source_from_related_state_paths(bundle, field):
    from dataclasses import replace
    paths, app, helper = bundle
    paths = replace(paths, **{field: app/('protected-'+field)})
    old = hashes(app)
    result = upgrade.UpgradeCoordinator(paths, helper_dir=helper,
        from_version='0.4.2', to_version='0.4.3').run_prepare()
    assert not result['ok'] and result['kind'] == 'invalid_restore_paths'
    assert result['steps_done'] == [] and result['rollback_actions'] == []
    assert hashes(app) == old and not (paths.runtime_dir/'upgrade-restore').exists()


def test_direct_save_rejects_overlap_without_removing_existing_material(bundle):
    paths, app, helper = bundle
    area = paths.runtime_dir/'upgrade-restore'
    area.mkdir()
    target = area/'Fathom.app'
    app.rename(target)
    helper = target/'Contents/Resources/helper/fathom-helper'
    marker = area/'existing-material'
    marker.write_text('keep this')
    old = hashes(target)
    with pytest.raises(upgrade.RestoreFailed, match='重叠'):
        upgrade.save_old_bundle(paths, helper_dir=helper, backup_path=None,
            from_version='0.4.2', to_version='0.4.3', txn_id='a'*32)
    assert hashes(target) == old and marker.read_text() == 'keep this'


def test_path_prefix_sibling_is_allowed(bundle):
    paths, app, helper = bundle
    sibling = paths.runtime_dir.with_name(paths.runtime_dir.name+'-neighbor')
    sibling.mkdir()
    target = sibling/'Fathom.app'
    app.rename(target)
    helper = target/'Contents/Resources/helper/fathom-helper'
    prepare(paths, helper)
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 0 and result['ok'], result


def test_standalone_link_dependency_inside_restore_area_is_protected(bundle):
    paths, app, original = bundle
    helper = app.parent/'standalone-helper'
    shutil.copytree(original, helper)
    area = paths.runtime_dir/'upgrade-restore'
    area.mkdir()
    material = area/'referenced-resource'
    material.write_text('must not be removed')
    (helper/'resource').symlink_to(material)
    code, result = cli(paths, 'upgrade-prepare', '--from', '0.4.2', '--to', '0.4.3',
                       '--helper-dir', str(helper), '--expected-helper-pid', '0')
    assert code == 1 and result['kind'] == 'invalid_restore_paths', result
    assert material.read_text() == 'must not be removed' and not paths.journal_path.exists()


def tree_state_without_following_links(root):
    """拒绝前后比较链接本身与文件字节，循环树也可检查。"""
    result = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            item = Path(current)/name
            key = item.relative_to(root).as_posix()
            if item.is_symlink():
                result[key] = ('link', os.readlink(item))
            elif item.is_dir():
                result[key] = ('dir', item.stat().st_mode)
            else:
                result[key] = ('file', item.stat().st_mode, item.read_bytes())
    return result


@pytest.mark.parametrize('existing_journal', [False, True])
@pytest.mark.parametrize('kind', ['file', 'directory', 'intermediate-link', 'parent-link'])
def test_transitive_dependency_rejected_before_any_prepare_side_effect(bundle, kind, existing_journal):
    paths, app, original = bundle
    helper = app.parent/'standalone-helper'
    shutil.copytree(original, helper)
    shared = app.parent/'shared'
    shared.mkdir()
    area = paths.runtime_dir/'upgrade-restore'
    area.mkdir()
    (area/'existing-material').write_text('keep previous restore bytes')
    if kind == 'directory':
        target = area/'protected-source'
        target.mkdir()
        (target/'resource').write_text('must survive')
    elif kind in ('intermediate-link', 'parent-link'):
        outside = app.parent/'outside'
        outside.mkdir()
        (outside/'resource').write_text('safe final bytes, unsafe intermediate hop')
        target = area/'protected-link'
        target.symlink_to(outside if kind == 'parent-link' else outside/'resource')
        if kind == 'parent-link':
            target = target/'resource'
    else:
        target = area/'protected-source'
        target.write_text('must survive')
    (shared/'nested').symlink_to(target)
    (helper/'shared').symlink_to(shared, target_is_directory=True)
    (paths.runtime_dir/'synthetic-scan').mkdir()
    if existing_journal:
        paths.journal_path.write_text('existing journal bytes must survive')
    before = tree_state_without_following_links(paths.runtime_dir.parent)
    code, result = cli(paths, 'upgrade-prepare', '--from', '0.4.2', '--to', '0.4.3',
                       '--helper-dir', str(helper), '--expected-helper-pid', '0')
    assert code == 1 and result['kind'] == 'invalid_restore_paths', result
    assert result['steps_done'] == [] and result['rollback_actions'] == []
    assert tree_state_without_following_links(paths.runtime_dir.parent) == before
    assert paths.journal_path.exists() == existing_journal


@pytest.mark.parametrize('kind', ['directory-self', 'directory-pair', 'symlink-pair'])
def test_transitive_link_cycle_rejected_without_partial_transaction(bundle, kind):
    paths, app, original = bundle
    helper = app.parent/'standalone-helper'
    shutil.copytree(original, helper)
    shared = app.parent/'shared'
    shared.mkdir()
    (helper/'shared').symlink_to(shared, target_is_directory=True)
    if kind == 'directory-self':
        (shared/'loop').symlink_to(shared, target_is_directory=True)
    elif kind == 'directory-pair':
        other = app.parent/'other'
        other.mkdir()
        (shared/'other').symlink_to(other, target_is_directory=True)
        (other/'back').symlink_to(shared, target_is_directory=True)
    else:
        (shared/'a').symlink_to('b')
        (shared/'b').symlink_to('a')
    area = paths.runtime_dir/'upgrade-restore'
    area.mkdir()
    (area/'existing-material').write_text('preserve material')
    (paths.runtime_dir/'synthetic-scan').mkdir()
    before = tree_state_without_following_links(paths.runtime_dir.parent)
    code, result = cli(paths, 'upgrade-prepare', '--from', '0.4.2', '--to', '0.4.3',
                       '--helper-dir', str(helper), '--expected-helper-pid', '0')
    assert code == 1 and result['kind'] == 'invalid_restore_paths', result
    assert '循环' in result['error']
    assert result['steps_done'] == [] and result['rollback_actions'] == []
    assert tree_state_without_following_links(paths.runtime_dir.parent) == before
    assert not paths.journal_path.exists()


@pytest.mark.parametrize('same_prefix', [False, True])
def test_safe_transitive_directory_links_and_repeated_alias_keep_legacy_copy(bundle, same_prefix):
    paths, app, original = bundle
    helper = app.parent/'standalone-helper'
    shutil.copytree(original, helper)
    shared = app.parent/'shared'
    shared.mkdir()
    outside = (paths.runtime_dir.with_name(paths.runtime_dir.name+'-neighbor')
               if same_prefix else app.parent/'outside')
    outside.mkdir()
    (outside/'resource').write_text('safe transitive bytes')
    (shared/'nested').symlink_to(outside, target_is_directory=True)
    (helper/'shared').symlink_to(shared, target_is_directory=True)
    (helper/'alias').symlink_to(shared, target_is_directory=True)
    source_before = tree_state_without_following_links(app.parent)
    external_before = tree_state_without_following_links(outside)
    prepare(paths, helper)
    assert tree_state_without_following_links(app.parent) == source_before
    assert tree_state_without_following_links(outside) == external_before
    saved = paths.runtime_dir/'upgrade-restore/helper'
    manifest = upgrade.read_restore_manifest(paths)
    assert upgrade._tree_stats(saved) == (manifest['helper_files'], manifest['helper_bytes'])
    for alias in ['shared', 'alias']:
        copied = saved/alias/'nested/resource'
        assert copied.read_text() == 'safe transitive bytes'
        assert not (saved/alias).is_symlink() and not copied.parent.is_symlink()
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 0 and result['ok'], result
    for alias in ['shared', 'alias']:
        assert (helper/alias/'nested/resource').read_text() == 'safe transitive bytes'
    assert tree_state_without_following_links(outside) == external_before
    assert not paths.journal_path.exists()



def test_standalone_directory_link_budget_counts_expanded_copy(bundle):
    paths, app, original = bundle
    helper = app.parent/'standalone-helper'
    shutil.copytree(original, helper)
    shared = app.parent/'shared'
    shared.mkdir()
    (shared/'resource').write_bytes(b'x' * 4096)
    (helper/'shared').symlink_to(shared, target_is_directory=True)
    area = paths.runtime_dir/'upgrade-restore'
    area.mkdir()
    (area/'existing-material').write_text('keep old bytes')
    before = tree_state_without_following_links(paths.runtime_dir.parent)
    old_budget = int(upgrade._tree_stats(helper)[1] * upgrade.RESTORE_DISK_BUDGET_FACTOR) + 1
    with pytest.raises(upgrade.UpgradeRefused, match='磁盘空间不足'):
        upgrade.save_old_bundle(paths, helper_dir=helper, backup_path=None,
            from_version='0.4.2', to_version='0.4.3', txn_id='a'*32,
            disk_free=lambda: old_budget)
    assert tree_state_without_following_links(paths.runtime_dir.parent) == before


@pytest.mark.parametrize('existing_journal', [False, True])
@pytest.mark.parametrize('relative', [False, True])
def test_ordinary_directory_hop_rejected_before_prepare(bundle, existing_journal, relative):
    paths, app, original = bundle
    helper = app.parent/'standalone-helper'
    shutil.copytree(original, helper)
    area = paths.runtime_dir/'upgrade-restore'
    hop = area/'hop'
    hop.mkdir(parents=True)
    (area/'existing-material').write_text('keep old recovery material')
    shared = app.parent/'shared'
    shared.mkdir()
    (shared/'resource').write_text('old source bytes')
    prefix = os.path.relpath(hop, helper) if relative else str(hop)
    link = helper/'resource'
    link.symlink_to(prefix+'/../../../installed/shared/resource')
    assert link.read_text() == 'old source bytes'
    assert link.resolve() == (shared/'resource').resolve()
    (paths.runtime_dir/'synthetic-scan').mkdir()
    if existing_journal:
        paths.journal_path.write_text('previous journal must remain byte exact')
    before = tree_state_without_following_links(paths.runtime_dir.parent)
    code, result = cli(paths, 'upgrade-prepare', '--from', '0.4.2', '--to', '0.4.3',
                       '--helper-dir', str(helper), '--expected-helper-pid', '0')
    assert code == 1 and result['kind'] == 'invalid_restore_paths', result
    assert result['steps_done'] == [] and result['rollback_actions'] == []
    assert tree_state_without_following_links(paths.runtime_dir.parent) == before
    assert link.read_text() == 'old source bytes'
    assert paths.journal_path.exists() == existing_journal


@pytest.mark.parametrize('relative', [False, True])
def test_safe_ordinary_hop_and_ancestors_allow_parent_traversal(bundle, relative):
    paths, app, original = bundle
    helper = app.parent/'standalone-helper'
    shutil.copytree(original, helper)
    shared = app.parent/'shared'
    hop = shared/'hop'
    hop.mkdir(parents=True)
    (shared/'resource').write_text('safe parent traversal bytes')
    # Every absolute source crosses / and the common ancestor of runtime and
    # installed. Only the final resource is copied, not those ancestor trees.
    prefix = os.path.relpath(hop, helper) if relative else str(hop)
    for name in ['resource', 'alias']:
        (helper/name).symlink_to(prefix+'/../resource')
        assert (helper/name).read_text() == 'safe parent traversal bytes'
    before = tree_state_without_following_links(app.parent)
    prepare(paths, helper)
    assert tree_state_without_following_links(app.parent) == before
    for name in ['resource', 'alias']:
        saved = paths.runtime_dir/'upgrade-restore/helper'/name
        assert not saved.is_symlink()
        assert saved.read_text() == 'safe parent traversal bytes'
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 0 and result['ok'], result
    for name in ['resource', 'alias']:
        assert (helper/name).read_text() == 'safe parent traversal bytes'
    assert hop.is_dir() and not paths.journal_path.exists()


def test_ordinary_hop_through_explicit_backup_directory_is_protected(bundle):
    paths, app, original = bundle
    helper = app.parent/'standalone-helper'
    shutil.copytree(original, helper)
    backup = app.parent/'separate-backup'
    hop = backup/'hop'
    hop.mkdir(parents=True)
    (backup/'existing-backup').write_text('keep backup bytes')
    (app.parent/'resource').write_text('safe final bytes')
    (helper/'resource').symlink_to(str(hop)+'/../../resource')
    assert (helper/'resource').read_text() == 'safe final bytes'
    before = tree_state_without_following_links(paths.runtime_dir.parent)
    with pytest.raises(upgrade.RestoreFailed, match='路径解析依赖'):
        upgrade.save_old_bundle(paths, helper_dir=helper, backup_path=backup,
            from_version='0.4.2', to_version='0.4.3', txn_id='a'*32)
    assert tree_state_without_following_links(paths.runtime_dir.parent) == before


def test_copied_ancestor_subtree_still_rejects_protected_descendant(bundle):
    paths, app, original = bundle
    helper = app.parent/'standalone-helper'
    shutil.copytree(original, helper)
    (helper/'ancestor-tree').symlink_to(paths.runtime_dir.parent, target_is_directory=True)
    (paths.runtime_dir/'synthetic-scan').mkdir()
    before = tree_state_without_following_links(paths.runtime_dir.parent)
    code, result = cli(paths, 'upgrade-prepare', '--from', '0.4.2', '--to', '0.4.3',
                       '--helper-dir', str(helper), '--expected-helper-pid', '0')
    assert code == 1 and result['kind'] == 'invalid_restore_paths', result
    assert result['steps_done'] == [] and result['rollback_actions'] == []
    assert tree_state_without_following_links(paths.runtime_dir.parent) == before



@pytest.mark.parametrize('existing_journal', [False, True])
@pytest.mark.parametrize('kind', ['file', 'directory', 'intermediate-link'])
def test_app_link_hop_protected_before_any_prepare_side_effect(bundle, existing_journal, kind):
    paths, app, helper = bundle
    area = paths.runtime_dir/'upgrade-restore'
    hop = area/'hop'
    hop.mkdir(parents=True)
    (area/'existing-material').write_bytes(b'old recovery bytes\x00\xff')
    target = '../../../../runtime/upgrade-restore/hop/../../../installed/Fathom.app/Contents/Resources/frontend'
    if kind != 'directory':
        target += '/index.html'
    if kind == 'intermediate-link':
        # A protected symlink also must be checked, not only its final target.
        (area/'via-hop').symlink_to('hop')
        target = target.replace('/hop/', '/via-hop/')
    link = app/'Contents/Resources/front-link'
    link.symlink_to(target, target_is_directory=kind == 'directory')
    readable = link/'index.html' if kind == 'directory' else link
    assert readable.read_text() == 'synthetic resource 0.4.2'
    assert readable.resolve().is_relative_to(app)
    upgrade._bundle_fingerprint(app)  # The old final-target-only check accepts it.
    (paths.runtime_dir/'synthetic-scan').mkdir()
    if existing_journal:
        paths.journal_path.write_bytes(b'old journal\x00\xff')
    before = tree_state_without_following_links(paths.runtime_dir.parent)
    code, result = cli(paths, 'upgrade-prepare', '--from', '0.4.2', '--to', '0.4.3',
                       '--helper-dir', str(helper), '--expected-helper-pid', '0')
    assert code == 1 and result['kind'] == 'invalid_restore_paths', result
    assert result['steps_done'] == [] and result['rollback_actions'] == []
    assert tree_state_without_following_links(paths.runtime_dir.parent) == before
    assert readable.read_text() == 'synthetic resource 0.4.2'
    assert paths.journal_path.exists() == existing_journal


def test_app_safe_parent_and_framework_links_survive_complete_replacement(bundle):
    paths, app, helper = bundle
    resources = app/'Contents/Resources'
    (resources/'frontend/hop').mkdir()
    for name in ['front-link', 'front-alias']:
        (resources/name).symlink_to('frontend/hop/../index.html')
    framework = app/'Contents/Frameworks/Synthetic.framework'
    version = framework/'Versions/A'
    (version/'Resources').mkdir(parents=True)
    (version/'Resources/data').write_text('old framework resource')
    (version/'Synthetic').write_text('old framework binary')
    (framework/'Versions/Current').symlink_to('A', target_is_directory=True)
    (framework/'Resources').symlink_to('Versions/Current/Resources', target_is_directory=True)
    (framework/'Synthetic').symlink_to('Versions/Current/Synthetic')
    (framework/'ResourceAlias').symlink_to('Resources', target_is_directory=True)
    old = upgrade._bundle_fingerprint(app)
    prepare(paths, helper)
    assert upgrade._bundle_fingerprint(app) == old
    saved = paths.runtime_dir/'upgrade-restore/app'
    assert upgrade._bundle_fingerprint(saved) == old
    assert (saved/'Contents/Frameworks/Synthetic.framework/Resources').is_symlink()
    assert (saved/'Contents/Resources/front-alias').is_symlink()
    shutil.rmtree(app)
    write_app(app, '0.4.3')
    code, result = cli(paths, 'upgrade-rollback')
    assert code == 0 and result['ok'], result
    assert upgrade._bundle_fingerprint(app) == old
    assert (framework/'Resources/data').read_text() == 'old framework resource'
    assert (framework/'Synthetic').read_text() == 'old framework binary'
    assert (resources/'front-alias').read_text() == 'synthetic resource 0.4.2'
    assert not paths.journal_path.exists()


def test_app_symlink_cycle_rejected_before_prepare_side_effects(bundle):
    paths, app, helper = bundle
    resources = app/'Contents/Resources'
    (resources/'cycle-a').symlink_to('cycle-b')
    (resources/'cycle-b').symlink_to('cycle-a')
    area = paths.runtime_dir/'upgrade-restore'
    area.mkdir()
    (area/'existing-material').write_bytes(b'keep old material')
    (paths.runtime_dir/'synthetic-scan').mkdir()
    before = tree_state_without_following_links(paths.runtime_dir.parent)
    code, result = cli(paths, 'upgrade-prepare', '--from', '0.4.2', '--to', '0.4.3',
                       '--helper-dir', str(helper), '--expected-helper-pid', '0')
    assert code == 1 and result['kind'] == 'invalid_restore_paths', result
    assert '循环' in result['error']
    assert result['steps_done'] == [] and result['rollback_actions'] == []
    assert tree_state_without_following_links(paths.runtime_dir.parent) == before
