#!/usr/bin/env python3
"""
filmy_porzadkuj.py

Porządkuje bibliotekę FILMÓW dokumentalnych (jednorazowych produkcji) pod Jellyfin,
operując na NAS-ie po SSH (find + mkdir -p + mv; bez SFTP). Dopasowania TMDB (filmy)
liczone są lokalnie.

Docelowa struktura (film = osobny folder, plik nazwany jak folder):
  Nazwa filmu (ROK) [tmdbid-XXXXX]/
    Nazwa filmu (ROK) [tmdbid-XXXXX].PL.1080i.HDTV.H264-B89.ts

Filmy wieloczęściowe (CZ1/CZ2, Part 2, CD2) trafiają do JEDNEGO folderu jako:
    Nazwa filmu (ROK) [tmdbid-XXXXX] - part1.<release>.ext
    Nazwa filmu (ROK) [tmdbid-XXXXX] - part2.<release>.ext

Program NIE modyfikuje zawartości plików — tylko mkdir / mv (oraz kasowanie śmieci).
Domyślnie PODGLĄD. Faktyczne zmiany: flaga --apply.

Tryby dodatkowe:
  --cleanup-only   usuń .vsmeta i puste katalogi (bez TMDB i przenoszenia)
  --dedupe-only    usuń duplikaty tego samego filmu/części (zostaje największy plik)
"""

import argparse
import datetime
import json
import os
import re
import shlex
import sys
import unicodedata

try:
    import paramiko
except ImportError:
    print('Brak biblioteki paramiko. Uruchom przez ./uruchom_filmy.sh', file=sys.stderr)
    sys.exit(1)
try:
    import requests
except ImportError:
    print('Brak biblioteki requests. Uruchom przez ./uruchom_filmy.sh', file=sys.stderr)
    sys.exit(1)


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

RELEASE_WORDS = {
    'pl', 'eng', 'en', 'multi', 'lektor', 'napisy', 'dub', 'sub', 'uhd', 'uhdtv',
    'hdtv', 'hdr', 'hdr10', 'dv', 'sdr', 'web', 'webrip', 'webdl', 'dl', 'bluray',
    'bdrip', 'brrip', 'dvdrip', 'hdrip', 'remux', 'hevc', 'avc', 'truehd', 'atmos',
    'flac', 'aac', 'dts', 'ddp', 'ac', 'eac', 'proper', 'repack', 'internal',
    'extended', 'remastered', 'uncut', 'hybrid', 'imax', 'dual', 'dubbed', 'subbed',
    'ma', 'mr', 'hd', 'sd',
}


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


def looks_like_title(tail):
    if not tail:
        return False
    for t in re.split(r'[.\s_\-]+', tail):
        if t and re.fullmatch(r'[^\W\d_]{3,}', t) and normalize(t) not in RELEASE_WORDS:
            return True
    return False


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


# --------------------------- sprzątanie ---------------------------

def build_cleanup_lines(root, keep_vsmeta=False):
    qroot = shlex.quote(root)
    junk = r'@eaDir|\.?DS_Store|\._.*|Thumbs\.db|@SynoResource|\.@__thumb|desktop\.ini'
    lines = []
    if not keep_vsmeta:
        junk += r'|.*\.vsmeta'
        lines += [
            f"VSN=$(find {qroot} -type f -name '*.vsmeta' 2>/dev/null | wc -l)",
            f"find {qroot} -type f -name '*.vsmeta' -exec rm -f -- {{}} + 2>/dev/null",
            "printf 'USUNIETO VSMETA: %s\\n' \"$VSN\"",
        ]
    lines += [
        f"JUNK={shlex.quote(junk)}",
        f"find {qroot} -depth -type d 2>/dev/null | while IFS= read -r d; do",
        f"  [ \"$d\" = {qroot} ] && continue",
        "  case \"$d\" in */@eaDir|*/@eaDir/*) continue;; esac",
        "  if [ -z \"$(ls -A \"$d\" 2>/dev/null | grep -vxE \"$JUNK\")\" ]; then",
        "    rm -rf -- \"$d\" 2>/dev/null && printf 'USUNIETO KATALOG: %s\\n' \"$d\"",
        "  fi",
        "done",
    ]
    return lines


# --------------------------- SSH ---------------------------

def ssh_run(client, command, stdin_data=None):
    sin, sout, serr = client.exec_command(command)
    if stdin_data is not None:
        sin.write(stdin_data)
        sin.channel.shutdown_write()
    out = sout.read().decode('utf-8', 'replace')
    err = serr.read().decode('utf-8', 'replace')
    code = sout.channel.recv_exit_status()
    return code, out, err


def connect(cfg):
    print(f"Łączę: {cfg['user']}@{cfg['host']}:{cfg.get('port', '22')}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(cfg['host'], port=int(cfg.get('port', '22')),
                       username=cfg['user'], password=cfg['password'],
                       look_for_keys=False, allow_agent=False, timeout=20)
    except Exception as exc:  # noqa: BLE001
        print(f'Nie udało się połączyć: {exc}', file=sys.stderr)
        sys.exit(1)
    return client


# --------------------------- główny przebieg ---------------------------

def main():
    ap = argparse.ArgumentParser(description='Porządkowanie FILMÓW pod Jellyfin (SSH + TMDB).')
    ap.add_argument('--config', default='konfiguracja_filmy.txt')
    ap.add_argument('--overrides', default='tmdb_overrides_filmy.txt')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--api-key', default=None)
    ap.add_argument('--language', default=None)
    ap.add_argument('--path', default=None)
    ap.add_argument('--no-cleanup-empty', action='store_true')
    ap.add_argument('--cleanup-only', action='store_true',
                    help='tylko usuń .vsmeta i puste katalogi (bez TMDB i przenoszenia)')
    ap.add_argument('--keep-vsmeta', action='store_true')
    ap.add_argument('--dedupe-only', action='store_true',
                    help='usuń duplikaty tego samego filmu/części (zostaje największy plik)')
    ap.add_argument('--flatten-only', action='store_true',
                    help='wyjmij pliki wideo z podfolderów wprost do biblioteki, '
                         'potem usuń .vsmeta i puste foldery (bez TMDB)')
    ap.add_argument('--cache', default=None)
    ap.add_argument('--no-cache', action='store_true')
    ap.add_argument('--log', default=None)
    ap.add_argument('--no-log', action='store_true')
    args = ap.parse_args()

    cfg = read_config(args.config)
    for req in ('host', 'user', 'password'):
        if not cfg.get(req):
            print(f'Brak "{req}" w {args.config}', file=sys.stderr)
            sys.exit(1)

    root = (args.path or cfg.get('path') or '').rstrip('/')
    if not root:
        print('Brak ścieżki biblioteki (path).', file=sys.stderr)
        sys.exit(1)

    api_key = args.api_key or os.environ.get('TMDB_API_KEY') or cfg.get('tmdb_api_key')
    need_tmdb = not (args.cleanup_only or args.dedupe_only or args.flatten_only)
    if need_tmdb and (not api_key or api_key.startswith('TU_WPISZ')):
        print('Brak klucza TMDB (--api-key / TMDB_API_KEY / tmdb_api_key w konfiguracji).',
              file=sys.stderr)
        sys.exit(1)

    language = args.language or cfg.get('tmdb_language') or 'en-US'
    apply = args.apply or cfg.get('tryb', 'podglad').lower().startswith('wykonaj')
    if args.dry_run:
        apply = False

    overrides = load_overrides(args.overrides)
    tmdb = Tmdb(api_key, language) if api_key else None
    cache_path = None if args.no_cache else (args.cache or cfg.get('cache_path') or 'tmdb_cache_filmy.json')
    log_path = None if args.no_log else (args.log or cfg.get('log_path') or 'filmy.log')

    client = connect(cfg)

    code, _o, _e = ssh_run(client, f'test -d {shlex.quote(root)}')
    if code != 0:
        print(f'Ścieżka nie istnieje na NAS-ie: {root}', file=sys.stderr)
        client.close()
        sys.exit(1)

    # --- tryb: tylko sprzątanie ---
    if args.cleanup_only:
        print(f'Biblioteka: {root}\nTryb: TYLKO SPRZĄTANIE')
        _c, out, _e = ssh_run(client, 'sh -s',
                              stdin_data='\n'.join(build_cleanup_lines(root, args.keep_vsmeta)) + '\n')
        if out:
            print(out, end='')
        removed = sum(1 for ln in out.split('\n') if ln.startswith('USUNIETO KATALOG:'))
        vsm = next((ln.split(':', 1)[1].strip()
                    for ln in out.split('\n') if ln.startswith('USUNIETO VSMETA:')), None)
        client.close()
        if vsm is not None:
            print(f'Usunięto plików .vsmeta: {vsm}')
        print(f'Usunięto pustych katalogów: {removed}')
        return

    # --- tryb: usuwanie duplikatów (ten sam film + część) ---
    if args.dedupe_only:
        print(f'Biblioteka: {root}')
        print('Tryb: USUWANIE DUPLIKATÓW '
              + ('— WYKONANIE' if apply else '— PODGLĄD (dodaj --apply aby skasować)'))
        _c, out, _e = ssh_run(client, f"find {shlex.quote(root)} -type f -printf '%s\\t%p\\n'")
        if _c != 0 or not out.strip():
            _c, out, _e = ssh_run(
                client, f"find {shlex.quote(root)} -type f -exec stat -c '%s\t%n' -- {{}} +")
        groups = {}
        for line in out.split('\n'):
            if '\t' not in line:
                continue
            size_s, path = line.split('\t', 1)
            base = path.rsplit('/', 1)[-1]
            if os.path.splitext(base)[1].lower() not in VIDEO_EXT:
                continue
            d = path.rsplit('/', 1)[0]
            _q, _y, part, _r, _e2 = parse_movie(base)
            try:
                size = int(size_s)
            except ValueError:
                size = 0
            groups.setdefault((d, part), []).append((size, path))
        losers = []
        for (d, part), items in sorted(groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
            if len(items) < 2:
                continue
            items.sort(key=lambda x: (-x[0], x[1]))
            keep = items[0]
            tag = f' część {part}' if part else ''
            print(f'{d[len(root):].lstrip("/")}{tag}:')
            print(f'  ZOSTAJE:  {keep[1].rsplit("/", 1)[-1]}  ({keep[0]} B)')
            for sz, p in items[1:]:
                print(f'  DUPLIKAT: {p.rsplit("/", 1)[-1]}  ({sz} B)')
                losers.append(p)
        if not losers:
            print('Nie znaleziono duplikatów.')
            client.close()
            return
        if apply:
            script = '\n'.join(
                f'rm -f -- {shlex.quote(p)} && printf "USUNIETO: %s\\n" {shlex.quote(p)}'
                for p in losers) + '\n'
            _c, o, _e = ssh_run(client, 'sh -s', stdin_data=script)
            if o:
                print('---')
                print(o, end='')
            n = sum(1 for ln in o.split('\n') if ln.startswith('USUNIETO:'))
            client.close()
            print(f'\nUsunięto duplikatów: {n}')
        else:
            client.close()
            print(f'\nDuplikatów do usunięcia: {len(losers)}  (dodaj --apply aby skasować)')
        return

    # --- tryb: spłaszczenie (wyjmij pliki z podfolderów do biblioteki) ---
    if args.flatten_only:
        print(f'Biblioteka: {root}')
        print('Tryb: SPŁASZCZENIE '
              + ('— WYKONANIE' if apply else '— PODGLĄD (dodaj --apply aby wykonać)'))
        _c, out, _e = ssh_run(client, f"find {shlex.quote(root)} -type f -printf '%s\\t%p\\n'")
        if _c != 0 or not out.strip():
            _c, out, _e = ssh_run(
                client, f"find {shlex.quote(root)} -type f -exec stat -c '%s\t%n' -- {{}} +")
        sizes = {}
        for line in out.split('\n'):
            if '\t' in line:
                s, p = line.split('\t', 1)
                try:
                    sizes[p] = int(s)
                except ValueError:
                    sizes[p] = 0
        existing = set(sizes)
        # pliki wideo leżące w PODFOLDERACH (nie wprost w root)
        dest_map = {}
        for p in sizes:
            if os.path.splitext(p)[1].lower() not in VIDEO_EXT:
                continue
            d = p.rsplit('/', 1)[0]
            if d == root:
                continue  # już w bibliotece
            base = p.rsplit('/', 1)[-1]
            dest_map.setdefault(f'{root}/{base}', []).append((p, sizes.get(p, 0)))

        plan = []
        to_delete = []
        for dest, cands in dest_map.items():
            if dest in existing and all(c[0] != dest for c in cands):
                cands = cands + [(dest, sizes.get(dest, 0))]
            seen = {}
            for p, s in cands:
                seen[p] = s
            cands = [(p, seen[p]) for p in seen]
            cands.sort(key=lambda c: (-c[1], c[0]))
            keeper = cands[0]
            if keeper[0] != dest:
                plan.append((keeper[0], dest))
            for p, s in cands[1:]:
                to_delete.append((p, s, dest))

        print(f'Plików do wyjęcia: {len(plan)}, duplikatów: {len(to_delete)}')
        for src, dest in plan:
            print(f'{src[len(root):].lstrip("/")}')
            print(f'   ->  {dest[len(root):].lstrip("/")}')
        dup_lines = []
        for p, s, dest in to_delete:
            verb = 'USUN' if apply else 'DUP '
            dup_lines.append(f'{verb} {p}  ({human(s)}, {s} B)')
            dup_lines.append(f'     zostaje: {dest}')
        for ln in dup_lines:
            print('  ' + ln)

        moved = removed_dup = 0
        apply_output = ''
        if apply:
            lines = []
            for p, s, dest in to_delete:
                q = shlex.quote(p)
                lines.append(f'rm -f -- {q} && printf "USUNIETO DUP: %s\\n" {q}')
            for src, dest in plan:
                qsrc, qdst = shlex.quote(src), shlex.quote(dest)
                lines.append(
                    f'if [ -e {qdst} ]; then printf "POMIJAM (istnieje): %s\\n" {qdst}; '
                    f'else mv -- {qsrc} {qdst} && printf "OK: %s\\n" {qdst}; fi'
                )
            if not args.no_cleanup_empty:
                lines += build_cleanup_lines(root, args.keep_vsmeta)
            _c, out, err = ssh_run(client, 'sh -s', stdin_data='\n'.join(lines) + '\n')
            if out:
                print('---')
                print(out, end='')
            if err.strip():
                print(err, file=sys.stderr, end='')
            apply_output = out
            moved = sum(1 for ln in out.split('\n') if ln.startswith('OK:'))
            removed_dup = sum(1 for ln in out.split('\n') if ln.startswith('USUNIETO DUP:'))
            vsm = next((ln.split(':', 1)[1].strip()
                        for ln in out.split('\n') if ln.startswith('USUNIETO VSMETA:')), None)
            rd = sum(1 for ln in out.split('\n') if ln.startswith('USUNIETO KATALOG:'))
            if removed_dup:
                print(f'Usunięto duplikatów: {removed_dup}')
            if vsm is not None and vsm != '0':
                print(f'Usunięto plików .vsmeta: {vsm}')
            if rd:
                print(f'Usunięto pustych katalogów: {rd}')

        client.close()
        write_log(log_path, root, apply, plan, moved, removed_dup, apply_output,
                  dup_lines, [])
        print('\n==================== PODSUMOWANIE ====================')
        if apply:
            print(f'Wyjęto plików: {moved}, usunięto duplikatów: {removed_dup}')
        else:
            print(f'Do wyjęcia: {len(plan)}, duplikatów: {len(to_delete)}  '
                  f'(podgląd — dodaj --apply aby wykonać)')
        return

    # --- tryb główny: pełna sekwencja (mapowanie -> dedupe -> czyszczenie) ---
    _c, out, err = ssh_run(client, f"find {shlex.quote(root)} -type f -printf '%s\\t%p\\n'")
    if _c != 0 or not out.strip():
        _c, out, err = ssh_run(
            client, f"find {shlex.quote(root)} -type f -exec stat -c '%s\t%n' -- {{}} +")
    sizes = {}
    for line in out.split('\n'):
        if '\t' not in line:
            continue
        s, p = line.split('\t', 1)
        try:
            sizes[p] = int(s)
        except ValueError:
            sizes[p] = 0
    existing = set(sizes)
    video_files = [p for p in sizes if os.path.splitext(p)[1].lower() in VIDEO_EXT]

    # grupuj po filmie (folder z [tmdbid] albo tytuł); w grupie trzymamy pliki+części
    groups = {}
    for full in video_files:
        base = full.rsplit('/', 1)[-1]
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

    cache = load_cache(cache_path)
    cache_loaded = len(cache)
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
    print(f'Plików do zmiany nazwy/przeniesienia: {len(plan)}')
    print(f'Duplikatów {"do usunięcia" if not apply else "usuwanych"}: {len(to_delete)}')
    print('---')
    for src, dest in plan:
        print(src[len(root):].lstrip('/'))
        print('   ->  ' + dest[len(root):].lstrip('/'))

    moved = removed_dup = 0
    apply_output = ''
    if apply:
        lines = []
        # 1) usuń duplikaty (zostaje największy) — PRZED przenoszeniem, by zwolnić nazwę
        for p, s, dest in to_delete:
            q = shlex.quote(p)
            lines.append(f'rm -f -- {q} && printf "USUNIETO DUP: %s\\n" {q}')
        # 2) mapowanie: przenieś/zmień nazwę
        for src, dest in plan:
            qsrc, qdst, qdir = shlex.quote(src), shlex.quote(dest), shlex.quote(dest.rsplit('/', 1)[0])
            lines.append(
                f'mkdir -p {qdir}; '
                f'if [ -e {qdst} ]; then printf "POMIJAM (istnieje): %s\\n" {qdst}; '
                f'else mv -- {qsrc} {qdst} && printf "OK: %s\\n" {qdst}; fi'
            )
        # 3) czyszczenie: .vsmeta + puste katalogi
        if not args.no_cleanup_empty:
            lines += build_cleanup_lines(root, args.keep_vsmeta)
        if lines:
            _c, out, err = ssh_run(client, 'sh -s', stdin_data='\n'.join(lines) + '\n')
            if out:
                print('---')
                print(out, end='')
            if err.strip():
                print(err, file=sys.stderr, end='')
            apply_output = out
            moved = sum(1 for ln in out.split('\n') if ln.startswith('OK:'))
            removed_dup = sum(1 for ln in out.split('\n') if ln.startswith('USUNIETO DUP:'))
            vsm = next((ln.split(':', 1)[1].strip()
                        for ln in out.split('\n') if ln.startswith('USUNIETO VSMETA:')), None)
            rd = sum(1 for ln in out.split('\n') if ln.startswith('USUNIETO KATALOG:'))
            if removed_dup:
                print(f'Usunięto duplikatów: {removed_dup}')
            if vsm is not None and vsm != '0':
                print(f'Usunięto plików .vsmeta: {vsm}')
            if rd:
                print(f'Usunięto pustych katalogów: {rd}')

    client.close()
    write_log(log_path, root, apply, plan, moved, removed_dup, apply_output,
              dup_lines, unresolved)

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
        print('\n  Aby wymusić dopasowanie, dopisz do tmdb_overrides_filmy.txt, np.:')
        print('      Nazwa filmu = 12345')


if __name__ == '__main__':
    main()
