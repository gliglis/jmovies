#!/usr/bin/env python3
"""
organize.py — find duplicates across a NAS media library, by TMDB id only.

Runs LOCALLY on the NAS (e.g. as a Synology Task Scheduler job). It scans one
or more directories and reports every TMDB id that appears more than once —
nothing else is compared, only the [tmdbid-XXXXX] tag embedded in names.

What carries an id (and is therefore counted):
  * video files whose name contains [tmdbid-XXXXX]   (movie libraries),
  * folders whose name contains [tmdbid-XXXXX]        (series libraries).
A TMDB id found on two or more such entries is reported as a duplicate group.

The program only READS the filesystem — it never renames, moves or deletes
anything. The result is written to a log file and printed to the console.

The log file name carries the date (jduplicates-YYYY-MM-DD.log), so scheduled
runs are easy to read by date.

Usage:
  python3 organize.py --dir /volume1/video/Movies
  python3 organize.py --dir /volume1/video/Movies --dir "/volume1/video/TV/Series"
  python3 organize.py /volume1/video/Movies "/volume1/video/TV/Series"   # positional also works
"""

import argparse
import datetime
import logging
import os
import re
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

VIDEO_EXT = {'.ts', '.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.mpg', '.mpeg', '.m2ts'}
RE_TMDBID = re.compile(r'\[tmdbid-(\d+)\]', re.IGNORECASE)

log = logging.getLogger('jduplicates')


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


def config_paths(path):
    """Return every 'path=' entry from the config, in order (one path per line).
    Paths may contain spaces and need no quoting inside the config file."""
    out = []
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            if k.strip() == 'path' and v.strip():
                out.append(v.strip())
    return out


def setup_logging(log_path):
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter('%(asctime)s  %(levelname)s  %(message)s', '%Y-%m-%d %H:%M:%S')
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    if log_path:
        try:
            os.makedirs(os.path.dirname(log_path) or '.', exist_ok=True)
            fh = logging.FileHandler(log_path, encoding='utf-8')
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError as exc:
            log.warning('Could not open log file (%s): %s', log_path, exc)


def prune_logs(log_dir, prefix, keep_days):
    """Delete <prefix>*.log files in log_dir older than keep_days (0 = keep all)."""
    if not log_dir or not keep_days:
        return
    cutoff = time.time() - keep_days * 86400
    try:
        for f in os.listdir(log_dir):
            if f.startswith(prefix) and f.endswith('.log'):
                fp = os.path.join(log_dir, f)
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
    except OSError:
        pass


def acquire_lock(lock_path, stale_seconds=6 * 3600):
    """Create an exclusive lock file. Returns True on success. A stale lock is
    replaced; if locking is impossible, returns True so the run is not blocked."""
    try:
        if os.path.exists(lock_path) and time.time() - os.path.getmtime(lock_path) > stale_seconds:
            os.remove(lock_path)
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode('ascii'))
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def release_lock(lock_path):
    try:
        os.remove(lock_path)
    except OSError:
        pass


def scan(path, by_key):
    """Add every id-tagged folder and video file under `path` to by_key, keyed by
    (kind, tmdbid). 'folder' ids (series) and 'movie' ids (video files) are kept
    in separate groups, because TMDB numbers movies and TV shows independently.
    Returns the number of tagged entries found."""
    found = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d != '@eaDir']
        for d in dirnames:
            m = RE_TMDBID.search(d)
            if m:
                by_key.setdefault(('folder', int(m.group(1))), set()).add(
                    os.path.realpath(os.path.join(dirpath, d)))
                found += 1
        for f in filenames:
            if os.path.splitext(f)[1].lower() not in VIDEO_EXT:
                continue
            m = RE_TMDBID.search(f)
            if m:
                by_key.setdefault(('movie', int(m.group(1))), set()).add(
                    os.path.realpath(os.path.join(dirpath, f)))
                found += 1
    return found


def main():
    ap = argparse.ArgumentParser(
        description='Find duplicates in a NAS media library by TMDB id (read-only).')
    ap.add_argument('paths', nargs='*', help='one or more directories to scan (positional)')
    ap.add_argument('--dir', '--path', dest='dirs', action='append', default=[],
                    metavar='DIR', help='directory to scan; repeat for several (e.g. --dir A --dir B)')
    ap.add_argument('--config', default=os.path.join(SCRIPT_DIR, 'config.txt'))
    ap.add_argument('--log-dir', default=None,
                    help='directory for the dated log file (default: next to the script)')
    ap.add_argument('--no-log', action='store_true', help='do not write a log file')
    args = ap.parse_args()

    cfg = read_config(args.config)
    all_paths = args.dirs + args.paths or config_paths(args.config)

    log_dir = None
    if args.no_log:
        log_path = None
    else:
        datestr = datetime.date.today().isoformat()
        log_dir = args.log_dir or cfg.get('log_dir') or SCRIPT_DIR
        log_path = os.path.join(log_dir, f'jduplicates-{datestr}.log')
    try:
        keep_days = int(cfg.get('log_keep_days') or 0)
    except ValueError:
        keep_days = 0
    prune_logs(log_dir, 'jduplicates-', keep_days)
    setup_logging(log_path)

    if not all_paths:
        log.error('No paths given. Use --dir /path (repeatable) or add path= lines to config.txt.')
        sys.exit(1)

    # Keep only existing directories; drop duplicates / nested repeats by realpath.
    seen, roots = set(), []
    for p in all_paths:
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

    lock_path = os.path.join(SCRIPT_DIR, 'jduplicates.lock')
    if not acquire_lock(lock_path):
        log.error('Another run is in progress (lock: %s). Exiting.', lock_path)
        sys.exit(0)

    try:
        log.info('==== START ==== scanning %d path(s)', len(roots))

        by_key = {}
        total = 0
        for root in roots:
            n = scan(root, by_key)
            total += n
            log.info('Scanned: %s  (%d tagged entries)', root, n)

        dups = {k: sorted(paths) for k, paths in by_key.items() if len(paths) > 1}

        log.info('Distinct TMDB ids: %d | tagged entries: %d | DUPLICATE ids: %d',
                 len(by_key), total, len(dups))

        dup_entries = 0
        for kind, tid in sorted(dups, key=lambda k: (k[0], k[1])):
            paths = dups[(kind, tid)]
            dup_entries += len(paths)
            log.info('tmdbid-%d (%s, %d copies):', tid, kind, len(paths))
            for p in paths:
                log.info('    %s', p)

        log.info('==== DONE ====')
        if dups:
            log.info('Found %d duplicate id(s) across %d entries.', len(dups), dup_entries)
        else:
            log.info('No duplicates found.')
    finally:
        release_lock(lock_path)


if __name__ == '__main__':
    main()