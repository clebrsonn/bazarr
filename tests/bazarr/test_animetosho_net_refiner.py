"""Functional AniDB selection regressions, without invoking a metadata service."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from subliminal_patch.core import Episode
from subzero.language import Language
from subliminal_patch.providers.animetosho_xyz import AnimeToshoXYZProvider
from subtitles.refiners import anidb


@pytest.fixture
def memory_region():
    original = anidb.region.__dict__.copy()
    anidb.region.configure('dogpile.cache.memory', replace_existing_backend=True)
    try:
        yield anidb.region
    finally:
        anidb.region.__dict__.clear()
        anidb.region.__dict__.update(original)


@pytest.mark.parametrize('enabled,expected', [(['animetosho_xyz'], True),
                                            (['animetosho'], True),
                                            (['jimaku'], True),
                                            (['tsukihime'], False),
                                            (['animetosho_xyz', 'animetosho'], True)])
def test_anidb_refinement_selection_preserves_other_providers(monkeypatch, enabled, expected):
    monkeypatch.setattr(anidb, 'settings', SimpleNamespace(general=SimpleNamespace(enabled_providers=enabled)))
    refine = Mock()
    monkeypatch.setattr(anidb, 'refine_anidb_ids', refine)
    item = Episode('Some.Show.S01E01.mkv', 'Some Show', 1, 1, series_tvdb_id=123)
    item.series_anidb_id = None
    anidb.refine_from_anidb(item.name, item)
    assert refine.called is expected


def test_migrated_provider_requires_anidb_api_credentials(monkeypatch, caplog):
    monkeypatch.setattr(anidb, 'settings', SimpleNamespace(
        general=SimpleNamespace(enabled_providers=['animetosho_xyz']),
        anidb=SimpleNamespace(api_client=None, api_client_ver=1)))
    client = Mock()
    client.get_show_information.return_value = (123, 3, 0)
    client.has_api_credentials = False
    monkeypatch.setattr(anidb, 'AniDBClient', lambda *args: client)
    item = Episode('Some.Show.S01E03.mkv', 'Some Show', 1, 3, series_tvdb_id=123)
    anidb.refine_anidb_ids(item)
    assert 'credentials are not fully set up' in caplog.text
    client.get_episode_ids.assert_not_called()


@pytest.mark.parametrize('stage', ['mapping', 'episode'])
@pytest.mark.parametrize('failure', ['http', 'timeout'])
def test_xyz_metadata_network_failure_returns_to_native_fallback(monkeypatch, requests_mock, caplog,
                                                                memory_region, stage, failure):
    from requests.exceptions import Timeout
    monkeypatch.setattr(anidb, 'settings', SimpleNamespace(
        general=SimpleNamespace(enabled_providers=['animetosho_xyz']),
        anidb=SimpleNamespace(api_client='offline-test', api_client_ver=1)))
    mappings_url = 'https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list.xml'
    mappings = '<anime-list><anime tvdbid="123" defaulttvdbseason="1" anidbid="100"/></anime-list>'
    options = {'status_code': 503} if failure == 'http' else {'exc': Timeout('offline fixture')}
    requests_mock.get(mappings_url, **(options if stage == 'mapping' else {'text': mappings}))
    requests_mock.get(anidb.api_url, **options)
    item = Episode('Some.Show.S01E03.mkv', 'Some Show', 1, 3, series_tvdb_id=123)
    anidb.refine_from_anidb(item.name, item)
    assert item.series_anidb_episode_id is None
    assert 'native' in caplog.text
    assert requests_mock.call_count == (1 if stage == 'mapping' else 2)
    assert 'offline-test' not in caplog.text
    requests_mock.reset_mock()
    requests_mock.get('https://feed.animetosho.net/api', text='<rss><channel/></rss>')
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(item, {Language('eng')}) == []
    assert requests_mock.request_history[0].qs == {
        't': ['tvsearch'], 'tvdbid': ['123'], 'season': ['1'], 'ep': ['3']}
    assert requests_mock.call_count == 3


def test_healthy_xyz_metadata_refinement_supplies_anidb_episode_id(monkeypatch, requests_mock, memory_region):
    monkeypatch.setattr(anidb, 'settings', SimpleNamespace(
        general=SimpleNamespace(enabled_providers=['animetosho_xyz']),
        anidb=SimpleNamespace(api_client='offline-test', api_client_ver=1)))
    requests_mock.get('https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list.xml',
                      text='<anime-list><anime tvdbid="123" defaulttvdbseason="1" anidbid="100"/></anime-list>')
    requests_mock.get(anidb.api_url, text='<anime><episodes><episode id="12345"><epno>3</epno>'
                      '</episode></episodes></anime>')
    item = Episode('Some.Show.S01E03.mkv', 'Some Show', 1, 3, series_tvdb_id=123)
    anidb.refine_from_anidb(item.name, item)
    assert item.series_anidb_episode_id == (100, 12345)
    assert requests_mock.call_count == 2
    assert memory_region.get(anidb.cache_key_refiner)['daily_api_request_count'] == 1
    requests_mock.reset_mock()
    requests_mock.get('https://feed.animetosho.net/api', text='<rss><channel/></rss>')
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(item, {Language('eng')}) == []
    assert requests_mock.request_history[0].qs == {'t': ['search'], 'eid': ['12345']}
    assert requests_mock.call_count == 3


@pytest.mark.parametrize('state,cached_episode,expected_eid', [
    ({'daily_api_request_count': 1}, True, True),
    ({'is_throttled': True, 'daily_api_request_count': 17}, True, False),
    ({'daily_api_request_count': 200}, True, True),
    ({'daily_api_request_count': 200}, False, False),
])
def test_native_refiner_cached_ids_respect_throttle_and_quota_without_http(
        monkeypatch, requests_mock, memory_region, state, cached_episode, expected_eid):
    monkeypatch.setattr(anidb, 'settings', SimpleNamespace(
        general=SimpleNamespace(enabled_providers=['animetosho_xyz']),
        anidb=SimpleNamespace(api_client='offline-test', api_client_ver=1)))
    mappings = '<anime-list><anime tvdbid="123" defaulttvdbseason="1" anidbid="100"/></anime-list>'
    requests_mock.get('https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list.xml',
                      text=mappings)
    requests_mock.get(anidb.api_url, text='<anime><episodes><episode id="12345"><epno>3</epno>'
                      '</episode></episodes></anime>')
    # Seed the real decorated mapping/episode caches with bounded synthetic responses.
    client = anidb.AniDBClient('offline-test', 1)
    assert client.get_show_information(123, 1, 3) == (100, 3, 0)
    if cached_episode:
        assert client.get_episode_ids(100, 3) == (100, 12345)
    memory_region.set(anidb.cache_key_refiner, dict(state))
    requests_mock.reset_mock()
    base = 'https://feed.animetosho.net'
    requests_mock.get(base + '/api', text='<rss><channel/></rss>')
    item = Episode('Some.Show.S01E03.mkv', 'Some Show', 1, 3, series_tvdb_id=123)
    anidb.refine_from_anidb(item.name, item)
    assert requests_mock.call_count == 0
    assert bool(item.series_anidb_episode_id) is expected_eid
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(item, {Language('eng')}) == []
    assert len(requests_mock.request_history) <= 3
    assert all(request.url.startswith(base) for request in requests_mock.request_history)
    assert ('eid' in requests_mock.request_history[0].qs) is expected_eid
    if cached_episode:
        assert memory_region.get(anidb.cache_key_refiner) == state
    else:
        assert memory_region.get(anidb.cache_key_refiner)['is_throttled'] is True


@pytest.mark.parametrize('enabled', [['animetosho'], ['jimaku']])
def test_other_providers_keep_metadata_network_exception_semantics(monkeypatch, enabled):
    from requests.exceptions import Timeout
    monkeypatch.setattr(anidb, 'settings', SimpleNamespace(general=SimpleNamespace(enabled_providers=enabled)))
    monkeypatch.setattr(anidb, 'refine_anidb_ids', Mock(side_effect=Timeout('offline')))
    item = Episode('Some.Show.S01E03.mkv', 'Some Show', 1, 3, series_tvdb_id=123)
    with pytest.raises(Timeout):
        anidb.refine_from_anidb(item.name, item)


def test_xyz_refiner_does_not_mask_programmer_error(monkeypatch):
    monkeypatch.setattr(anidb, 'settings', SimpleNamespace(
        general=SimpleNamespace(enabled_providers=['animetosho_xyz'])))
    monkeypatch.setattr(anidb, 'refine_anidb_ids', Mock(side_effect=TypeError('programmer bug')))
    item = Episode('Some.Show.S01E03.mkv', 'Some Show', 1, 3, series_tvdb_id=123)
    with pytest.raises(TypeError, match='programmer bug'):
        anidb.refine_from_anidb(item.name, item)
