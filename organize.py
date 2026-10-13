#!/usr/bin/env python3
"""
organize.py

Porządkuje bibliotekę FILMÓW pod Jellyfin/Plex — działa LOKALNIE na serwerze
(np. jako zadanie w Harmonogramie zadań Synology). Bez SSH. Dopasowania TMDB
liczone lokalnie.

Co robi (jedna sekwencja, "komplet"):
  1) MAPOWANIE  — plikom BEZ identyfikatora dodaje [tmdbid-XXXXX] w nazwie.
                  Pliki, które identyfikator już mają, są POMIJANE (nieruszane).
  2) DEDUPLIKACJA — gdy pod tą samą nazwą docelową jest kilka plików, zostaje
                  największy, reszta jest usuwana.
  3) SPRZĄTANIE — usuwa .vsmeta i puste katalogi.

Docelowa nazwa (płasko — plik zostaje bez dodatkowego folderu):
  Nazwa filmu (ROK) [tmdbid-XXXXX].PL.1080i.HDTV.H264-B89.ts
Filmy wieloczęściowe (CZ1/CZ2, Part 2, CD2):
  Nazwa filmu (ROK) [tmdbid-XXXXX] - part1.<release>.ext

Program NIE tworzy kopii i NIE zakłada folderów — tylko zmiana nazwy/przeniesienie
(+ kasowanie śmieci). Domyślnie PODGLĄD. Faktyczne zmiany: flaga --apply.

Uruchomienie:
  python3 organize.py --dir /volume1/video/Filmy            # podgląd
  python3 organize.py --dir /volume1/video/Filmy --apply    # wykonanie

Klucz TMDB: flaga --api-key, zmienna TMDB_API_KEY albo wpis tmdb_api_key w config.txt
(szukanym obok skryptu). Cache TMDB gromadzi i pozycje nowo rozpoznane, i te
odczytane wprost z nazw plików już posiadających identyfikator — dzięki temu
kolejne uruchomienia nie odpytują TMDB o to, co już znane.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import sys
import unicodedata

try:
    import requests
except ImportError:
    print('Brak biblioteki requests. Zainstaluj: pip3 install requests (lub uruchom ./run.sh)',
          file=sys.stderr)
    sys.exit(1)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

VIDEO_EXT = {'.ts', '.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.mpg', '.mpeg', '.m2ts'}

RE_TMDBID = re.compile(r'\[tmdbid-(\d+)\]', re.IGNORECASE)
RE_YEAR_PAREN = re.compile(r'\((?:19|20)\d{2}\)')
RE_YEAR_BARE = re.compile(r'(?<![0-9])(?:19|20)\d{2}(?![0-9])')
# Część filmu: CZ1 / Cz 2 / Część 3 / Part 2 / Pt 2 / CD2 / Disc 2
RE_PART = re.compile(
    r'(?<![A-Za-z0-9])(?:CZ|Cz|Część|Czesc|CZĘŚĆ|Part|Pt|CD|Disc|Dysk|Odcinek|Odc)'
    r'\s*\.?\s*(\d{1,2})(?![A-Za-z0-9])', re.IGNORECASE)

_RELEASE = (r'(?:PL|ENG|EN|MULTi|LEKTOR|NAPISY|NAPiSY|DUB|SUB|2160p|1080p|1080i|720p|720i|'
            r'576p|540p|480p|UHD|UHDTV|HDTV|HDR10|HDR|DV|SDR|WEB-?DL|WEBRip|WEB|BluRay|'
            r'Blu-?Ray|BDRip|BRRip|DVDRip|HDRip|REMUX|HEVC|AVC|x264|x265|h264|h265|H\.264|'
            r'H\.265|DDP?5\.1|DD\+|E?AC-?3|AAC(?:2\.0)?|DTS(?:-HD)?(?:\.MA)?|TrueHD|Atmos|'
            r'FLAC|MA|5\.1|2\.0|B89|MR)')
RE_RELEASE_TOKEN = re.compile(r'(?<![A-Za-z0-9])' + _RELEASE + r'(?![A-Za-z0-9])', re.IGNORECASE)


# --------------------------- konfiguracja / pliki ---------------------------

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


def load_overrides(path):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            left, right = line.split('=', 1)
            right = right.strip()
            if right.isdigit():
                out[normalize(left.strip())] = int(right)
    return out


def load_cache(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get('ok')}


def save_cache(path, cache):
    if not path:
        return
    ok = {k: v for k, v in cache.items() if isinstance(v, dict) and v.get('ok')}
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(ok, fh, ensure_ascii=False, indent=0, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        print(f'Uwaga: nie udało się zapisać cache ({exc}).', file=sys.stderr)


def write_log(path, root, apply, plan, moved, removed_dup, apply_output,
              dup_lines, unresolved):
    if not path:
        return
    ts = datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
    lines = ['=' * 60,
             f'{ts}  tryb={"WYKONANIE" if apply else "PODGLĄD"}  biblioteka={root}',
             f'Zaplanowano zmian: {len(plan)}']
    for src, dest in plan:
        lines.append(f'  {src}')
        lines.append(f'    -> {dest}')
    if apply:
        lines.append(f'Wynik: przeniesiono {moved}, usunięto duplikatów {removed_dup}')
        for ln in (apply_output or '').splitlines():
            lines.append('  ' + ln)
    if dup_lines:
        lines.append(f'DUPLIKATY ({"usunięte" if apply else "wykryte"}): '
                     f'{sum(1 for x in dup_lines if x.startswith("USUN"))}')
        lines.extend('  ' + x for x in dup_lines)
    if unresolved:
        lines.append(f'Bez jednoznacznego dopasowania TMDB: {len(unresolved)}')
        for query, hint, reason, cands, nfiles in unresolved:
            hint_s = f', rok z folderu: {hint}' if hint else ''
            lines.append(f'  "{query}" — {reason}{hint_s} ({nfiles} plik(ów))')
            for c in cands:
                lines.append(f'      {c["name"]} ({c["year"]}) tmdbid-{c["id"]}')
    lines.append('')
    try:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
    except OSError as exc:
        print(f'Uwaga: nie udało się zapisać logu ({exc}).', file=sys.stderr)


# --------------------------- tekst / nazwy ---------------------------

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
    s = re.sub(r'(?<![A-Za-z0-9])(?:19|20)\d{2}(?![A-Za-z0-9])', '', s)
    s = re.sub(r'\[[^\]]*\]', '', s)
    s = re.sub(r'\s{2,}', ' ', s)
    return s.strip(' -')


def year_from_text(s):
    m = RE_YEAR_PAREN.search(s)
    if m:
        return m.group(0)[1:5]
    m = RE_YEAR_BARE.search(s)
    return m.group(0) if m else None


def strip_sep(s):
    return s.strip(' ._-')


def clean_release(s):
    s = strip_sep(s)
    s = re.sub(r'\s+', '.', s)
    s = re.sub(r'\.{2,}', '.', s)
    return s.strip('.')


def human(n):
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{int(n)} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


def parse_movie(base):
    """Zwraca (tytul_query, rok_lub_None, numer_czesci_lub_None, release, ext)."""
    stem, ext = os.path.splitext(base)

    # Numer części (opcjonalnie); wytnij go z nazwy do dalszej obróbki
    pm = RE_PART.search(stem)
    part = int(pm.group(1)) if pm else None
    stem_wo = (stem[:pm.start()] + ' ' + stem[pm.end():]) if pm else stem

    # Podział na tytuł / część techniczną wg pierwszego znanego tagu release
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
    """Z folderu nad plikiem wyciąga (tytul_query, rok) jeśli to folder filmu."""
    for part in ancestors:
        p = part.strip()
        q = clean_query(p)
        if q:
            return q, year_from_text(part)
    return None, None


# --------------------------- TMDB (filmy) ---------------------------

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


def resolve_movie(tmdb, query, tmdbid_known, hint_year, cache, overrides):
    nq = normalize(query)
    if not tmdbid_known and nq in overrides:
        okey = f'ov:{overrides[nq]}'
        if okey in cache:
            return cache[okey]
        try:
            res = to_result(tmdb.get_movie(overrides[nq]))
        except requests.RequestException as exc:
            res = {'ok': False, 'reason': f'błąd TMDB: {exc}', 'candidates': []}
        cache[okey] = res
        return res

    key = f'id:{tmdbid_known}' if tmdbid_known else f'q:{nq}|{hint_year}'
    if key in cache:
        return cache[key]
    try:
        if tmdbid_known:
            res = to_result(tmdb.get_movie(tmdbid_known))
        else:
            results = tmdb.search_movie(query)
            chosen = pick_candidate(results, nq, hint_year)
            cand_source = results
            if not chosen and not results:
                words = query.split()
                for cut in range(len(words) - 1, 1, -1):
                    q = ' '.join(words[:cut])
                    r2 = tmdb.search_movie(q)
                    if not r2:
                        continue
                    cand_source = r2
                    nq2 = normalize(q)
                    exact = [x for x in r2
                             if normalize(_title(x)) == nq2 or normalize(_otitle(x)) == nq2]
                    if exact:
                        chosen = pick_candidate(exact, nq2, hint_year) or exact[0]
                        break
            if chosen:
                res = to_result(chosen)
            else:
                reason = 'brak wyników TMDB' if not cand_source \
                    else f'{len(cand_source)} wyników, brak jednoznacznego'
                cands = [{'id': r.get('id'), 'name': _title(r), 'year': _yr(r)}
                         for r in cand_source[:5]]
                res = {'ok': False, 'reason': reason, 'candidates': cands}
    except requests.RequestException as exc:
        res = {'ok': False, 'reason': f'błąd TMDB: {exc}', 'candidates': []}
    cache[key] = res
    return res


# --------------------------- operacje na plikach (lokalnie) ---------------------------

def list_files_with_sizes(root):
    """Zwraca {ścieżka: rozmiar} dla wszystkich plików pod root (pomija @eaDir)."""
    sizes = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '@eaDir']
        for f in filenames:
            full = os.path.join(dirpath, f)
            try:
                sizes[full] = os.path.getsize(full)
            except OSError:
                sizes[full] = 0
    return sizes


def _junk_re(keep_vsmeta):
    pat = r'@eaDir|\.?DS_Store|\._.*|Thumbs\.db|@SynoResource|\.@__thumb|desktop\.ini'
    if not keep_vsmeta:
        pat += r'|.*\.vsmeta'
    return re.compile(pat, re.IGNORECASE)


def remove_vsmeta(root):
    """Usuwa pliki *.vsmeta. Zwraca liczbę usuniętych."""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '@eaDir']
        for f in filenames:
            if f.lower().endswith('.vsmeta'):
                try:
                    os.remove(os.path.join(dirpath, f))
                    n += 1
                except OSError:
                    pass
    return n


def remove_empty_dirs(root, keep_vsmeta):
    """Usuwa katalogi zawierające wyłącznie śmieci (lub puste). Zwraca liczbę usuniętych."""
    junk = _junk_re(keep_vsmeta)
    root = os.path.abspath(root)
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        ad = os.path.abspath(dirpath)
        if ad == root or '@eaDir' in ad.split(os.sep):
            continue
        try:
            entries = os.listdir(dirpath)
        except OSError:
            continue
        if all(junk.fullmatch(e) for e in entries):
            try:
                shutil.rmtree(dirpath)
                removed += 1
            except OSError:
                pass
    return removed


def move_file(src, dest):
    """Przenosi/zmienia nazwę; obsługuje też inny system plików. Zwraca True przy sukcesie."""
    try:
        os.rename(src, dest)
    except OSError:
        shutil.move(src, dest)
    return True


# --------------------------- główny przebieg ---------------------------

def main():
    ap = argparse.ArgumentParser(
        description='Porządkowanie FILMÓW pod Jellyfin (lokalnie + TMDB): '
                    'dodanie [tmdbid] do nienazwanych plików, deduplikacja, sprzątanie.')
    ap.add_argument('--dir', '--path', dest='path', default=None,
                    help='katalog-biblioteka z filmami (wymagane, chyba że podane w config.txt)')
    ap.add_argument('--config', default=os.path.join(SCRIPT_DIR, 'config.txt'))
    ap.add_argument('--overrides', default=os.path.join(SCRIPT_DIR, 'tmdb_overrides.txt'))
    ap.add_argument('--apply', action='store_true', help='wykonaj zmiany (domyślnie podgląd)')
    ap.add_argument('--dry-run', action='store_true', help='wymuś podgląd')
    ap.add_argument('--api-key', default=None)
    ap.add_argument('--language', default=None)
    ap.add_argument('--no-cleanup-empty', action='store_true',
                    help='nie usuwaj .vsmeta ani pustych katalogów')
    ap.add_argument('--keep-vsmeta', action='store_true', help='zachowaj pliki .vsmeta')
    ap.add_argument('--cache', default=None)
    ap.add_argument('--no-cache', action='store_true')
    ap.add_argument('--log', default=None)
    ap.add_argument('--no-log', action='store_true')
    args = ap.parse_args()

    cfg = read_config(args.config)

    root = (args.path or cfg.get('path') or '').rstrip('/')
    if not root:
        print('Brak katalogu biblioteki. Podaj --dir /ścieżka (lub path= w config.txt).',
              file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(root):
        print(f'Katalog nie istnieje: {root}', file=sys.stderr)
        sys.exit(1)

    api_key = args.api_key or os.environ.get('TMDB_API_KEY') or cfg.get('tmdb_api_key')
    if not api_key or api_key.startswith('TU_WPISZ'):
        print('Brak klucza TMDB (--api-key / TMDB_API_KEY / tmdb_api_key w config.txt).',
              file=sys.stderr)
        sys.exit(1)

    language = args.language or cfg.get('tmdb_language') or 'en-US'
    apply = args.apply or cfg.get('tryb', 'podglad').lower().startswith('wykonaj')
    if args.dry_run:
        apply = False

    overrides = load_overrides(args.overrides)
    tmdb = Tmdb(api_key, language)
    cache_path = None if args.no_cache else \
        (args.cache or cfg.get('cache_path') or os.path.join(SCRIPT_DIR, 'tmdb_cache.json'))
    log_path = None if args.no_log else \
        (args.log or cfg.get('log_path') or os.path.join(SCRIPT_DIR, 'jmovies.log'))

    sizes = list_files_with_sizes(root)
    existing = set(sizes)
    video_files = [p for p in sizes if os.path.splitext(p)[1].lower() in VIDEO_EXT]

    cache = load_cache(cache_path)
    cache_loaded = len(cache)

    # Grupowanie: tylko pliki BEZ identyfikatora w WŁASNEJ nazwie są kandydatami
    # do zmiany nazwy. Pliki już otagowane są pomijane i tylko dopisywane do cache.
    groups = {}
    skipped_tagged = 0
    for full in video_files:
        base = full.rsplit('/', 1)[-1]

        m_self = RE_TMDBID.search(base)
        if m_self:
            # Już ma identyfikator -> nie wymaga edycji; dopisz do cache (bez TMDB).
            skipped_tagged += 1
            tid = int(m_self.group(1))
            ckey = f'id:{tid}'
            if ckey not in cache:
                title_part = base[:m_self.start()]
                cache[ckey] = {'ok': True, 'id': tid,
                               'name': sanitize_name(clean_query(title_part)) or f'tmdbid-{tid}',
                               'year': year_from_text(title_part) or ''}
            continue

        ancestors = full[len(root):].lstrip('/').split('/')[:-1]
        tmdbid_known = None
        for a in ancestors:
            m = RE_TMDBID.search(a)
            if m:
                tmdbid_known = int(m.group(1))
                break

        title_q, year, part, release, ext = parse_movie(base)
        f_title, f_year = folder_title_year([a for a in ancestors if not RE_TMDBID.search(a)])
        if f_title and (len(f_title) >= len(title_q)):
            query, hint_year = f_title, (f_year or year)
        else:
            query, hint_year = title_q, (year or f_year)

        key = f'id:{tmdbid_known}' if tmdbid_known else f'q:{normalize(query)}'
        g = groups.setdefault(key, {'query': query, 'tmdbid': tmdbid_known,
                                    'hint_year': hint_year, 'files': []})
        if not g['hint_year'] and hint_year:
            g['hint_year'] = hint_year
        g['files'].append((full, part, release, ext))

    unresolved = []
    dest_map = {}   # dest -> [(path, size), ...] kandydaci do tej samej nazwy

    for g in groups.values():
        res = resolve_movie(tmdb, g['query'], g['tmdbid'], g['hint_year'], cache, overrides)
        if not res.get('ok'):
            unresolved.append((g['query'], g['hint_year'], res.get('reason', '?'),
                               res.get('candidates', []), len(g['files'])))
            continue
        name, year, tid = res['name'], res['year'], res['id']
        base_name = f'{name} ({year}) [tmdbid-{tid}]' if year else f'{name} [tmdbid-{tid}]'
        for full, part, release, ext in g['files']:
            stem_out = base_name + (f' - part{part}' if part else '') + (f'.{release}' if release else '')
            # Płaska struktura: plik wprost w bibliotece, bez osobnego folderu.
            dest = f'{root}/{stem_out}{ext}'
            dest_map.setdefault(dest, []).append((full, sizes.get(full, 0)))

    # Dla każdej nazwy docelowej: zostaje NAJWIĘKSZY plik, reszta to duplikaty.
    plan = []            # (src, dest) do przeniesienia/zmiany nazwy
    to_delete = []       # (path, size, dest) duplikaty do usunięcia
    for dest, cands in dest_map.items():
        # dołącz plik już leżący pod docelową nazwą (jeśli nie jest wśród kandydatów)
        if dest in existing and all(c[0] != dest for c in cands):
            cands = cands + [(dest, sizes.get(dest, 0))]
        # usuń duplikaty po ścieżce
        seen = {}
        for p, s in cands:
            seen[p] = s
        cands = [(p, seen[p]) for p in seen]
        if len(cands) == 1 and cands[0][0] == dest:
            continue  # już poprawnie nazwany, nic do roboty
        cands.sort(key=lambda c: (-c[1], c[0]))  # największy pierwszy
        keeper = cands[0]
        if keeper[0] != dest:
            plan.append((keeper[0], dest))
        for p, s in cands[1:]:
            to_delete.append((p, s, dest))

    if cache_path:
        save_cache(cache_path, cache)

    # Raport duplikatów (ścieżki + rozmiary) — do konsoli i do logu
    dup_lines = []
    verb = 'USUN' if apply else 'DUP '
    for p, s, dest in to_delete:
        dup_lines.append(f'{verb} {p}  ({human(s)}, {s} B)')
        dup_lines.append(f'     zostaje: {dest}  ({human(sizes.get(dest, 0) or max((x[1] for x in dest_map.get(dest, [])), default=0))})')

    print(f'Biblioteka: {root}')
    print('Tryb: ' + ('WYKONANIE (mapowanie + dedupe + czyszczenie)' if apply
                       else 'PODGLĄD (nic nie zmieniam)'))
    if cache_path:
        ok_now = sum(1 for v in cache.values() if isinstance(v, dict) and v.get('ok'))
        print(f'Cache TMDB: {cache_path} (wczytano: {cache_loaded}, zapisano: {ok_now})')
    if log_path:
        print(f'Log: {log_path}')
    print(f'Pominięto (mają już identyfikator): {skipped_tagged}')
    print(f'Plików do zmiany nazwy/przeniesienia: {len(plan)}')
    print(f'Duplikatów {"do usunięcia" if not apply else "usuwanych"}: {len(to_delete)}')
    print('---')
    for src, dest in plan:
        print(src[len(root):].lstrip('/'))
        print('   ->  ' + dest[len(root):].lstrip('/'))

    moved = removed_dup = 0
    apply_lines = []
    if apply:
        # 1) usuń duplikaty (zostaje największy) — PRZED przenoszeniem, by zwolnić nazwę
        for p, s, dest in to_delete:
            try:
                os.remove(p)
                removed_dup += 1
                apply_lines.append(f'USUNIETO DUP: {p}')
            except OSError as exc:
                apply_lines.append(f'BŁĄD (usuwanie): {p} ({exc})')
        # 2) mapowanie: przenieś/zmień nazwę
        for src, dest in plan:
            if os.path.exists(dest):
                apply_lines.append(f'POMIJAM (istnieje): {dest}')
                continue
            try:
                move_file(src, dest)
                moved += 1
                apply_lines.append(f'OK: {dest}')
            except OSError as exc:
                apply_lines.append(f'BŁĄD (przenoszenie): {src} ({exc})')
        # 3) czyszczenie: .vsmeta + puste katalogi
        vsm = rd = 0
        if not args.no_cleanup_empty:
            if not args.keep_vsmeta:
                vsm = remove_vsmeta(root)
            rd = remove_empty_dirs(root, args.keep_vsmeta)

        for ln in apply_lines:
            print(ln)
        if removed_dup:
            print(f'Usunięto duplikatów: {removed_dup}')
        if vsm:
            print(f'Usunięto plików .vsmeta: {vsm}')
        if rd:
            print(f'Usunięto pustych katalogów: {rd}')

    write_log(log_path, root, apply, plan, moved, removed_dup,
              '\n'.join(apply_lines), dup_lines, unresolved)

    print('\n==================== PODSUMOWANIE ====================')
    if apply:
        print(f'Przeniesiono/zmieniono: {moved}, usunięto duplikatów: {removed_dup}')
    else:
        print(f'Zaplanowano zmian: {len(plan)}, duplikatów do usunięcia: {len(to_delete)}  '
              f'(podgląd — dodaj --apply aby wykonać)')

    if to_delete:
        print(f'\nDuplikaty ({"usunięte" if apply else "do usunięcia"}) — ścieżki i rozmiary:')
        for ln in dup_lines[:60]:
            print('  ' + ln)

    if unresolved:
        print(f'\nFilmy bez jednoznacznego dopasowania TMDB ({len(unresolved)}):')
        for query, hint, reason, cands, nfiles in unresolved:
            hint_s = f', rok z folderu: {hint}' if hint else ''
            print(f'  "{query}"  — {reason}{hint_s}  ({nfiles} plik(ów))')
            for c in cands:
                print(f'      kandydat: {c["name"]} ({c["year"]})  tmdbid-{c["id"]}')
        print('\n  Aby wymusić dopasowanie, dopisz do tmdb_overrides.txt, np.:')
        print('      Nazwa filmu = 12345')


if __name__ == '__main__':
    main()