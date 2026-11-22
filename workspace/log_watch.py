"""Read-only notifications of local provider log writes (low latency, 2026-11-14).

The page used to learn of an external agent's work by polling: running/idle every 3 s, its chat and the floor every 10 s.
This watches the providers' own stores and calls `publish({path: provider})` within milliseconds of a write, so the server
can push a small `provider/changed` event over the stream it already has. No log contents are read here: the providers'
incremental parsers stay the only readers.

What is watched is decided by a rule per store (`roots`: path -> (provider, keep)), where `keep(parts, is_dir)` gets the
path's parts below the store root. Only the files the adapters read are kept: transcripts, registrations, the Codex state
database; never a project's working tree, never the busy logging databases beside them.

How (macOS/BSD): a kqueue vnode watch per directory (an entry added, removed or renamed) and per recently written file
(appended to). The thread blocks on the kqueue, so a write is seen at once; events arriving within SETTLE of the first are
published together. Descriptors are limited (FD_BUDGET), so only recently written files ("hot", HOT_SECONDS) and recently
changed directories get one; the rest are cold and checked by their metadata (inode, size, mtime) in slices, each about
every COLD_CYCLE seconds. A cold file that changes becomes hot at once, with the folder of its subagents beside it. Without
kqueue (Linux), hot paths are checked every TICK instead. Numbers are judgement, to be checked against the latency
harness's measurements, not measured optima.
"""
import os
from pathlib import Path
import select
import threading
import time

TICK = 0.2            # s: the longest the thread blocks; also how often hot paths without a native watch are checked
SETTLE = 0.015        # s: after the first event, how long further events are gathered into the same publish
COLD_CYCLE = 3.0      # s: about how often each cold path is checked
HOT_SECONDS = 6 * 3600          # a file or folder written within this long gets a native watch
FD_WANT = 4096        # the descriptor limit asked for (the macOS default soft limit is 256)


def descriptor_budget(want=FD_WANT):
    """How many watches to hold: half the process's descriptor limit, raised towards `want` when the hard limit allows,
    less a reserve for HTTP connections, SQLite and the providers' own files."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = want if hard == resource.RLIM_INFINITY else min(want, hard)
        if soft != resource.RLIM_INFINITY and soft < target:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
                soft = target
            except (ValueError, OSError):
                pass
        limit = want if soft == resource.RLIM_INFINITY else min(soft, want)
    except ImportError:
        limit = 256
    return max(0, limit // 2 - 64)


def signature(path):
    try:
        s = path.stat()
        return s.st_ino, s.st_size, s.st_mtime_ns
    except OSError:
        return None


class Entry:
    __slots__ = ('provider', 'keep', 'root', 'is_dir', 'sig', 'fd', 'hot')

    def __init__(self, provider, keep, root, is_dir, sig):
        self.provider, self.keep, self.root, self.is_dir, self.sig = provider, keep, root, is_dir, sig
        self.fd, self.hot = None, False


class LogWatch:
    def __init__(self, roots, publish, native=True, tick=TICK, settle=SETTLE, cold_cycle=COLD_CYCLE, hot_seconds=HOT_SECONDS,
                 budget=None):
        self.roots = {Path(p): spec for p, spec in roots.items()}
        self.publish = publish
        self.tick, self.settle, self.cold_cycle, self.hot_seconds = tick, settle, cold_cycle, hot_seconds
        self.queue = select.kqueue() if native and hasattr(select, 'kqueue') else None
        self.budget = (descriptor_budget() if budget is None else budget) if self.queue else 0
        self.entries = {}          # path -> Entry
        self.children = {}         # folder -> names kept below it
        self.by_fd = {}
        self.cold, self.cold_at, self.cold_last = [], 0, 0.0
        self.missing = set(self.roots)
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None
        self.lock = threading.Lock()

    # ---------- the inventory ----------
    def kept(self, root, path, is_dir):
        provider, keep = self.roots[root]
        parts = path.relative_to(root).parts
        if parts and parts[-1].startswith('.'):
            return False
        return keep(parts, is_dir)

    def add(self, path, root, is_dir, changed, now):
        sig = signature(path)
        if sig is None:
            return
        provider, keep = self.roots[root]
        e = self.entries.get(path)
        if e is None:
            e = self.entries[path] = Entry(provider, keep, root, is_dir, sig)
            if changed is not None and not is_dir:
                changed[path] = provider          # a new transcript, registration or lock
        recent = now - sig[2] / 1e9 < self.hot_seconds
        if is_dir:
            names = set()
            try:
                with os.scandir(path) as it:
                    items = [(x.name, x.is_dir(follow_symlinks=False)) for x in it]
            except OSError:
                items = []
            for name, sub in items:
                p = path / name
                if self.kept(root, p, sub):
                    names.add(name)
                    if p not in self.entries:
                        self.add(p, root, sub, changed, now)
            self.children[path] = names
            # every folder the adapters list from is kept warm: the store's root and the folders just below it
            if recent or len(path.relative_to(root).parts) <= 1:
                self.heat(path)
            else:
                self.cold.append(path)
        elif recent:
            self.heat(path)
        else:
            self.cold.append(path)

    def drop(self, path, changed):
        e = self.entries.pop(path, None)
        if e is None:
            return
        self.unwatch(path, e)
        if e.is_dir:
            for name in self.children.pop(path, ()):
                self.drop(path / name, changed)
        elif changed is not None:
            changed[path] = e.provider          # a deleted or renamed-away log: its session is read again

    # ---------- native watches ----------
    def heat(self, path):
        e = self.entries.get(path)
        if e is None or e.hot:
            return
        e.hot = True
        if self.queue and len(self.by_fd) < self.budget:
            fd = None
            try:
                fd = os.open(path, getattr(os, 'O_EVTONLY', os.O_RDONLY) | os.O_CLOEXEC)
                flags = select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE
                self.queue.control([select.kevent(fd, filter=select.KQ_FILTER_VNODE, flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                                  fflags=flags)], 0, 0)
                e.fd = fd
                self.by_fd[fd] = path
            except OSError:
                if fd is not None:
                    os.close(fd)

    def unwatch(self, path, e):
        if e.fd is not None:
            self.by_fd.pop(e.fd, None)
            try:
                os.close(e.fd)          # closing the descriptor removes its kevent
            except OSError:
                pass
            e.fd = None
        e.hot = False

    # ---------- what changed ----------
    def check(self, path, changed, now):
        """Compare a path with its last metadata; a changed file is reported and made hot, a changed folder re-listed."""
        e = self.entries.get(path)
        if e is None:
            return
        sig = signature(path)
        if sig == e.sig:
            return
        if sig is None or (e.sig and sig[0] != e.sig[0]):
            # gone, or replaced by another file under the same name: watched again from scratch
            root, is_dir = e.root, e.is_dir
            self.drop(path, changed)
            if sig is not None:
                self.add(path, root, is_dir, changed, now)
                if not is_dir:
                    changed[path] = self.roots[root][0]
            return
        e.sig = sig
        if e.is_dir:
            self.relist(path, changed, now)
        else:
            changed[path] = e.provider
        if not e.hot:
            self.heat(path)
            if not e.is_dir:          # a resumed old session: the folder its subagents write into is warmed with it
                d = path.with_suffix('')
                for p in [d] + [d / n for n in self.children.get(d, ())]:
                    if p in self.entries and self.entries[p].is_dir:
                        self.heat(p)

    def relist(self, folder, changed, now):
        e = self.entries.get(folder)
        if e is None:
            return
        try:
            with os.scandir(folder) as it:
                now_names = {x.name: x.is_dir(follow_symlinks=False) for x in it}
        except OSError:
            self.drop(folder, changed)
            return
        old = self.children.get(folder, set())
        kept = {n for n, sub in now_names.items() if self.kept(e.root, folder / n, sub)}
        for n in old - kept:
            self.drop(folder / n, changed)
        for n in kept - old:
            self.add(folder / n, e.root, now_names[n], changed, now)
        self.children[folder] = kept
        # a file replaced by rename (a registration written atomically) keeps its name: one without a watch of its own
        # is compared here, since its folder's event is the only sign
        for n in kept & old:
            x = self.entries.get(folder / n)
            if x is not None and not x.is_dir and x.fd is None:
                self.check(folder / n, changed, now)

    def scan_roots(self, changed, now):
        for root in list(self.missing):
            if root.is_dir():
                self.missing.discard(root)
                self.add(root, root, True, changed, now)

    def step(self, wait):
        """One turn: wait up to `wait` for native events, gather those within SETTLE, then the paths checked by metadata.
        Returns {path: provider} of what changed."""
        changed, now_m = {}, time.monotonic()
        paths = set()
        if self.queue:
            events = self.queue.control(None, 512, wait)
            if events:
                deadline = time.monotonic() + self.settle
                while True:
                    paths.update(self.by_fd.get(ev.ident) for ev in events)
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    events = self.queue.control(None, 512, left)
                    if not events:
                        break
        elif wait:
            self.stop_event.wait(wait)
        now = time.time()
        # the watched paths that fired, then hot paths without a watch (no kqueue, or over the budget)
        paths.discard(None)
        paths.update(p for p, e in self.entries.items() if e.hot and e.fd is None)
        for p in sorted(paths, key=lambda p: (not self.entries[p].is_dir) if p in self.entries else True):
            self.check(p, changed, now)
        # a slice of the cold paths, so each is checked about every cold_cycle seconds
        if time.monotonic() - self.cold_last >= self.tick:
            self.cold_last = time.monotonic()
            if self.cold_at >= len(self.cold):
                self.cold_at = 0
                self.cool(now)
                self.scan_roots(changed, now)
            n = max(1, int(len(self.cold) * self.tick / self.cold_cycle) + 1)
            for p in self.cold[self.cold_at:self.cold_at + n]:
                e = self.entries.get(p)
                if e is not None and not e.hot:
                    self.check(p, changed, now)
            self.cold_at += n
        return changed

    def cool(self, now):
        """Once a cycle: watches on paths quiet for HOT_SECONDS are given back; the cold list is rebuilt."""
        for p, e in self.entries.items():
            if e.hot and e.sig and now - e.sig[2] / 1e9 > self.hot_seconds and not (e.is_dir and len(p.relative_to(e.root).parts) <= 1):
                self.unwatch(p, e)
        self.cold = [p for p, e in self.entries.items() if not e.hot]
        if self.queue:          # watches freed above go to hot paths still waiting for one
            for p, e in self.entries.items():
                if e.hot and e.fd is None and len(self.by_fd) < self.budget:
                    e.hot = False
                    self.heat(p)

    # ---------- the thread ----------
    def start(self, wait=True):
        """The baseline scan (nothing is reported for what was already there; about 0.6 s for one real set of stores on
        2026-11-14) runs on the thread; `wait` holds until it is done."""
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run, name='agent-log-watch', daemon=True)
        self.thread.start()
        if wait:
            self.ready.wait()
        return self

    def run(self):
        try:
            self.scan_roots(None, time.time())
            self.cold = [p for p, e in self.entries.items() if not e.hot]
        except Exception as e:
            self.error = repr(e)
        finally:
            self.ready.set()
        while not self.stop_event.is_set():
            try:
                changed = self.step(self.tick)
                if changed:
                    self.publish(changed)
                self.error = None
            except Exception as e:          # an unreadable folder for a moment must not end the watching
                self.error = repr(e)
                self.stop_event.wait(self.tick)

    def stats(self):
        return {'paths': len(self.entries), 'watched': len(self.by_fd), 'hotUnwatched': sum(1 for e in self.entries.values() if e.hot and e.fd is None),
                'cold': sum(1 for e in self.entries.values() if not e.hot), 'budget': self.budget, 'native': bool(self.queue), 'error': self.error}

    def close(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        for e in self.entries.values():
            if e.fd is not None:
                try:
                    os.close(e.fd)
                except OSError:
                    pass
                e.fd = None
        self.by_fd.clear()
        if self.queue:
            self.queue.close()
            self.queue = None
