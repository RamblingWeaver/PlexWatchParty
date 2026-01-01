class DummyPart:
    def __init__(self, file):
        self.file = file


class DummyMedia:
    def __init__(self, key, parts):
        self.key = key
        self.parts = parts


class DummyItem:
    def __init__(self, key, title="file"):
        self.key = key
        self.title = title


class DummyClientObj:
    def __init__(self, name="Local", machine="machine-1"):
        self.play_calls = []
        self.play_called = False
        self.pause_called = False
        self.seek_calls = []
        self.stop_called = False
        self.title = name
        self.machineIdentifier = machine

    def playMedia(self, media, *args, **kwargs):
        # Accept either a positional offset or keyword `offset` and normalize to milliseconds
        offset_ms = None
        # Positional offset (first extra arg) may be provided by some clients
        if args:
            off = args[0]
            if isinstance(off, (int, float)):
                offset_ms = int(off) * 1000 if off < 100000 else int(off)
        # If offset provided as kwarg, prefer that
        if "offset" in kwargs:
            off = kwargs.get("offset")
            if isinstance(off, (int, float)):
                offset_ms = int(off) * 1000 if off < 100000 else int(off)
        self.play_calls.append((media, {"offset_ms": offset_ms}))

    def play(self):
        self.play_called = True

    def pause(self):
        self.pause_called = True

    def seekTo(self, ms):
        # Normalize incoming seek value to milliseconds. If the value looks
        # small (likely seconds), convert to ms so tests can assert ms.
        if isinstance(ms, (int, float)) and ms < 100000:
            self.seek_calls.append(int(ms) * 1000)
        else:
            self.seek_calls.append(ms)

    def stop(self):
        self.stop_called = True

    def proxyThroughServer(self):
        return None
