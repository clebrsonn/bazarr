"""Native provider migration regressions; all HTTP is requests-mocked."""
from xml.sax.saxutils import escape

import pytest

from subliminal_patch.core import Episode, Movie
from subliminal_patch.providers.animetosho_xyz import AnimeToshoXYZProvider
from subzero.language import Language

BASE = 'https://feed.animetosho.net'
TITLE = 'Some.Show.S02E03.1080p.WEB-DL.mkv'
URL = 'https://storage.animetosho.net/attachments/123/1.xz'


def episode(**kwargs):
    return Episode(TITLE, 'Some Show', 2, 3, **kwargs)


def rss(*entries):
    return '<rss><channel>' + ''.join(
        f'<item><title>{escape(title)}</title><link>https://animetosho.net/view/release.n{id}</link>'
        f'<guid>https://animetosho.net/view/release.n{id}</guid></item>' for id, title in entries
    ) + '</channel></rss>'


def detail(requests_mock, id=123, title=TITLE, **extra):
    payload = {'id': id, 'torrent_name': title,
               'attachments': [{'id': 1, 'type': 'subtitle', 'url': URL,
                                'info': {'language_code': 'eng'}}]}
    payload.update(extra)
    requests_mock.get(f'{BASE}/json?show=torrent&id={id}', json=payload)
    return payload


def test_tvdb_episode_uses_net_torznab_rss_without_anidb(requests_mock):
    item = episode(series_tvdb_id=456)
    requests_mock.get(f'{BASE}/api?t=tvsearch&tvdbid=456&season=2&ep=3', text=rss((123, TITLE)))
    detail(requests_mock)
    with AnimeToshoXYZProvider() as provider:
        subtitles = provider.list_subtitles(item, {Language('eng')})
    assert len(subtitles) == 1
    assert subtitles[0].release_id == 123
    assert subtitles[0].provider_name == 'animetosho_xyz'
    assert {'series', 'season', 'episode'} <= subtitles[0].get_matches(item)
    assert requests_mock.call_count == 2


@pytest.mark.parametrize('tvdb_id', [None, 456])
def test_release_text_fallback_without_id_results(requests_mock, tvdb_id):
    item = episode(series_tvdb_id=tvdb_id)
    if tvdb_id:
        requests_mock.get(f'{BASE}/api?t=tvsearch&tvdbid=456&season=2&ep=3', text=rss())
    requests_mock.get(f'{BASE}/api?t=search&q=Some.Show.S02E03.1080p.WEB-DL', text=rss((123, TITLE)))
    detail(requests_mock)
    with AnimeToshoXYZProvider() as provider:
        subtitles = provider.list_subtitles(item, {Language('eng')})
    assert len(subtitles) == 1
    assert all('anidb' not in r.url and 'eid=' not in r.url for r in requests_mock.request_history)


def test_optional_series_tmdb_metadata_uses_tvsearch(requests_mock):
    item = episode()
    # Bazarr currently persists series_tvdb_id, not series_tmdb_id. Support this only
    # when an upstream metadata/refinement caller genuinely supplies the attribute.
    item.series_tmdb_id = 789
    requests_mock.get(f'{BASE}/api?t=tvsearch&tmdbid=789&season=2&ep=3', text=rss((123, TITLE)))
    detail(requests_mock)
    with AnimeToshoXYZProvider() as provider:
        assert len(provider.list_subtitles(item, {Language('eng')})) == 1
    assert requests_mock.call_count == 2


@pytest.mark.parametrize('metadata,query', [({'tmdb_id': 42}, 'tmdbid=42'),
                                          ({'imdb_id': 'tt1234567'}, 'imdbid=tt1234567')])
def test_movie_native_identifiers_and_movie_matches(requests_mock, metadata, query):
    item = Movie('Your.Name.2016.1080p.mkv', 'Your Name', year=2016)
    # database.py assigns these after native construction (the patched Video drops tmdb kwargs).
    for key, value in metadata.items():
        setattr(item, key, value)
    requests_mock.get(f'{BASE}/api?t=movie&{query}', text=rss((123, item.name)))
    detail(requests_mock, title=item.name)
    with AnimeToshoXYZProvider() as provider:
        assert provider.check(item)
        subtitles = provider.list_subtitles(item, {Language('eng')})
    assert len(subtitles) == 1
    assert 'title' in subtitles[0].get_matches(item)
    assert not {'series', 'season', 'episode'} & subtitles[0].get_matches(item)


@pytest.mark.parametrize('wrong', ['Some.Show.S01E03.mkv', 'Some.Show.S02E04.mkv',
                                   'Other.Show.S02E03.mkv', 'Some.Show.S02E03E04.mkv',
                                   'Some Show - 03.mkv'])
def test_refuses_wrong_or_ambiguous_episode_even_from_id_feed(requests_mock, wrong):
    item = episode(series_tvdb_id=456)
    requests_mock.get(f'{BASE}/api', text=rss((123, wrong)))
    detail(requests_mock, title=wrong)
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(item, {Language('eng')}) == []


def test_batch_uses_only_matching_file_and_scores_its_release(requests_mock):
    item = episode(series_tvdb_id=456, release_group='Good', resolution='1080p')
    requests_mock.get(f'{BASE}/api', text=rss((123, 'Some Show Season 2 Batch')))
    track = {'id': 1, 'type': 'subtitle', 'url': URL, 'info': {'language_code': 'eng'}}
    detail(requests_mock, title='Some Show Season 2 Batch', attachments=None, files=[
        {'filename': 'Some.Show.S02E04.720p-Bad.mkv', 'attachments': [dict(track, url=URL+'wrong')]},
        {'filename': 'Some.Show.S02E03.1080p-Good.mkv', 'attachments': [track]},
    ])
    with AnimeToshoXYZProvider() as provider:
        subtitles = provider.list_subtitles(item, {Language('eng')})
    assert len(subtitles) == 1
    assert subtitles[0].download_link == URL
    assert {'series', 'season', 'episode', 'resolution', 'release_group'} <= subtitles[0].get_matches(item)


def test_lookup_cache_uses_immutable_metadata_not_video_identity(requests_mock):
    requests_mock.get(f'{BASE}/api', text=rss((123, TITLE)))
    detail(requests_mock)
    with AnimeToshoXYZProvider() as provider:
        for _ in range(2):
            assert len(provider.list_subtitles(episode(series_tvdb_id=456), {Language('eng')})) == 1
    assert requests_mock.call_count == 2


def test_details_fanout_is_bounded_and_duplicate_ids_are_collapsed(requests_mock):
    # Positive IDs only; duplicate occurs before the result ceiling is reached.
    requests_mock.get(f'{BASE}/api', text=rss((1, TITLE), *[(i, TITLE) for i in range(1, 31)]))
    for i in range(1, 31):
        detail(requests_mock, id=i)
    with AnimeToshoXYZProvider() as provider:
        subtitles = provider.list_subtitles(episode(series_tvdb_id=456), {Language('eng')})
    assert len(subtitles) == 20
    assert requests_mock.call_count == 21


@pytest.mark.parametrize('info,expected', [
    ({'language_code': 'por', 'language': 'Portuguese[BR]', 'hearing_impaired': True}, Language('por', 'BR', hi=True)),
    ({'language_code': 'por', 'language': 'Portuguese', 'forced': 'false', 'hearing_impaired': 'false'}, Language('por')),
    ({'language_code': 'eng', 'forced': True, 'hearing_impaired': True}, Language('eng', forced=True, hi=True)),
])
def test_explicit_track_dispositions_and_hi_pool_support(requests_mock, info, expected):
    requests_mock.get(f'{BASE}/api', text=rss((123, TITLE)))
    detail(requests_mock, attachments=[{'id': 1, 'type': 'subtitle', 'url': URL, 'info': info}])
    assert expected in AnimeToshoXYZProvider.languages
    with AnimeToshoXYZProvider() as provider:
        subtitles = provider.list_subtitles(episode(series_tvdb_id=456), {expected})
    assert len(subtitles) == 1
    assert subtitles[0].language == expected
    assert subtitles[0].hearing_impaired is bool(expected.hi)


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://127.0.0.1/secret',
                                 'https://example.com/track.xz',
                                 'https://user:password@storage.animetosho.net/track.xz',
                                 'https://storage.animetosho.net:9999/track.xz'])
def test_untrusted_attachment_urls_are_not_offered(requests_mock, url):
    requests_mock.get(f'{BASE}/api', text=rss((123, TITLE)))
    detail(requests_mock, attachments=[{'id': 1, 'type': 'subtitle', 'url': url,
                                      'info': {'language_code': 'eng'}}])
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(episode(series_tvdb_id=456), {Language('eng')}) == []


def test_download_does_not_follow_untrusted_redirect(requests_mock):
    from subliminal.exceptions import ProviderError
    from subliminal_patch.providers.animetosho_xyz import AnimeToshoXYZSubtitle
    requests_mock.get(URL, status_code=302, headers={'Location': 'https://example.com/track'})
    subtitle = AnimeToshoXYZSubtitle(Language('eng'), URL, {}, TITLE, 123)
    with AnimeToshoXYZProvider() as provider, pytest.raises(ProviderError):
        provider.download_subtitle(subtitle)
    assert requests_mock.call_count == 1


def test_id_candidates_without_usable_tracks_fall_back_to_release(requests_mock):
    requests_mock.get(f'{BASE}/api?t=tvsearch&tvdbid=456&season=2&ep=3', text=rss((124, TITLE)))
    detail(requests_mock, id=124, attachments=[])
    requests_mock.get(f'{BASE}/api?t=search&q=Some.Show.S02E03.1080p.WEB-DL', text=rss((123, TITLE)))
    detail(requests_mock)
    with AnimeToshoXYZProvider() as provider:
        subtitles = provider.list_subtitles(episode(series_tvdb_id=456), {Language('eng')})
    assert len(subtitles) == 1
    assert subtitles[0].release_id == 123


def test_opaque_release_falls_back_to_title_and_native_episode(requests_mock):
    item = episode()
    item.name = '/not-a-real-library/opaque.mkv'
    requests_mock.get(f'{BASE}/api?t=search&q=opaque', text=rss())
    requests_mock.get(f'{BASE}/api?t=search&q=Some Show S02E03', text=rss((123, TITLE)))
    detail(requests_mock)
    with AnimeToshoXYZProvider() as provider:
        assert len(provider.list_subtitles(item, {Language('eng')})) == 1


def test_recorded_official_rss_numeric_view_ids_and_net_json_details(requests_mock, data):
    import json
    from pathlib import Path
    requests_mock.get(f'{BASE}/api', content=(Path(data) / 'animetosho_net_tvsearch_response.xml').read_bytes())
    payload = json.loads((Path(data) / 'animetosho_net_detail_response.json').read_text())
    requests_mock.get(f'{BASE}/json?show=torrent&id=695664', json=payload)
    item = Episode('One.Piece.S01E1180.mkv', 'One Piece', 1, 1180)
    with AnimeToshoXYZProvider() as provider:
        entries = provider._search_entries({'t': 'tvsearch', 'tmdbid': 37854, 'season': 1, 'ep': 1180})
        assert len(entries) == 20
        assert entries[0]['id'] == 695664
        subtitles = provider._get_series(entries[:1], item, set())
    assert {Language('por'), Language('por', 'BR'), Language('eng')} <= {s.language for s in subtitles}
    assert all({'series', 'season', 'episode'} <= s.get_matches(item) for s in subtitles)


@pytest.mark.parametrize('xml', [b'<rss>', b'<!DOCTYPE rss [<!ENTITY test "unsafe">]><rss><channel>&test;</channel></rss>',
                                 '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE rss><rss/>'.encode('utf-16')])
def test_malformed_or_dtd_rss_is_a_provider_error(requests_mock, xml):
    from subliminal.exceptions import ProviderError
    requests_mock.get(f'{BASE}/api', content=xml)
    with AnimeToshoXYZProvider() as provider, pytest.raises(ProviderError):
        provider.list_subtitles(episode(series_tvdb_id=456), {Language('eng')})
    assert requests_mock.call_count == 1


@pytest.mark.parametrize('info', [{}, {'language_code': 'invalid'}, {'language_code': None}])
def test_unidentified_track_language_is_not_invented_as_english(requests_mock, info):
    requests_mock.get(f'{BASE}/api', text=rss((123, TITLE)))
    detail(requests_mock, attachments=[{'id': 1, 'type': 'subtitle', 'url': URL, 'info': info}])
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(episode(series_tvdb_id=456), {Language('eng')}) == []


def test_corrupt_xz_download_is_a_provider_error(requests_mock):
    import lzma
    from subliminal.exceptions import ProviderError
    from subliminal_patch.providers.animetosho_xyz import AnimeToshoXYZSubtitle
    requests_mock.get(URL, content=lzma.compress(b'tiny offline subtitle')[:-5])
    subtitle = AnimeToshoXYZSubtitle(Language('eng'), URL, {}, TITLE, 123)
    with AnimeToshoXYZProvider() as provider, pytest.raises(ProviderError):
        provider.download_subtitle(subtitle)


@pytest.mark.parametrize('status', [404, 429, 500])
def test_http_failure_stops_without_retry_or_text_fallback(requests_mock, status):
    from subliminal.exceptions import ProviderError
    requests_mock.get(f'{BASE}/api', status_code=status)
    with AnimeToshoXYZProvider() as provider, pytest.raises(ProviderError):
        provider.list_subtitles(episode(series_tvdb_id=456), {Language('eng')})
    assert requests_mock.call_count == 1


def test_http_timeout_is_a_native_provider_error(requests_mock):
    from requests.exceptions import Timeout
    from subliminal.exceptions import ProviderError
    requests_mock.get(f'{BASE}/api', exc=Timeout('offline fixture'))
    with AnimeToshoXYZProvider() as provider, pytest.raises(ProviderError):
        provider.list_subtitles(episode(series_tvdb_id=456), {Language('eng')})


def test_native_xz_download_decodes_tiny_offline_subtitle(requests_mock):
    import lzma
    from subliminal_patch.providers.animetosho_xyz import AnimeToshoXYZSubtitle
    content = b'1\n00:00:01,000 --> 00:00:02,000\nOffline fixture\n'
    requests_mock.get(URL, content=lzma.compress(content))
    subtitle = AnimeToshoXYZSubtitle(Language('eng'), URL, {}, TITLE, 123)
    with AnimeToshoXYZProvider() as provider:
        assert provider.download_subtitle(subtitle) is subtitle
    assert subtitle.content == content


def test_empty_rss_results_are_cached_without_details(requests_mock):
    requests_mock.get(f'{BASE}/api', text=rss())
    with AnimeToshoXYZProvider() as provider:
        for _ in range(2):
            assert provider.list_subtitles(episode(series_tvdb_id=456), {Language('eng')}) == []
    assert requests_mock.call_count == 3


def test_top_level_batch_tracks_are_safely_refused(requests_mock):
    requests_mock.get(f'{BASE}/api', text=rss((123, TITLE)))
    detail(requests_mock, files=[{'filename': TITLE}, {'filename': 'Some.Show.S02E04.mkv'}])
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(episode(series_tvdb_id=456), {Language('eng')}) == []
