import base64
import json
import os

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
MAX_RETRIES = 5


class EnvironmentSetupError(RuntimeError):
    pass


class _Controller:
    def __init__(self, env):
        self.env = env

    def execute_python_command(self, command):
        first = command.splitlines()[0] if command else ""
        self.env.record("execute", first=first[:200])
        if command.rstrip().endswith("print('made')"):
            return {"status": "success", "output": "made\n", "error": "", "returncode": 0}
        return {"status": "success", "output": f"{len(command)} {first}", "error": "", "returncode": 0}

    def get_file(self, file_path):
        self.env.record("get_file", path=file_path)
        return self.env.files.get(file_path)

    def get_screenshot(self):
        return PNG

    def start_recording(self):
        self.env.record("start_recording")

    def end_recording(self, dest):
        self.env.record("end_recording")


class _SetupController:
    def __init__(self, env):
        self.env = env

    def _upload_file_setup(self, files):
        for f in files:
            with open(f["local_path"], "rb") as src:
                self.env.files[f["path"]] = src.read()
        self.env.record("upload", paths=[f["path"] for f in files])


class DesktopEnv:
    def __init__(self, **kwargs):
        self.client_password = kwargs.get("client_password") or "osworld-public-evaluation"
        self.enable_proxy = kwargs.get("enable_proxy", False)
        self.controller, self.setup_controller = _Controller(self), _SetupController(self)
        self.files, self.actions, self.action_history = {}, [], []
        self.task, self.instruction, self.user_simulator = None, None, None
        self.is_environment_used = False
        self.record("started", **{k: kwargs.get(k) for k in ("path_to_vm", "client_password", "enable_proxy")})

    def record(self, event, **fields):
        log = os.environ.get("FAKE_DESKTOP_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"event": event, **fields}) + "\n")

    def reset(self, task_config=None, seed=None, options=None):
        self.task = task_config
        self.instruction = task_config.get("instruction") if task_config is not None else None
        self.actions = []
        self.record("reset", task=task_config.get("id") if task_config is not None else None)

    def _get_obs(self):
        return {"screenshot": PNG}

    def step(self, action, pause=2):
        self.actions.append(action)
        self.action_history.append(action)
        self.record("step", action=action, pause=pause)
        if action not in ("DONE", "FAIL", "WAIT"):
            self.controller.execute_python_command(action)
        return self._get_obs(), 0, action in ("DONE", "FAIL"), {}

    def evaluate(self):
        self.record("evaluate", last=self.actions[-1] if self.actions else None)
        return self.task.evaluate(self)

    def close(self):
        self.record("close")
