# -*- coding: utf-8 -*-
import logging
import lzma
import os
import re
from time import monotonic
from typing import Callable
from urllib.parse import urlsplit
from xml.etree import ElementTree

from babelfish.exceptions import LanguageReverseError
from guessit import guessit
from requests import Session, Response
from requests.exceptions import RequestException
from subzero.language import Language

from subliminal.exceptions import ProviderError
from subliminal_patch.providers import Provider
from subliminal_patch.providers.mixins import ProviderSubtitleArchiveMixin
from subliminal_patch.subtitle import Subtitle, guess_matches
from subliminal.video import Episode, Movie

logger = logging.getLogger(__name__)

supported_languages = [
    "ara",
    "eng",
    "fin",
    "fra",
    "deu",
    "heb",
    "ind",
    "ita",
    "jpn",
    "por",
    "pol",
    "rus",
    "spa",
    "swe",
    "tha",
    "tur",
    "vie",
]

_BRAZILIAN_PORTUGUESE_RE = re.compile(r'brazil|\bbr\b', re.IGNORECASE)
_VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.ts', '.m2ts', '.mpg', '.mpeg', '.webm', '.wmv', '.ogm'}


class _AnimeToshoHTTPError(ProviderError):
    """Expected transport/status failure, distinct from unsafe metadata."""


class _RSSBuilder(ElementTree.TreeBuilder):
    def doctype(self, name, pubid, system):
        # Parser callbacks reject DTDs regardless of byte encoding, before expansion.
        raise ProviderError('DTD is not allowed in AnimeTosho RSS')


def _safe_attachment_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https'
                and parsed.hostname in {'storage.animetosho.net', 'storage.animetosho.xyz'}
                and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except (TypeError, ValueError):
        return False


def _normalized_id(value, imdb=False):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if imdb:
        text = text.removeprefix('tt')
    if not re.fullmatch(r'[0-9]+', text) or len(text) > 20:
        return None
    number = int(text)
    return str(number) if number > 0 else None


def _anidb_id(value):
    """Reject malformed AniDB IDs rather than repairing padding or leading zeros."""
    normalized = _normalized_id(value)
    if isinstance(value, str) and value != normalized:
        return None
    return normalized


def _release_id(value):
    """Accept only canonical positive release IDs, bounded before integer conversion."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10 ** 20:
        return value
    if isinstance(value, str) and 1 <= len(value) <= 20 and re.fullmatch(r'[1-9][0-9]*', value):
        return int(value)
    raise ProviderError('Invalid AnimeTosho release ID')


def _identity_evidence(video, data, params=None):
    """Compare native RSS/detail IDs and RSS series/movie year, never episode airdate."""
    attrs = ({'tmdbid': 'tmdb_id', 'imdbid': 'imdb_id'} if isinstance(video, Movie)
             else {'tvdbid': 'series_tvdb_id', 'tmdbid': 'series_tmdb_id', 'imdbid': 'series_imdb_id'})
    evidence = False
    year = _normalized_id(data.get('year'))
    if year and len(year) == 4 and video.year:
        if year != str(video.year):
            return False, False
        evidence = isinstance(video, Movie)
    for key, attr in attrs.items():
        known = _normalized_id(getattr(video, attr, None), key == 'imdbid')
        returned = _normalized_id(data.get(key), key == 'imdbid')
        if known and returned:
            if known != returned:
                return False, False
            evidence = True
        if (known and params and params.get('t') == ('movie' if isinstance(video, Movie) else 'tvsearch')
                and _normalized_id(params.get(key), key == 'imdbid') == known):
            evidence = True
    return True, evidence


def _matches_video(video, name, identity=False):
    if not isinstance(name, str) or not name:
        return False
    if isinstance(video, Movie):
        native = guessit(name)
        if 'season' in native or 'episode' in native:
            return False
    guess = guessit(name, {'type': 'episode' if isinstance(video, Episode) else 'movie'})
    matches = guess_matches(video, guess)
    if guess.get('year') and video.year and guess['year'] != video.year:
        return False
    if isinstance(video, Episode):
        # No absolute-to-season conversion: seasonless anime numbering is accepted only
        # for season 1 with exactly the requested episode, never ranges or batch guesses.
        return ('series' in matches and guess.get('episode') == video.episode
                and guess.get('season', 1) == video.season)
    # Missing movie year is not agreement: text fallback must have a consistent known
    # detail ID, or come from the verified native ID query. Refuse otherwise.
    return ('title' in matches and guess.get('type') == 'movie'
            and (identity or bool(guess.get('year') and video.year == guess['year'])))


class AnimeToshoXYZSubtitle(Subtitle):
    provider_name = 'animetosho_xyz'

    def __init__(self, language, download_link, meta, release_info, release_id, forced=False, hearing_impaired=False,
                 identity_metadata=()):
        # AnimeTosho reports the forced disposition flag of the subtitle track. Keep it on the
        # language so a forced (signs only) track is never offered as a normal subtitle.
        language = Language.rebuild(language, forced=forced, hi=hearing_impaired)

        super(AnimeToshoXYZSubtitle, self).__init__(
            language,
            hearing_impaired=hearing_impaired,
            page_link=download_link,
        )
        self.meta = meta
        self.download_link = download_link
        self.release_info = release_info
        self.release_id = release_id
        self.forced = forced
        self.matches = set()
        # Only provider-validated RSS/detail evidence; never infer a year from query IDs.
        self.identity_metadata = tuple(dict(data) for data in identity_metadata)

    @property
    def id(self):
        # AnimeTosho may reuse the same attachment URL across releases; keep each
        # release association distinct so scoring can use release-specific metadata.
        return f'{self.release_id}:{self.download_link}'

    def get_matches(self, video):
        name = self.meta.get('file_name') or self.meta.get('torrent_name', '')
        self.matches = guess_matches(video, guessit(name))
        if (isinstance(video, Movie) and video.year
                and all(_identity_evidence(video, data)[0] for data in self.identity_metadata)
                and any(_normalized_id(data.get('year')) == str(video.year) for data in self.identity_metadata)
                and _matches_video(video, name, identity=True)):
            self.matches.add('year')
        # Native guess_matches skips falsy season 0; restore only verified specials.
        if (isinstance(video, Episode)
                and (video.season == 1 or (type(video.season) is int and video.season == 0
                     and type(video.episode) is int and video.episode > 0))
                and _matches_video(video, name)):
            self.matches.add('season')
        return self.matches


class AnimeToshoXYZProvider(Provider, ProviderSubtitleArchiveMixin):
    provider_name = 'animetosho_xyz'
    subtitle_class = AnimeToshoXYZSubtitle
    languages = {Language('por', 'BR')} | {Language(sl) for sl in supported_languages}
    # The provider pool intersects the requested languages with the languages declared here before
    # calling list_subtitles, so the forced variants have to be declared as well or a profile
    # asking for forced subtitles would never reach this provider.
    languages.update(set(Language.rebuild(lang, forced=True) for lang in languages))
    languages.update(set(Language.rebuild(lang, hi=True) for lang in languages))
    video_types = (Episode, Movie)

    result_limit = 20
    cache_ttl = 300

    def __init__(self):
        self.session = None
        self._cache = {}

    def _cached(self, key, load):
        now = monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]
        value = load()
        if len(self._cache) >= 256:
            self._cache.clear()
        self._cache[key] = (now, value)
        return value

    def initialize(self):
        self.session = Session()
        self.session.headers.update({'User-Agent': os.environ.get("SZ_USER_AGENT", "Bazarr")})

    def terminate(self):
        self.session.close()

    def checked(self, fn: Callable) -> Response:
        """
        Executes the provided function and performs error handling and response validation for API calls.

        :param fn: The callable function that makes the HTTP request.
        :type fn: Callable
        :return: The HTTP response object returned by the provided function if the status code is valid.
        :rtype: Response
        :raises ProviderError: If the HTTP transport fails or the response status is not successful.
        """
        response = None
        try:
            response = fn()
        except RequestException as error:
            logger.warning('AnimeTosho metadata transport failed (%s).', type(error).__name__)
            raise _AnimeToshoHTTPError('AnimeTosho HTTP request failed') from error
        else:
            status_code = response.status_code
            if status_code == 404:
                logger.error(f"Endpoint not found: {response.url}")
                raise _AnimeToshoHTTPError("Endpoint not found")
            elif status_code != 200:
                logger.error(f"HTTP error {status_code} for {response.url}")
                raise _AnimeToshoHTTPError(f"HTTP error {status_code}")

            return response

    def list_subtitles(self, video, languages):
        seen = set()
        # AniDB, native ID, then text; retain the existing title alternative when
        # an earlier identifier stage is absent, without adding a fourth discovery.
        queries = list(self._search_queries(video))[:3]
        for index, params in enumerate(queries):
            try:
                entries = self._search_entries(params)
                # Reserve one detail for each remaining fallback, unless this stage already
                # supplies requested tracks. The global ceiling remains twenty unique details.
                ceiling = self.result_limit - (len(queries) - index - 1)
                subtitles = self._get_series(entries, video, seen, params=params,
                                             languages=languages, ceiling=ceiling)
            except _AnimeToshoHTTPError:
                # Only expected transport/status failures may advance a stage. Unsafe
                # metadata and identity errors still propagate, and attempted details
                # remain in seen so neither the budget nor failed releases are retried.
                if index == len(queries) - 1:
                    raise
                logger.warning('AnimeTosho metadata stage unavailable; trying next fallback.')
                continue
            if subtitles:
                return subtitles
            if len(seen) >= self.result_limit:
                break
        return []

    @staticmethod
    def _search_queries(video):
        if isinstance(video, Movie):
            if _normalized_id(getattr(video, 'tmdb_id', None)):
                yield {'t': 'movie', 'tmdbid': video.tmdb_id}
            elif _normalized_id(getattr(video, 'imdb_id', None), imdb=True):
                yield {'t': 'movie', 'imdbid': video.imdb_id}
            title_query = f'{video.title} {video.year or ""}'.strip()
        else:
            episode_id = getattr(video, 'series_anidb_episode_id', None)
            # The native cached resolver returns (series_id, episode_id). Accept that
            # shape only when its series agrees with the refined video's series ID.
            if isinstance(episode_id, tuple) and len(episode_id) == 2:
                series_id = _anidb_id(getattr(video, 'series_anidb_id', None))
                episode_id = episode_id[1] if series_id and _anidb_id(episode_id[0]) == series_id else None
            episode_id = _anidb_id(episode_id)
            if episode_id:
                yield {'t': 'search', 'eid': episode_id}
            if _normalized_id(getattr(video, 'series_tvdb_id', None)):
                yield {'t': 'tvsearch', 'tvdbid': video.series_tvdb_id,
                       'season': video.season, 'ep': video.episode}
            # No persisted series TMDB metadata exists today; accept only an explicit attr.
            elif _normalized_id(getattr(video, 'series_tmdb_id', None)):
                yield {'t': 'tvsearch', 'tmdbid': video.series_tmdb_id,
                       'season': video.season, 'ep': video.episode}
            title_query = f'{video.series} S{video.season:02d}E{video.episode:02d}'
        release = os.path.splitext(os.path.basename(video.name))[0]
        if release:
            yield {'t': 'search', 'q': release}
        if title_query and title_query != release:
            yield {'t': 'search', 'q': title_query}

    def _search_entries(self, params):
        # Only immutable request metadata enters the key, never a Video object.
        key = ('search', tuple(sorted(params.items())))
        return self._cached(key, lambda: self._fetch_entries(params))

    def _fetch_entries(self, params):
        # Use request directly: native RetryingSession.get would pace repeated
        # discovery attempts before this bounded fallback loop can advance.
        response = self.checked(lambda: self.session.request(
            'GET', 'https://feed.animetosho.net/api', params=params, timeout=10, allow_redirects=False))
        try:
            root = ElementTree.fromstring(response.content,
                                          parser=ElementTree.XMLParser(target=_RSSBuilder()))
        except (ElementTree.ParseError, LookupError, ValueError) as error:
            raise ProviderError('Invalid AnimeTosho RSS') from error
        if root.tag != 'rss' or root.find('channel') is None:
            raise ProviderError('Unexpected AnimeTosho RSS response')
        entries = []
        for item in root.findall('./channel/item'):
            link = item.findtext('link') or item.findtext('guid') or ''
            try:
                path = urlsplit(link).path
            except ValueError:
                continue
            match = re.search(r'/view/(?:[^/]*\.n)?([^/]+)$', path)
            release_id = _release_id(match[1]) if match else None
            if release_id is not None and release_id not in {entry['id'] for entry in entries}:
                metadata = []
                for attr in item:
                    if attr.tag not in {'{http://torznab.com/schemas/2015/feed}attr',
                                        '{http://www.newznab.com/DTD/2010/feeds/attributes/}attr'}:
                        continue
                    key = attr.get('name')
                    if key == 'imdb':
                        key = 'imdbid'
                    if key in {'tvdbid', 'tmdbid', 'imdbid', 'year'}:
                        # Preserve duplicate attributes: one conflicting value vetoes the release.
                        metadata.append({key: attr.get('value')})
                entries.append({'id': release_id, 'title': item.findtext('title', ''), 'metadata': metadata})
            if len(entries) >= self.result_limit:
                break
        return entries

    def download_subtitle(self, subtitle):
        logger.info('Downloading subtitle %r', subtitle)

        if not _safe_attachment_url(subtitle.page_link):
            raise ProviderError('Untrusted attachment URL')
        r = self.checked(lambda: self.session.get(subtitle.page_link, timeout=10, allow_redirects=False))

        if not self._is_xz_file(r.content):
            raise ProviderError('Unidentified archive type')

        try:
            subtitle.content = lzma.decompress(r.content)
        except lzma.LZMAError as error:
            raise ProviderError('Invalid XZ subtitle') from error
        return subtitle

    @staticmethod
    def _is_xz_file(content):
        return content.startswith(b'\xFD\x37\x7A\x58\x5A\x00')

    def _get_series(self, entries, video, seen, params=None, languages=None, ceiling=None):
        subtitles = []

        for entry in entries:
            release_id = _release_id(entry.get('id'))
            if release_id in seen:
                continue
            if len(seen) >= self.result_limit:
                break
            if ceiling is not None and len(seen) >= ceiling and not subtitles:
                break
            seen.add(release_id)
            torrent_data = self._cached(('torrent', release_id), lambda: self._fetch_torrent(release_id))

            compatible, identity = _identity_evidence(video, torrent_data, params)
            for metadata in entry.get('metadata', []):
                agrees, evidence = _identity_evidence(video, metadata)
                compatible = compatible and agrees
                identity = identity or evidence
            if not compatible:
                continue
            for subtitle_file, file_name in self._iter_subtitle_attachments(torrent_data, video, identity):
                info = subtitle_file.get('info')
                if not isinstance(info, dict):
                    continue
                lang_code = info.get('language_code')
                if not isinstance(lang_code, str) or not lang_code:
                    continue
                try:
                    lang = Language.fromalpha3b(lang_code)
                except LanguageReverseError:
                    logger.debug('Ignoring unidentified subtitle language %r', lang_code)
                    continue

                # Portuguese and Brazilian Portuguese share the same code, so the language name is
                # the only thing telling them apart. The API labels it "Portuguese[BR]", but the
                # spelled-out "Brazilian Portuguese" also shows up.
                language_name = info.get('language')
                if (lang.alpha3 == 'por' and isinstance(language_name, str)
                        and _BRAZILIAN_PORTUGUESE_RE.search(language_name)):
                    lang = Language('por', 'BR')

                subtitle = self.subtitle_class(
                    lang,
                    subtitle_file['url'],
                    meta={**torrent_data, 'file_name': file_name},
                    release_info=entry.get('title'),
                    release_id=release_id,
                    identity_metadata=[{key: data[key] for key in ('year', 'tvdbid', 'tmdbid', 'imdbid') if key in data}
                                       for data in [torrent_data, *entry.get('metadata', [])]],
                    # AnimeTosho exposes the forced disposition flag of the track as a boolean.
                    forced=info.get('forced') in (True, 1, 'true', '1'),
                    hearing_impaired=info.get('hearing_impaired') in (True, 1, 'true', '1'),
                )

                logger.debug('Found subtitle %r', subtitle)
                if languages is None or subtitle.language in languages:
                    subtitles.append(subtitle)

        return subtitles

    def _fetch_torrent(self, release_id):
        release_id = _release_id(release_id)
        # Bypass native RetryingSession.get here too: each failed detail must consume
        # only one attempt before the bounded stage fallback advances.
        response = self.checked(lambda: self.session.request(
            'GET', 'https://feed.animetosho.net/json', params={'show': 'torrent', 'id': release_id},
            timeout=10, allow_redirects=False))
        try:
            data = response.json()
        except ValueError as error:
            raise ProviderError('Invalid AnimeTosho JSON') from error
        if not isinstance(data, dict):
            raise ProviderError('Invalid AnimeTosho detail mapping')
        if _release_id(data.get('id')) != release_id:
            raise ProviderError('Mismatched AnimeTosho release ID')
        for field in ('files', 'attachments'):
            if data.get(field) is not None and not isinstance(data[field], list):
                raise ProviderError('Invalid AnimeTosho detail container')
        # Keep the cache's raw mapping immutable; malformed entries are skipped on use.
        return data

    @staticmethod
    def _iter_subtitle_attachments(torrent_data, video, identity=False):
        """Bind tracks to an unambiguous matching media file, not a batch's title."""
        seen = set()

        def collect(attachments, name):
            if not isinstance(attachments, list) or not _matches_video(video, name, identity):
                return
            for attachment in attachments:
                if not isinstance(attachment, dict) or not isinstance(attachment.get('id'), (int, str, type(None))):
                    continue
                if attachment.get('type') != 'subtitle' or not _safe_attachment_url(attachment.get('url')):
                    continue
                key = (attachment.get('id'), attachment.get('url'))
                if key in seen:
                    continue
                seen.add(key)
                yield attachment, name

        files = torrent_data.get('files') or []
        media = [file for file in files if isinstance(file, dict)
                 and isinstance(file.get('filename'), str)
                 and os.path.splitext(file['filename'])[1].lower() in _VIDEO_EXTENSIONS]
        names = {file['filename'] for file in media}
        # Top-level tracks cannot be assigned safely to a multi-file batch.
        if len(names) == 1 or not files:
            name = next(iter(names)) if names else torrent_data.get('torrent_name', '')
            yield from collect(torrent_data.get('attachments'), name)
        for file in media:
            yield from collect(file.get('attachments'), file.get('filename', ''))
