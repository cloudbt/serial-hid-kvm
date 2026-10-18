"""Saved-recording catalog and signed, directory-confined file links."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.parse import urlencode


class RecordingLibrary:
    def __init__(self, directory):
        self.root = Path(directory).expanduser().resolve()
        self.secret = secrets.token_bytes(32)

    @staticmethod
    def identifier(relative):
        return hashlib.sha256(relative.encode()).hexdigest()[:32]

    def confined(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Recording is outside the configured directory")
        return path

    def catalog(self):
        entries = {}
        if not self.root.exists():
            return entries
        for path in self.root.iterdir():
            if path.name.startswith('.') or path.suffix.lower() not in ('.mp4', '.webm') or not path.is_file():
                continue
            try:
                self.confined(path)
            except ValueError:
                continue
            key = self.identifier(path.stem + '.mp4')
            if key not in entries or path.suffix.lower() == '.mp4':
                state = 'MP4 ready' if path.suffix.lower() == '.mp4' else self.source_state(path)
                entries[key] = self.entry(key, path, path.name, state)
        for manifest in (self.root / '.recording-sessions').glob('*/session.json'):
            try:
                data = json.loads(manifest.read_text(encoding='utf-8'))
                if not data['finished']:
                    continue
                destination = self.confined(self.root / data['filename'])
                source = self.confined(manifest.with_name('source.webm'))
                key = self.identifier(destination.name)
                path = destination if destination.is_file() else source
                state = 'MP4 ready' if path == destination else self.source_state(source)
                name = destination.name if path == destination else destination.with_suffix('.webm').name
                entries[key] = self.entry(key, path, name, state)
                entries[key]['session_id'] = data.get('id')
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return entries

    @staticmethod
    def source_state(source):
        log = source.with_suffix('.conversion.log')
        return 'MP4 conversion failed · WebM available' if log.exists() else 'WebM saved · MP4 processing'

    def entry(self, key, path, name, state):
        stat = path.stat()
        return dict(id=key, path=path, name=name, state=state, size=stat.st_size, modified=stat.st_mtime)

    def sign(self, relative, expires):
        return hmac.new(self.secret, f'{relative}\n{expires}'.encode(), hashlib.sha256).hexdigest()

    def file_url(self, relative):
        self.confined(self.root / relative)
        expires = str(int(time.time()) + 12 * 3600)
        return '/recordings/file?' + urlencode(dict(file=relative, expires=expires, key=self.sign(relative, expires)))

    def items(self, limit=10):
        entries = sorted(self.catalog().values(), key=lambda item: item['modified'], reverse=True)
        if limit is not None:
            entries = entries[:limit]
        result = []
        expires = str(int(time.time()) + 12 * 3600)
        for entry in entries:
            item = {k: v for k, v in entry.items() if k != 'path'}
            relative = entry['path'].relative_to(self.root).as_posix()
            item['url'] = self.file_url(relative)
            result.append(item)
        return result

    MEDIA = ('.mp4', '.webm')
    # Minutes/transcript side files, served as text beside the recordings.
    TEXT = ('.md', '.srt', '.vtt')

    def resolve_link(self, query, suffixes=MEDIA + TEXT):
        relative, expires, key = (query.get(name, [''])[0] for name in ('file', 'expires', 'key'))
        try:
            if int(expires) < time.time() or not hmac.compare_digest(key, self.sign(relative, expires)):
                raise ValueError('Recording link expired or invalid; refresh the recordings list')
        except (TypeError, ValueError) as exc:
            raise ValueError('Recording link expired or invalid; refresh the recordings list') from exc
        path = self.confined(self.root / relative)
        if path.suffix.lower() not in suffixes or not path.is_file():
            raise FileNotFoundError('Recording no longer exists')
        return path

    def open_local(self, key, action):
        if os.name != 'nt':
            raise ValueError('Local file opening is supported on Windows')
        if action not in ('open', 'reveal'):
            raise ValueError('Unknown recording action')
        entry = self.catalog().get(key)
        if entry is None:
            raise ValueError('Recording no longer exists')
        path = str(self.confined(entry['path']))
        if action == 'open':
            os.startfile(path)
        else:
            subprocess.Popen(['explorer.exe', '/select,', path], creationflags=subprocess.CREATE_NO_WINDOW)
