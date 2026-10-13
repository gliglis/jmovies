#!/usr/bin/env python3
"""
organize.py — add TMDB identifiers to movie file names.

Runs LOCALLY on the NAS (e.g. as a Synology Task Scheduler job). A single run
does the whole job:

  * for every video file WITHOUT an identifier it fetches a TMDB match and
    renames the file according to the pattern:
        Movie Title (YEAR) [tmdbid-XXXXX].<rest-of-release>.ext
  * files that already have an identifier are skipped (left untouched),
  * renaming happens IN PLACE — no folders are created and no file is moved
    to a different directory,
  * if the target name is already taken (collision), the file is NOT skipped:
    it gets a " - duplicateN" suffix (the identifier stays in the name) and
    the event is written to the log; no file is ever overwritten or deleted,
  * the run (start / progress / finish) is written to a log file and printed
    to the console.

Usage:
  python3 organize.py --dir /volume1/video/Movies            # preview
  python3 organize.py --dir /volume1/video/Movies --apply    # execute

TMDB key: --api-key flag, TMDB_API_KEY environment variable, or the
tmdb_api_key entry in config.txt located next to the script.
"""

import argparse
import datetime
import logging
import os
import re
import sys
import unicodedata

try:
    import requests
except ImportError:
    print('Missing the requests library. Install it: pip3 install requests', file=sys.stderr)
    sys.exit(1)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

VIDEO_EXT = {'.ts', '.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.mpg', '.mpeg', '.m2ts'}

RE_TMDBID = re.compile(r'\[tmdbid-(\d+)\]', re.IGNORECASE)
RE_YEAR_PAREN = re.compile(r'\((?:19|20)\d{2}\)')
RE_YEAR_BARE = re.compile(r'(?<![0-9])(?:19|20)\d{2}(?![0-9])')
# Movie part number: CZ1 / Cz 2 / Część 3 / Part 2 / Pt 2 / CD2 / Disc 2
RE_PART = re.compile(
    r'(?<![A-Za-z0-9])(?:CZ|Cz|Część|Czesc|CZĘŚĆ|Part|Pt|CD|Disc|Dysk|Odcinek|Odc)'
    r'\s*\.?\s*(\d{1,2})(?![A-Za-z0-9])', re.IGNORECASE)

_RELEASE = (r'(?:PL|ENG|EN|MULTi|LEKTOR|NAPISY|NAPiSY|DUB|SUB|2160p|1080p|1080i|720p|720i|'
            r'576p|540p|480p|UHD|UHDTV|HDTV|HDR10|HDR|DV|SDR|WEB-?DL|WEBRip|WEB|BluRay|'
            r'Blu-?Ray|BDRip|BRRip|DVDRip|HDRip|REMUX|HEVC|AVC|x264|x265|h264|h265|H\.264|'
            r'H\.265|DDP?5\.1|DD\+|E?AC-?3|AAC(?:2\.0)?|DTS(?:-HD)?(?:\.MA)?|TrueHD|Atmos|'
            r'FLAC|MA|5\.1|2\.0|B89|MR)')
RE_RELEASE_TOKEN = re.compile(r'(?<![A-Za-z0-9])' + _RELEASE + r'(?![A-Za-z0-9])', re.IGNORECASE)

log = logging.getLogger('jmovies')


# --------------------------- configuration ---------------------------

def read_config(path):
    cfg = {}
    if not os.path.isfile(path):
        return cfg
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            cfg[k.strip()] = v.strip()
    return cfg


# --------------------------- text / names ---------------------------

def normalize(s):
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return re.sub(r'\s{2,}', ' ', s).strip()


def sanitize_name(name):
    s = re.sub(r'[<>:"/\\|?*]', ' ', name)
    s = re.sub(r'\s{2,}', ' ', s)
    return s.strip(' .')


def clean_query(raw):
    s = RE_TMDBID.sub('', raw)
    s = re.sub(r'[._]+', ' ', s)
    s = RE_YEAR_PAREN.sub('', s)
    s = RE_YEAR_BARE.sub('', s)
    s = re.sub(r'\[[^\]]*\]', '', s)
    s = re.sub(r'\s{2,}', ' ', s)
    return s.strip(' -')


def year_from_text(s):
    m = RE_YEAR_PAREN.search(s)
    if m:
        return m.group(0)[1:5]
    m = RE_YEAR_BARE.search(s)
    return m.group(0) if m else None


def clean_release(s):
    s = s.strip(' ._-')
    s = re.sub(r'\s+', '.', s)
    s = re.sub(r'\.{2,}', '.', s)
    return s.strip('.')


def parse_movie(base):
    """Return (title_query, year_or_None, part_number_or_None, release, ext)."""
    stem, ext = os.path.splitext(base)

    pm = RE_PART.search(stem)
    part = int(pm.group(1)) if pm else None
    stem_wo = (stem[:pm.start()] + ' ' + stem[pm.end():]) if pm else stem

    rel_m = RE_RELEASE_TOKEN.search(stem_wo)
    if rel_m:
        title = stem_wo[:rel_m.start()]
        release = clean_release(stem_wo[rel_m.start():])
    else:
        title = stem_wo
        release = ''

    year = year_from_text(title) or year_from_text(stem)
    return clean_query(title), year, part, release, ext


def folder_title_year(ancestors):
    """Extract (title_query, year) from the parent folder if it looks like a movie folder."""
    for part in ancestors:
        q = clean_query(part.strip())
        if q:
            return q, year_from_text(part)
    return None, None


# --------------------------- TMDB ---------------------------

class Tmdb:
    def __init__(self, key, language):
        self.language = language
        self.session = requests.Session()
        self.v3 = None
        if key.count('.') >= 2 and len(key) > 50:
            self.session.headers['Authorization'] = f'Bearer {key}'
        else:
            self.v3 = key

    def _params(self, extra=None):
        p = {'language': self.language}
        if self.v3:
            p['api_key'] = self.v3
        if extra:
            p.update(extra)
        return p

    def search_movie(self, query):
        r = self.session.get('https://api.themoviedb.org/3/search/movie',
                             params=self._params({'query': query, 'include_adult': 'false'}),
                             timeout=25)
        r.raise_for_status()
        return r.json().get('results', [])

    def get_movie(self, mid):
        r = self.session.get(f'https://api.themoviedb.org/3/movie/{mid}',
                             params=self._params(), timeout=25)
        r.raise_for_status()
        return r.json()


def _title(r):
    return r.get('title') or r.get('name') or ''


def _otitle(r):
    return r.get('original_title') or r.get('original_name') or ''


def _yr(r):
    return (r.get('release_date') or r.get('first_air_date') or '')[:4]


def to_result(data):
    return {'ok': True, 'id': data.get('id'),
            'name': sanitize_name(_title(data) or _otitle(data)),
            'year': _yr(data)}


def pick_candidate(results, nquery, hint_year):
    if not results:
        return None
    exact = [r for r in results
             if normalize(_title(r)) == nquery or normalize(_otitle(r)) == nquery]
    if hint_year and hint_year.isdigit():
        hy = int(hint_year)
        ey = [r for r in exact if _yr(r) == hint_year]
        if len(ey) == 1:
            return ey[0]
        ay = [r for r in results if _yr(r) == hint_year]
        if len(ay) == 1:
            return ay[0]
        withy = [r for r in exact if _yr(r).isdigit()]
        if withy:
            withy.sort(key=lambda r: abs(int(_yr(r)) - hy))
            if len(withy) == 1 or abs(int(_yr(withy[0])) - hy) < abs(int(_yr(withy[1])) - hy):
                return withy[0]
    if len(exact) == 1:
        return exact[0]
    if len(results) == 1:
        return results[0]
    return None


def resolve_movie(tmdb, query, tmdbid_known, hint_year):
    """Return {'ok':True,'id','name','year'} or {'ok':False,'reason'}."""
    try:
        if tmdbid_known:
            return to_result(tmdb.get_movie(tmdbid_known))
        nq = normalize(query)
        results = tmdb.search_movie(query)
        chosen = pick_candidate(results, nq, hint_year)
        cand_source = results
        if not chosen and not results:
            words = query.split()
            for cut in range(len(words) - 1, 1, -1):
                r2 = tmdb.search_movie(' '.join(words[:cut]))
                if not r2:
                    continue
                cand_source = r2
                nq2 = normalize(' '.join(words[:cut]))
                exact = [x for x in r2
                         if normalize(_title(x)) == nq2 or normalize(_otitle(x)) == nq2]
                if exact:
                    chosen = pick_candidate(exact, nq2, hint_year) or exact[0]
                    break
        if chosen:
            return to_result(chosen)
        reason = ('no TMDB results' if not cand_source
                  else f'{len(cand_source)} results, no unambiguous match')
        return {'ok': False, 'reason': reason}
    except requests.RequestException as exc:
        return {'ok': False, 'reason': f'TMDB error: {exc}'}


# --------------------------- logging ---------------------------

LOG_FMT = logging.Formatter('%(asctime)s  %(levelname)s  %(message)s', '%Y-%m-%d %H:%M:%S')


def setup_console():
    log.setLevel(logging.INFO)
    log.handlers.clear()
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(LOG_FMT)
    log.addHandler(ch)


def slug(text):
    """Filesystem-safe fragment from a path's basename, for the log file name."""
    s = re.sub(r'[^\w.-]+', '_', text.strip('/').split('/')[-1] or 'root')
    return s.strip('_') or 'root'


def add_file_handler(log_dir, name):
    """Attach a file handler <log_dir>/<name>; return it (or None on failure)."""
    if not log_dir:
        return None
    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.FileHandler(os.path.join(log_dir, name), encoding='utf-8')
        fh.setFormatter(LOG_FMT)
        log.addHandler(fh)
        return fh
    except OSError as exc:
        log.warning('Could not open log file (%s): %s', os.path.join(log_dir, name), exc)
        return None


def drop_handler(fh):
    if fh:
        log.removeHandler(fh)
        fh.close()


# --------------------------- main flow ---------------------------

class Stats:
    def __init__(self):
        self.renamed = self.skipped_tagged = self.duplicates = 0
        self.unresolved = self.errors = 0


def process_root(root, tmdb, apply, stats):
    """Rename movie files under one library root, in place."""
    video_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '@eaDir']
        for f in filenames:
            if os.path.splitext(f)[1].lower() in VIDEO_EXT:
                video_files.append(os.path.join(dirpath, f))
    video_files.sort()

    log.info('---- path: %s  (%d video files) ----', root, len(video_files))
    total = len(video_files)
    claimed = set()  # target names taken under this root (collision protection)

    for idx, src in enumerate(video_files, 1):
        base = os.path.basename(src)
        dirn = os.path.dirname(src)

        # Files that already carry an identifier in their own name — skip.
        if RE_TMDBID.search(base):
            stats.skipped_tagged += 1
            continue

        # Hints from the file name and (optionally) from the parent folder.
        ancestors = src[len(root):].strip('/').split('/')[:-1]
        tmdbid_known = next((int(m.group(1)) for a in ancestors
                             if (m := RE_TMDBID.search(a))), None)
        title_q, year, part, release, ext = parse_movie(base)
        f_title, f_year = folder_title_year([a for a in ancestors if not RE_TMDBID.search(a)])
        if f_title and len(f_title) >= len(title_q):
            query, hint_year = f_title, (f_year or year)
        else:
            query, hint_year = title_q, (year or f_year)

        res = resolve_movie(tmdb, query, tmdbid_known, hint_year)
        if not res.get('ok'):
            stats.unresolved += 1
            log.warning('[%d/%d] NO MATCH: %s — %s', idx, total, base, res.get('reason', '?'))
            continue

        name, yr, tid = res['name'], res['year'], res['id']
        base_name = f'{name} ({yr}) [tmdbid-{tid}]' if yr else f'{name} [tmdbid-{tid}]'
        new_base = base_name + (f' - part{part}' if part else '') \
            + (f'.{release}' if release else '') + ext
        # Rename IN PLACE — the file stays in its own directory.
        dest = os.path.join(dirn, new_base)

        if dest == src:
            stats.skipped_tagged += 1
            continue

        # Name collision (same target already exists or was claimed this run):
        # do not overwrite — append a " - duplicateN" suffix (the TMDB id stays in the name).
        if dest in claimed or os.path.exists(dest):
            stem_c, ext_c = os.path.splitext(new_base)
            dup_n = 1
            while True:
                cand = os.path.join(dirn, f'{stem_c} - duplicate{dup_n}{ext_c}')
                if cand not in claimed and not os.path.exists(cand):
                    dest, new_base = cand, os.path.basename(cand)
                    break
                dup_n += 1
            stats.duplicates += 1
            log.warning('[%d/%d] DUPLICATE: "%s" — name taken, assigning "%s"',
                        idx, total, base, new_base)

        claimed.add(dest)
        if apply:
            try:
                os.rename(src, dest)
                stats.renamed += 1
                log.info('[%d/%d] OK: %s  ->  %s', idx, total, base, new_base)
            except OSError as exc:
                stats.errors += 1
                log.error('[%d/%d] RENAME ERROR "%s": %s', idx, total, base, exc)
        else:
            stats.renamed += 1
            log.info('[%d/%d] RENAME: %s  ->  %s', idx, total, base, new_base)


def main():
    ap = argparse.ArgumentParser(
        description='Add TMDB identifiers to movie file names (local, in place).')
    ap.add_argument('paths', nargs='*', help='movie library directory/directories (positional)')
    ap.add_argument('--dir', '--path', dest='dirs', action='append', default=[], metavar='DIR',
                    help='movie library directory; repeat for several (e.g. --dir A --dir B)')
    ap.add_argument('--config', default=os.path.join(SCRIPT_DIR, 'config.txt'))
    ap.add_argument('--apply', action='store_true', help='execute changes (preview by default)')
    ap.add_argument('--dry-run', action='store_true', help='force preview')
    ap.add_argument('--api-key', default=None)
    ap.add_argument('--language', default=None)
    ap.add_argument('--log-dir', default=None,
                    help='directory for dated log files (default: next to the script)')
    ap.add_argument('--no-log', action='store_true', help='do not write log files')
    args = ap.parse_args()

    cfg = read_config(args.config)

    given = args.dirs + args.paths or ([cfg['path']] if cfg.get('path') else [])
    api_key = args.api_key or os.environ.get('TMDB_API_KEY') or cfg.get('tmdb_api_key')
    language = args.language or cfg.get('tmdb_language') or 'en-US'
    apply = args.apply or cfg.get('mode', 'preview').lower().startswith('apply')
    if args.dry_run:
        apply = False
    log_dir = None if args.no_log else (args.log_dir or cfg.get('log_dir') or SCRIPT_DIR)
    datestr = datetime.date.today().isoformat()

    setup_console()
    main_log = add_file_handler(log_dir, f'jmovies-{datestr}.log')

    if not given:
        log.error('No library directory. Pass --dir /path (or set path= in config.txt).')
        sys.exit(1)
    if not api_key or api_key.startswith('PUT_YOUR'):
        log.error('No TMDB key (--api-key / TMDB_API_KEY / tmdb_api_key in config.txt).')
        sys.exit(1)

    # Keep existing directories only; drop duplicates / repeats by realpath.
    seen, roots = set(), []
    for p in given:
        rp = os.path.realpath(p.rstrip('/'))
        if not os.path.isdir(rp):
            log.error('Not a directory, skipping: %s', p)
            continue
        if rp not in seen:
            seen.add(rp)
            roots.append(rp)
    if not roots:
        log.error('None of the given paths is a directory.')
        sys.exit(1)

    tmdb = Tmdb(api_key, language)
    stats = Stats()

    log.info('==== START ==== libraries: %s', ', '.join(roots))
    log.info('Mode: %s', 'APPLY' if apply else 'PREVIEW (no changes, add --apply)')

    # Each path is processed separately and also gets its own dated log file.
    for root in roots:
        path_log = add_file_handler(log_dir, f'jmovies-{slug(root)}-{datestr}.log')
        process_root(root, tmdb, apply, stats)
        drop_handler(path_log)

    log.info('==== DONE ====')
    verb = 'Renamed' if apply else 'To rename'
    log.info('%s: %d (incl. suffixed duplicates: %d) | skipped (already have id): %d | '
             'no match: %d | errors: %d',
             verb, stats.renamed, stats.duplicates, stats.skipped_tagged,
             stats.unresolved, stats.errors)
    if not apply:
        log.info('This was a PREVIEW — add --apply to execute changes.')
    drop_handler(main_log)


if __name__ == '__main__':
    main()