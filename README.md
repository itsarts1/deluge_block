# deluge_block

Automatically scan Deluge every 10 seconds for torrents containing configured
file extensions.

## Requirements

```bash
pip install deluge-client
```

## Configuration

Copy `config-sample.ini` to `config.ini`, then update the Deluge RPC connection
settings and extension lists.

```ini
[login]
host=<host>
port=58846
user=<username>
password=<password>

[lists]
forbidden=.zipx,.mkv.lnk,.exe,.bat,.msi
unwanted=.txt,.nfo,.jpg,.png,.gif,.html

[behavior]
delete_downloaded_data=false
scan_interval_seconds=10
```

Extension matching is case-insensitive, and whitespace around configured
extensions is ignored.

## Behavior

Torrents that contain any file with an extension in the `forbidden` list are
removed from Deluge. Forbidden files take precedence even if the file is already
set to Skip priority.

By default, removing a forbidden torrent does not delete downloaded data. Set
`delete_downloaded_data=true` only if you want `core.remove_torrent` to remove
the torrent and delete downloaded data.

Torrents that do not contain forbidden files are kept. Files with extensions in
the `unwanted` list are set to Skip priority (`0`), while all other file
priorities are preserved.

If Deluge has not exposed a torrent's file metadata yet, or if the file and
priority counts do not match, the torrent is not cached and will be checked
again on a later scan.

## Running

```bash
python block.py
```

The script keeps running and scans Deluge every 10 seconds by default. Change
`scan_interval_seconds` in `config.ini` to use a different interval.

## Tests

Tests use mocked Deluge RPC calls and never modify real torrents.

```bash
python -m unittest discover -v
```

## Thanks

Thanks to https://github.com/dcquence/deluge_exclude_files for getting this
started.
