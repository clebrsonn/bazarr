"""Synthetic offline regressions converted from independent native reproductions."""
import copy
import json

import pytest
from requests.exceptions import Timeout
from subliminal.exceptions import ProviderError
from subliminal_patch.core import Episode, Movie, SZProviderPool
from subliminal_patch.providers.animetosho_xyz import AnimeToshoXYZProvider, AnimeToshoXYZSubtitle
from subzero.language import Language
from subliminal_patch.score import ComputeScore
from test_animetosho_net import BASE, URL, rss

NAME = 'Some.Show.S02E03.mkv'


def attachment(**extra):
    return {'id': 1, 'type': 'subtitle', 'url': URL, 'info': {'language_code': 'eng'}, **extra}


def payload(id=123, name=NAME, **extra):
    return {'id': id, 'torrent_name': name, 'attachments': [attachment()], **extra}


@pytest.mark.parametrize('endpoint', ['api', 'json'])
@pytest.mark.parametrize('status', [301, 302, 307, 308])
def test_metadata_redirect_is_refused_without_following(requests_mock, endpoint, status):
    requests_mock.get(BASE + '/' + endpoint, status_code=status,
                      headers={'Location': 'http://127.0.0.1:8123/internal'})
    requests_mock.get('http://127.0.0.1:8123/internal',
                      **({'text': rss()} if endpoint == 'api' else {'json': payload()}))
    with AnimeToshoXYZProvider() as provider, pytest.raises(ProviderError, match='HTTP error'):
        if endpoint == 'api':
            provider._search_entries({'t': 'search', 'q': 'x'})
        else:
            provider._get_series([{'id': 123, 'title': NAME}], Episode(NAME, 'Some Show', 2, 3), set())
    assert requests_mock.call_count == 1
    assert requests_mock.request_history[0].url.startswith(BASE)


@pytest.mark.parametrize('fallback', ['release', 'title'])
def test_unusable_id_results_reserve_bounded_fallback(requests_mock, fallback):
    video = Episode(NAME, 'Some Show', 2, 3, series_tvdb_id=456)
    requests_mock.get(BASE + '/api?t=tvsearch&tvdbid=456&season=2&ep=3',
                      text=rss(*[(i, NAME) for i in range(1, 21)]))
    for i in range(1, 21):
        requests_mock.get(BASE + f'/json?show=torrent&id={i}', json=payload(id=i, attachments=[]))
    release_entries = [(123, NAME)] if fallback == 'release' else [(i, NAME) for i in range(30, 50)]
    requests_mock.get(BASE + '/api?t=search&q=Some.Show.S02E03', text=rss(*release_entries))
    for i in range(30, 50):
        requests_mock.get(BASE + f'/json?show=torrent&id={i}',
                          json=payload(id=i, attachments=[attachment(info={'language_code': 'jpn'})]))
    requests_mock.get(BASE + '/api?t=search&q=Some Show S02E03', text=rss((123, NAME)))
    requests_mock.get(BASE + '/json?show=torrent&id=123', json=payload())
    with AnimeToshoXYZProvider() as provider:
        for _ in range(2):
            subs = provider.list_subtitles(video, {Language('eng')})
            assert len(subs) == 1
            assert subs[0].release_id == 123
    details = [r for r in requests_mock.request_history if '/json?' in r.url]
    assert len(details) <= 20
    assert len(requests_mock.request_history) <= 23
    assert any('t=search' in r.url for r in requests_mock.request_history)


@pytest.mark.parametrize('wrong', ['Your.Name.S01E01.mkv', 'Your.Name.1x01.mkv'])
def test_movie_refuses_explicit_episode(requests_mock, wrong):
    video = Movie('Your.Name.2016.mkv', 'Your Name', year=2016)
    requests_mock.get(BASE + '/api', text=rss((123, wrong)))
    requests_mock.get(BASE + '/json', json=payload(name=wrong))
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(video, {Language('eng')}) == []


@pytest.mark.parametrize('extra', [{'tmdbid': '841'}, {'imdbid': 'tt0087182'}, {'tmdbid': '438631', 'imdbid': 'tt0087182'}])
def test_movie_conflicting_identity_refused_even_with_correct_year(requests_mock, extra):
    video = Movie('Dune.2021.mkv', 'Dune', year=2021)
    video.tmdb_id, video.imdb_id = 438631, 'tt1160419'
    requests_mock.get(BASE + '/api', text=rss((123, video.name)))
    requests_mock.get(BASE + '/json', json=payload(name=video.name, **extra))
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(video, {Language('eng')}) == []


@pytest.mark.parametrize('extra', [{'tvdbid': '999'}, {'tmdbid': '999'}, {'imdbid': 'tt0000999'}])
def test_episode_conflicting_identity_refused(requests_mock, extra):
    video = Episode(NAME, 'Some Show', 2, 3, series_tvdb_id=456, series_imdb_id='tt0000456')
    video.series_tmdb_id = 789
    requests_mock.get(BASE + '/api', text=rss((123, NAME)))
    requests_mock.get(BASE + '/json', json=payload(**extra))
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(video, {Language('eng')}) == []


def test_episode_explicit_series_year_contradiction_refused(requests_mock):
    video = Episode('Some.Show.2020.S02E03.mkv', 'Some Show', 2, 3, year=2020)
    wrong = 'Some.Show.1990.S02E03.mkv'
    requests_mock.get(BASE + '/api', text=rss((123, wrong)))
    requests_mock.get(BASE + '/json', json=payload(name=wrong))
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(video, {Language('eng')}) == []


@pytest.mark.parametrize('extra', [{}, {'tmdbid': None}, {'tmdbid': []}])
def test_yearless_movie_text_needs_identity_evidence(requests_mock, extra):
    video = Movie('Dune.2021.mkv', 'Dune', year=2021)
    video.tmdb_id = 438631
    requests_mock.get(BASE + '/api', text=rss((123, 'Dune.1080p.mkv')))
    requests_mock.get(BASE + '/api?t=movie&tmdbid=438631', text=rss())
    requests_mock.get(BASE + '/json', json=payload(name='Dune.1080p.mkv', **extra))
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(video, {Language('eng')}) == []


@pytest.mark.parametrize('extra,by_id', [({'tmdbid': '438631'}, False), ({'imdbid': 'tt1160419'}, False), ({}, True)])
def test_yearless_movie_verified_identity_is_sufficient(requests_mock, extra, by_id):
    video = Movie('Dune.2021.mkv', 'Dune', year=2021)
    video.tmdb_id, video.imdb_id = 438631, 'tt1160419'
    requests_mock.get(BASE + '/api', text=rss((123, 'Dune.1080p.mkv')))
    if not by_id:
        requests_mock.get(BASE + '/api?t=movie&tmdbid=438631', text=rss())
    requests_mock.get(BASE + '/json', json=payload(name='Dune.1080p.mkv', **extra))
    with AnimeToshoXYZProvider() as provider:
        assert len(provider.list_subtitles(video, {Language('eng')})) == 1


@pytest.mark.parametrize('files', [
    [{'filename': NAME}, {'filename': 'Roboto.ttf'}, {'filename': 'readme.txt'}],
    [{'filename': NAME}, {'filename': NAME}, {'filename': 'Some.Show.S02E04.ttf'}],
])
def test_one_distinct_media_file_keeps_top_level_tracks(requests_mock, files):
    requests_mock.get(BASE + '/api', text=rss((123, NAME)))
    requests_mock.get(BASE + '/json', json=payload(files=files))
    with AnimeToshoXYZProvider() as provider:
        subs = provider.list_subtitles(Episode(NAME, 'Some Show', 2, 3), {Language('eng')})
    assert len(subs) == 1
    assert subs[0].meta['file_name'] == NAME


@pytest.mark.parametrize('extra,expected', [
    ({'files': [None]}, 0),
    ({'attachments': [None]}, 0),
    ({'attachments': [None, attachment()]}, 1),
    ({'attachments': [attachment(info={'language_code': 'por', 'language': None})]}, 1),
    ({'attachments': [attachment(info=None)]}, 0),
    ({'attachments': [attachment(info=[])]}, 0),
    ({'attachments': [attachment(info=['bad'])]}, 0),
    ({'attachments': [attachment(url=['bad'])]}, 0),
    ({'attachments': [attachment(info={'language_code': 'por', 'language': ['bad']})]}, 1),
    ({'attachments': [attachment(info={'language_code': []})]}, 0),
    ({'attachments': [attachment(info={'language_code': 42})]}, 0),
    ({'attachments': [attachment(id=[])]}, 0),
    ({'files': [{'filename': NAME, 'attachments': [None, attachment()]}], 'attachments': None}, 1),
])
def test_malformed_entries_safe_and_cached_value_unmodified(requests_mock, extra, expected):
    original = payload(**extra)
    requests_mock.get(BASE + '/json', json=original)
    with AnimeToshoXYZProvider() as provider:
        first = provider._get_series([{'id': 123}], Episode(NAME, 'Some Show', 2, 3), set())
        cached = copy.deepcopy(provider._cache[('torrent', 123)][1])
        second = provider._get_series([{'id': 123}], Episode(NAME, 'Some Show', 2, 3), set())
        assert len(first) == len(second) == expected
        assert provider._cache[('torrent', 123)][1] == cached == original
    assert requests_mock.call_count == 1


@pytest.mark.parametrize('bad', [None, [], 'bad', {'id': 123, 'files': {}},
                                  {'id': 123, 'attachments': 'bad'}, {'torrent_name': NAME}])
def test_invalid_detail_container_is_provider_error_and_not_cached(requests_mock, bad):
    requests_mock.get(BASE + '/json', text=json.dumps(bad))
    with AnimeToshoXYZProvider() as provider:
        for _ in range(2):
            with pytest.raises(ProviderError):
                provider._get_series([{'id': 123}], Episode(NAME, 'Some Show', 2, 3), set())
            assert ('torrent', 123) not in provider._cache
    assert requests_mock.call_count == 2


@pytest.mark.parametrize('xml', ['<error code="100" description="bad query"/>', '<html/>', '<rss/>'])
def test_non_rss_success_payload_is_provider_error_not_cached(requests_mock, xml):
    requests_mock.get(BASE + '/api', text=xml)
    with AnimeToshoXYZProvider() as provider:
        for _ in range(2):
            with pytest.raises(ProviderError):
                provider._search_entries({'t': 'search', 'q': 'x'})
        assert not provider._cache
    assert requests_mock.call_count == 2


def test_malformed_item_url_is_skipped(requests_mock):
    requests_mock.get(BASE + '/api', text='<rss><channel><item><link>https://[broken/view/123</link></item></channel></rss>')
    with AnimeToshoXYZProvider() as provider:
        assert provider._search_entries({'t': 'search', 'q': 'x'}) == []


@pytest.mark.parametrize('bad', ['bad', ['438631'], True])
def test_malformed_known_movie_id_cannot_supply_query_provenance(requests_mock, bad):
    video = Movie('Dune.2021.mkv', 'Dune', year=2021)
    video.tmdb_id = bad
    requests_mock.get(BASE + '/api', text=rss((123, 'Dune.1080p.mkv')))
    requests_mock.get(BASE + '/json', json=payload(name='Dune.1080p.mkv'))
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(video, {Language('eng')}) == []
    assert all('t=movie' not in r.url for r in requests_mock.request_history)


@pytest.mark.parametrize('extra', [{'tmdbid': '841', 'imdbid': 'tt0087182'}, {'tmdbid': '438631', 'imdbid': 'tt0087182'}])
def test_yearless_movie_explicit_conflict_overrides_any_provenance(requests_mock, extra):
    video = Movie('Dune.2021.mkv', 'Dune', year=2021)
    video.tmdb_id, video.imdb_id = 438631, 'tt1160419'
    requests_mock.get(BASE + '/api', text=rss((123, 'Dune.1080p.mkv')))
    requests_mock.get(BASE + '/json', json=payload(name='Dune.1080p.mkv', **extra))
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(video, {Language('eng')}) == []


def rss_metadata(name=NAME, **metadata):
    """Synthetic Torznab attributes using the recorded native RSS field names."""
    attrs = ''.join(f'<torznab:attr name="{key}" value="{value}"/>' for key, value in metadata.items())
    return (rss((123, name)).replace('<rss>', '<rss xmlns:torznab="http://torznab.com/schemas/2015/feed">')
            .replace('</item>', attrs + '</item>'))


@pytest.mark.parametrize('metadata', [{'tvdbid': '999'}, {'year': '1990'}, {'imdb': 'tt0000999'}])
def test_native_rss_identity_and_series_year_contradictions_refused(requests_mock, metadata):
    video = Episode(NAME, 'Some Show', 2, 3, year=2020, series_tvdb_id=456, series_imdb_id='tt0000456')
    requests_mock.get(BASE + '/api', text=rss_metadata(**metadata))
    requests_mock.get(BASE + '/json', json=payload())
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(video, {Language('eng')}) == []


@pytest.mark.parametrize('metadata', [{'year': '2021'}, {'tmdbid': '438631'}, {'imdb': 'tt1160419'}])
def test_yearless_movie_native_rss_identity_supplies_evidence(requests_mock, metadata):
    video = Movie('Dune.2021.mkv', 'Dune', year=2021)
    video.tmdb_id, video.imdb_id = 438631, 'tt1160419'
    requests_mock.get(BASE + '/api', text=rss_metadata(name='Dune.1080p.mkv', **metadata))
    requests_mock.get(BASE + '/api?t=movie&tmdbid=438631', text=rss())
    requests_mock.get(BASE + '/json', json=payload(name='Dune.1080p.mkv'))
    with AnimeToshoXYZProvider() as provider:
        assert len(provider.list_subtitles(video, {Language('eng')})) == 1


def test_native_rss_series_year_not_later_episode_airdate(requests_mock):
    video = Episode(NAME, 'Some Show', 2, 3, year=2020, series_tvdb_id=456)
    requests_mock.get(BASE + '/api', text=rss_metadata(year='2020', tvairdate='2026-09-27'))
    requests_mock.get(BASE + '/json', json=payload(tvdbid='456'))
    with AnimeToshoXYZProvider() as provider:
        assert len(provider.list_subtitles(video, {Language('eng')})) == 1


def test_episode_yearless_later_season_is_not_airdate_contradiction(requests_mock):
    video = Episode(NAME, 'Some Show', 2, 3, year=2020, series_tvdb_id=456)
    requests_mock.get(BASE + '/api', text=rss((123, NAME)))
    requests_mock.get(BASE + '/json', json=payload(tvdbid='456', timestamp=1791628407))
    with AnimeToshoXYZProvider() as provider:
        assert len(provider.list_subtitles(video, {Language('eng')})) == 1


@pytest.mark.parametrize('year_source', ['rss', 'detail'])
def test_cycle2_verified_movie_year_retained_for_native_scoring(requests_mock, year_source):
    """Offline adaptation of the second review's native 124-versus-164 repro."""
    video = Movie('Dune.2021.1080p.WEB-DL.x264.mkv', 'Dune', year=2021,
                  source='Web', resolution='1080p', video_codec='H.264')
    name = 'Dune.1080p.WEB-DL.x264.mkv'
    requests_mock.get(BASE + '/api', text=rss_metadata(name=name, **({'year': '2021'} if year_source == 'rss' else {})))
    requests_mock.get(BASE + '/json', json=payload(name=name, **({'year': 2021} if year_source == 'detail' else {})))
    with AnimeToshoXYZProvider() as provider:
        subs = provider.list_subtitles(video, {Language('eng')})
    assert len(subs) == 1
    matches = subs[0].get_matches(video)
    assert 'year' in matches
    assert ComputeScore()(matches.copy(), subs[0], video, hearing_impaired=False)[0] == 164
    assert ComputeScore()(matches - {'year'}, subs[0], video, hearing_impaired=False)[0] == 124
    assert 'year' not in subs[0].get_matches(Movie('Dune.1984.mkv', 'Dune', year=1984))
    assert 'year' not in subs[0].get_matches(Movie('Other.2021.mkv', 'Other', year=2021))
    assert 'year' not in subs[0].get_matches(Movie('Dune.mkv', 'Dune'))


@pytest.mark.parametrize('year_source', ['rss', 'detail'])
def test_cycle2_scoring_year_conflicts_remain_rejected(requests_mock, year_source):
    video = Movie('Dune.2021.mkv', 'Dune', year=2021)
    requests_mock.get(BASE + '/api', text=rss_metadata(name='Dune.1080p.mkv', **({'year': '1984'} if year_source == 'rss' else {})))
    requests_mock.get(BASE + '/json', json=payload(name='Dune.1080p.mkv', **({'year': 1984} if year_source == 'detail' else {})))
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(video, {Language('eng')}) == []


@pytest.mark.parametrize('by_query', [False, True])
def test_cycle2_identity_without_year_does_not_invent_year_score(requests_mock, by_query):
    video = Movie('Dune.2021.mkv', 'Dune', year=2021)
    video.tmdb_id = 438631
    requests_mock.get(BASE + '/api', text=rss((123, 'Dune.1080p.mkv')))
    if not by_query:
        requests_mock.get(BASE + '/api?t=movie&tmdbid=438631', text=rss())
    requests_mock.get(BASE + '/json', json=payload(name='Dune.1080p.mkv', **({} if by_query else {'tmdbid': 438631})))
    with AnimeToshoXYZProvider() as provider:
        subs = provider.list_subtitles(video, {Language('eng')})
    assert len(subs) == 1
    assert 'year' not in subs[0].get_matches(video)
    assert not {'tmdb_id', 'imdb_id'} & subs[0].get_matches(video)


BAD_RELEASE_IDS = [True, False, 123.0, -123, 0, None, '', 'bad', '-123', '+123',
                   ' 123', '123 ', '0123', '1' * 21, '1' * 5000, 10 ** 20, [], {}]


@pytest.mark.parametrize('bad', BAD_RELEASE_IDS, ids=lambda value: str(value)[:40])
@pytest.mark.parametrize('side', ['requested', 'returned'])
def test_cycle2_invalid_detail_id_is_controlled_and_uncached(requests_mock, bad, side):
    requests_mock.get(BASE + '/json', json=payload(id=bad if side == 'returned' else 123))
    with AnimeToshoXYZProvider() as provider:
        with pytest.raises(ProviderError):
            provider._get_series([{'id': bad if side == 'requested' else 123}], Episode(NAME, 'Some Show', 2, 3), set())
        assert not provider._cache
    assert requests_mock.call_count == (1 if side == 'returned' else 0)


@pytest.mark.parametrize('requested,returned', [(123, 123), ('123', 123), (123, '123'), ('123', '123')])
def test_cycle2_canonical_detail_ids_share_cache(requests_mock, requested, returned):
    requests_mock.get(BASE + '/json', json=payload(id=returned))
    with AnimeToshoXYZProvider() as provider:
        for release_id in (requested, 123, '123'):
            subs = provider._get_series([{'id': release_id}], Episode(NAME, 'Some Show', 2, 3), set())
            assert len(subs) == 1
            assert subs[0].release_id == 123
        assert set(provider._cache) == {('torrent', 123)}
    assert requests_mock.call_count == 1


def test_cycle2_mismatched_detail_query_errors_then_retries_valid_id(requests_mock):
    """Offline second-review mismatch repro, followed by an unpoisoned retry."""
    video = Movie('Dune.2021.mkv', 'Dune', year=2021)
    video.tmdb_id = 438631
    requests_mock.get(BASE + '/api', text=rss((123, 'Dune.1080p.mkv')))
    requests_mock.get(BASE + '/json?show=torrent&id=123', [
        {'json': payload(id=999, name='Dune.1080p.mkv')},
        {'json': payload(id=123, name='Dune.1080p.mkv')},
    ])
    with AnimeToshoXYZProvider() as provider:
        with pytest.raises(ProviderError):
            provider.list_subtitles(video, {Language('eng')})
        assert ('torrent', 123) not in provider._cache
        subs = provider.list_subtitles(video, {Language('eng')})
        assert [s.release_id for s in subs] == [123]
        assert provider._cache[('torrent', 123)][1]['id'] == 123
    assert requests_mock.call_count == 3


def test_cycle2_direct_series_recorded_known_id_is_valid(requests_mock, data):
    from pathlib import Path
    recorded = json.loads((Path(data) / 'animetosho_xyz_series_response.json').read_text())
    assert recorded['id'] == 622245
    requests_mock.get(BASE + '/json?show=torrent&id=622245', json=recorded)
    video = Episode('Crowned.in.a.Hundred.Days.S01E08.mkv', 'Crowned in a Hundred Days', 1, 8)
    with AnimeToshoXYZProvider() as provider:
        subs = provider._get_series([{'id': 622245}], video, set())
        assert subs
        assert {s.release_id for s in subs} == {622245}
    assert requests_mock.call_count == 1


def test_cycle2_missing_requested_detail_id_is_controlled(requests_mock):
    with AnimeToshoXYZProvider() as provider:
        with pytest.raises(ProviderError):
            provider._get_series([{}], Episode(NAME, 'Some Show', 2, 3), set())
        assert not provider._cache
    assert requests_mock.call_count == 0


@pytest.mark.parametrize('body', [
    '<?xml version="1.0" encoding="UNKNOWN"?><rss><channel/></rss>',
    '<?xml version="1.0" encoding="UTF-32"?><rss><channel/></rss>',
    '<rss><channel><item><link>https://animetosho.net/view/' + '1' * 5000 + '</link></item></channel></rss>',
    *[rss((bad, 'Dune')) for bad in ('1' * 5000, '1' * 21, '0', '-123', '+123', '0123', '123.0', '١٢٣')],
], ids=['unknown-encoding', 'unsupported-encoding', 'numeric-5000-digits', '5000-digits', '21-digits', 'zero',
        'negative', 'plus', 'leading-zero', 'float', 'non-ascii'])
def test_cycle2_malformed_rss_is_controlled_before_details(requests_mock, body):
    """Offline second-review XML and oversized view-ID reproductions."""
    requests_mock.get(BASE + '/api', text=body)
    with AnimeToshoXYZProvider() as provider:
        for _ in range(2):
            with pytest.raises(ProviderError):
                provider.list_subtitles(Movie('Dune.2021.mkv', 'Dune', year=2021), {Language('eng')})
            assert not provider._cache
    assert requests_mock.call_count == 2
    assert all('/api?' in request.url for request in requests_mock.request_history)


@pytest.mark.parametrize('link', ['https://animetosho.net/view/123',
                                  'https://animetosho.net/view/release.n123',
                                  'https://animetosho.net/view/99999999999999999999'])
def test_cycle2_rss_canonical_positive_ids_are_valid(requests_mock, link):
    body = '<rss><channel><item><title>Dune</title><link>' + link + '</link></item></channel></rss>'
    requests_mock.get(BASE + '/api', text=body)
    with AnimeToshoXYZProvider() as provider:
        entries = provider._search_entries({'t': 'search', 'q': 'Dune'})
        assert len(entries) == 1
        assert entries[0]['id'] == (99999999999999999999 if link.endswith('99999999999999999999') else 123)


@pytest.mark.parametrize('lookup', ['tvdb', 'anidb'])
@pytest.mark.parametrize('season', [0, 1, 2])
def test_specials_native_core_explicit_season_download_gate(requests_mock, monkeypatch, season, lookup):
    """Exercise real listing, matching, scoring and pool gate; replace only download I/O."""
    name = f'Some.Show.S{season:02d}E01.mkv'
    video = Episode(name, 'Some Show', season, 1, series_tvdb_id=456)
    if lookup == 'anidb':
        video.series_anidb_episode_id = 12345
    requests_mock.get(BASE + '/api', text=rss((123, name)))
    requests_mock.get(BASE + '/json', json=payload(name=name))
    with AnimeToshoXYZProvider() as provider:
        subs = provider.list_subtitles(video, {Language('eng')})
    assert len(subs) == 1
    matches = subs[0].get_matches(video)
    pool = SZProviderPool(providers=['animetosho_xyz'])
    attempted = []

    def boundary_download(subtitle):
        attempted.append(subtitle)
        return True

    monkeypatch.setattr(pool, 'download_subtitle', boundary_download)
    downloaded = pool.download_best_subtitles(subs, video, {Language('eng')}, min_score=0)
    print({'season': season, 'lookup': lookup, 'matches': sorted(matches), 'attempts': len(attempted),
           'downloaded': len(downloaded)})
    assert {'series', 'season', 'episode'} <= matches
    assert downloaded == attempted == subs
    assert ('eid' in requests_mock.request_history[0].qs) is (lookup == 'anidb')
    assert requests_mock.call_count == 2


@pytest.mark.parametrize('lookup', ['tvdb', 'anidb'])
@pytest.mark.parametrize('name', [
    'Some.Show.S01E01.mkv', 'Some.Show.S02E01.mkv', 'Some.Show.-.01.mkv',
    'Some.Show.S00E02.mkv', 'Some.Show.S00E01E02.mkv', 'Other.Show.S00E01.mkv',
])
def test_specials_unverified_release_cannot_gain_season_or_download(requests_mock, monkeypatch, name, lookup):
    video = Episode('Some.Show.S00E01.mkv', 'Some Show', 0, 1, series_tvdb_id=456)
    if lookup == 'anidb':
        video.series_anidb_episode_id = 12345
    requests_mock.get(BASE + '/api', text=rss((123, name)))
    requests_mock.get(BASE + '/json', json=payload(name=name))
    with AnimeToshoXYZProvider() as provider:
        assert provider.list_subtitles(video, {Language('eng')}) == []
    # Direct matching must not accidentally restore season on a rejected candidate either.
    subtitle = AnimeToshoXYZSubtitle(Language('eng'), URL, {'file_name': name}, name, 123)
    assert 'season' not in subtitle.get_matches(video)
    pool = SZProviderPool(providers=['animetosho_xyz'])
    attempted = []

    def boundary_download(subtitle):
        attempted.append(subtitle)
        return True

    monkeypatch.setattr(pool, 'download_subtitle', boundary_download)
    assert pool.download_best_subtitles([subtitle], video, {Language('eng')}, min_score=0) == []
    assert attempted == []


@pytest.mark.parametrize('field,bad', [
    ('season', False), ('season', 0.0), ('season', '0'), ('season', None),
    ('episode', True), ('episode', 1.0), ('episode', '1'), ('episode', None),
    ('episode', 0), ('episode', -1),
])
def test_specials_season_match_requires_native_integer_episode_fields(field, bad):
    name = 'Some.Show.S00E01.mkv'
    video = Episode(name, 'Some Show', 0, 1)
    setattr(video, field, bad)
    subtitle = AnimeToshoXYZSubtitle(Language('eng'), URL, {'file_name': name}, name, 123)
    assert 'season' not in subtitle.get_matches(video)


@pytest.mark.parametrize('failure', [503, 'timeout'])
def test_policy_anidb_detail_transport_failure_reaches_native(requests_mock, failure):
    """Repository regression for the independent review's AniDB detail failures."""
    name = 'Some.Show.S00E01.mkv'
    video = Episode(name, 'Some Show', 0, 1, series_tvdb_id=456,
                    series_anidb_episode_id=12345)
    requests_mock.get(BASE + '/api?t=search&eid=12345', text=rss((1, name)))
    options = {'status_code': failure} if isinstance(failure, int) else {'exc': Timeout('fixture')}
    requests_mock.get(BASE + '/json?show=torrent&id=1', **options)
    requests_mock.get(BASE + '/api?t=tvsearch&tvdbid=456&season=0&ep=1', text=rss((2, name)))
    requests_mock.get(BASE + '/json?show=torrent&id=2', json=payload(id=2, name=name))
    with AnimeToshoXYZProvider() as provider:
        found = provider.list_subtitles(video, {Language('eng')})
        assert ('torrent', 1) not in provider._cache
    assert [s.release_id for s in found] == [2]
    assert {'series', 'season', 'episode'} <= found[0].get_matches(video)
    assert [r.qs for r in requests_mock.request_history] == [
        {'t': ['search'], 'eid': ['12345']}, {'show': ['torrent'], 'id': ['1']},
        {'t': ['tvsearch'], 'tvdbid': ['456'], 'season': ['0'], 'ep': ['1']},
        {'show': ['torrent'], 'id': ['2']}]


@pytest.mark.parametrize('failure', [503, 'timeout'])
def test_policy_native_discovery_transport_failure_reaches_release(requests_mock, failure):
    """Repository regression for the independent review's native discovery failures."""
    name = 'Some.Show.S00E01.mkv'
    video = Episode(name, 'Some Show', 0, 1, series_tvdb_id=456,
                    series_anidb_episode_id=12345)
    requests_mock.get(BASE + '/api?t=search&eid=12345', text=rss())
    options = {'status_code': failure} if isinstance(failure, int) else {'exc': Timeout('fixture')}
    params = {'t': 'tvsearch', 'tvdbid': 456, 'season': 0, 'ep': 1}
    requests_mock.get(BASE + '/api?t=tvsearch&tvdbid=456&season=0&ep=1', **options)
    requests_mock.get(BASE + '/api?t=search&q=Some.Show.S00E01', text=rss((2, name)))
    requests_mock.get(BASE + '/json?show=torrent&id=2', json=payload(id=2, name=name))
    with AnimeToshoXYZProvider() as provider:
        found = provider.list_subtitles(video, {Language('eng')})
        assert ('search', tuple(sorted(params.items()))) not in provider._cache
    assert [s.release_id for s in found] == [2]
    assert [r.qs for r in requests_mock.request_history] == [
        {'t': ['search'], 'eid': ['12345']},
        {'t': ['tvsearch'], 'tvdbid': ['456'], 'season': ['0'], 'ep': ['1']},
        {'t': ['search'], 'q': ['some.show.s00e01']}, {'show': ['torrent'], 'id': ['2']}]


@pytest.mark.parametrize('kind', ['tvdb', 'episode-tmdb', 'movie-tmdb'])
@pytest.mark.parametrize('stage', ['discovery', 'detail'])
@pytest.mark.parametrize('failure', [503, 'timeout'])
def test_policy_native_transport_fallback_uniform_and_no_retry(requests_mock, kind, stage, failure):
    """The same bounded stage boundary covers native episode and movie lookup."""
    if kind == 'movie-tmdb':
        name = 'Some.Movie.2020.mkv'
        video = Movie(name, 'Some Movie', year=2020)
        video.tmdb_id = 456
        params = {'t': 'movie', 'tmdbid': 456}
        native_url = BASE + '/api?t=movie&tmdbid=456'
    else:
        name = NAME
        video = Episode(name, 'Some Show', 2, 3)
        key = 'tvdbid' if kind == 'tvdb' else 'tmdbid'
        setattr(video, 'series_tvdb_id' if kind == 'tvdb' else 'series_tmdb_id', 456)
        params = {'t': 'tvsearch', key: 456, 'season': 2, 'ep': 3}
        native_url = BASE + f'/api?t=tvsearch&{key}=456&season=2&ep=3'
    options = {'status_code': failure} if isinstance(failure, int) else {'exc': Timeout('fixture')}
    requests_mock.get(native_url, **(options if stage == 'discovery' else {'text': rss((1, name))}))
    requests_mock.get(BASE + '/json?show=torrent&id=1', **options)
    # Repeated release ID must not retry a failed detail within this listing.
    requests_mock.get(BASE + '/api?t=search&q=' + name.removesuffix('.mkv'),
                      text=rss((1, name), (2, name)) if stage == 'detail' else rss((2, name)))
    requests_mock.get(BASE + '/json?show=torrent&id=2', json=payload(id=2, name=name))
    with AnimeToshoXYZProvider() as provider:
        assert [s.release_id for s in provider.list_subtitles(video, {Language('eng')})] == [2]
        assert ('torrent', 1) not in provider._cache
        if stage == 'discovery':
            assert ('search', tuple(sorted(params.items()))) not in provider._cache
    history = requests_mock.request_history
    assert len(history) == (3 if stage == 'discovery' else 4)
    assert sum('id' in r.qs and r.qs['id'] == ['1'] for r in history) == (stage == 'detail')
    assert sum('/api?' in r.url for r in history) == 2
