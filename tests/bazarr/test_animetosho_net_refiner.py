"""Functional AniDB selection regressions, without invoking a metadata service."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from subliminal_patch.core import Episode
from subtitles.refiners import anidb


@pytest.mark.parametrize('enabled,expected', [(['animetosho_xyz'], False),
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


def test_only_migrated_provider_does_not_require_anidb_api_credentials(monkeypatch, caplog):
    monkeypatch.setattr(anidb, 'settings', SimpleNamespace(
        general=SimpleNamespace(enabled_providers=['animetosho_xyz']),
        anidb=SimpleNamespace(api_client=None, api_client_ver=1)))
    client = Mock()
    client.get_show_information.return_value = (123, 3, 0)
    client.has_api_credentials = False
    monkeypatch.setattr(anidb, 'AniDBClient', lambda *args: client)
    item = Episode('Some.Show.S01E03.mkv', 'Some Show', 1, 3, series_tvdb_id=123)
    anidb.refine_anidb_ids(item)
    assert 'credentials are not fully set up' not in caplog.text
    client.get_episode_ids.assert_not_called()
