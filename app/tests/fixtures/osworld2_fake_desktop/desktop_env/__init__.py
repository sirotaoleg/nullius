import json
import os
import time

_sleep = time.sleep


def _recorded(seconds):
    if seconds == 60 and os.environ.get("FAKE_DESKTOP_LOG"):
        with open(os.environ["FAKE_DESKTOP_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "wait", "seconds": seconds}) + "\n")
        return
    _sleep(seconds)


time.sleep = _recorded
