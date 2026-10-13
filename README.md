# jmovies

Rename movie files in place with TMDB identifiers, for Jellyfin/Plex. Runs
locally on a NAS (e.g. a Synology scheduled task). Only the file name changes —
no folders are created, nothing is moved or deleted.

```
Inception.2010.PL.1080p.mkv  ->  Inception (2010) [tmdbid-27205].PL.1080p.mkv
```

## Use

```
python3 organize.py --dir "/volume1/video/Movies" --apply
```

- Without `--apply` it only previews (writes a log, changes nothing).
- `--dir` can be repeated; paths may also be listed as `path=` lines in `config.txt`.
- Needs `tmdb_api_key=` in `config.txt` (get a free key at themoviedb.org).

## Behaviour

- Only files without `[tmdbid-…]` are touched; already-tagged files are skipped.
- A name clash gets a ` - duplicateN` suffix (nothing is ever overwritten).
- Subtitles, `.nfo` and artwork sharing the file's name are renamed with it.
- Dated logs are written next to the script: `jmovies-YYYY-MM-DD.log` plus one
  per scanned path (`jmovies-path1-…`); `log_keep_days=` prunes old ones.
- A lock file prevents overlapping scheduled runs.

Requires only Python 3 (standard library — no `requests` needed).
