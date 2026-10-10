"""Agent 中心全局有界历史：纯读、跨区间、引擎关闭/快照淘汰仍可读。"""
import pytest
from fathom import api, config, db, analysis_manager as am
from tests.test_analysis_jobs_lookup import client, _insert_run, _setup, _execute, _wait_terminal_via_api

@pytest.fixture(autouse=True)
def isolate_manager(isolated, monkeypatch):
    monkeypatch.setattr(api, '_ANALYSIS_MANAGER', None)
    yield
    if api._ANALYSIS_MANAGER:
        api._ANALYSIS_MANAGER.shutdown()


def test_global_history_active_first_and_paged(client, isolated):
    from tests.test_analysis_manager import make_snapshots
    a, b = make_snapshots(isolated['scanroot'])
    _insert_run('old-failed', 'failed', a, b)
    _insert_run('other-active', 'running', a + 100, b + 100)
    _insert_run('new-cancelled', 'cancelled', a, b + 1)
    config.update_user_settings({'analysis': {'enabled': False}})
    response = client.get('/api/analysis/history?limit=1')
    assert response.status_code == 200, response.text
    first = response.json()
    assert first['total'] == 3 and first['has_more'] and first['next_offset'] == 1
    assert first['jobs'][0]['job_id'] == 'other-active'
    second = client.get('/api/analysis/history?limit=2&offset=1').json()
    assert second['total'] == 3 and not second['has_more'] and second['next_offset'] is None
    assert {j['job_id'] for j in second['jobs']} == {'old-failed', 'new-cancelled'}
    assert all('facts' not in j and 'result' not in j and 'prompt_text' not in j for j in first['jobs'] + second['jobs'])
    assert client.get(f'/api/analysis/jobs?a={a}&b={b}').json()['jobs'] == []
    assert client.get('/api/analysis/jobs').status_code == 400


def test_history_result_expired_disabled_and_revoked(client, isolated):
    setup = _setup(isolated)
    job = _execute(client, setup, 'agent-history')
    terminal = _wait_terminal_via_api(client, job['job_id'])
    assert terminal['status'] == 'succeeded'
    analysis_id = terminal['analysis_id']
    config.update_user_settings({'analysis': {'enabled': False}})
    conn = db.connect()
    conn.execute('DELETE FROM snapshots WHERE id=?', (setup['a'],))
    conn.commit(); conn.close()
    before = config.settings_path().read_bytes()
    history = client.get('/api/analysis/history').json()
    entry = history['jobs'][0]
    assert entry['analysis']['id'] == analysis_id and entry['analysis']['expired']
    original = client.get(f'/api/analyses/{analysis_id}')
    assert original.status_code == 200, original.text
    record = original.json()['analysis']
    assert record['expired'] and record['a']['snapshot_id'] == setup['a'] and record['facts']['entries']
    assert config.settings_path().read_bytes() == before
    assert client.delete(f'/api/analyses/{analysis_id}').status_code == 200
    revoked = client.get('/api/analysis/history').json()['jobs'][0]
    assert revoked['revoked'] and revoked['analysis'] is None
    assert client.get(f'/api/analyses/{analysis_id}').status_code == 404
    assert client.post('/api/analysis/previews', json={'a': setup['a'], 'b': setup['b']}).status_code == 403


@pytest.mark.parametrize('query', ['limit=0', 'limit=101', 'offset=-1'])
def test_history_bounds(client, query):
    assert client.get('/api/analysis/history?' + query).status_code == 400
