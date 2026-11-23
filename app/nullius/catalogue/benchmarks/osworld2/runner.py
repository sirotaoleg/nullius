import argparse
import datetime
import json
import logging
import os
import shutil
import sys
import tempfile
import threading

SCREEN = (1920, 1080)
FINISH_SECONDS = 5
CLOSE_SECONDS = 60
STOP_SECONDS = FINISH_SECONDS + CLOSE_SECONDS
FOLDER = "/tmp/agent"
SETUP_ATTEMPTS = 2
EVALUATION_FAILED = "evaluation_failed.json"
MULTI_PHASE = "a multi-phase task: agent.computer/v1 gives the agent one instruction"


def _evaluated(where, unit):
    try:
        from desktop_env.evaluators.errors import consume_evaluation_infrastructure_error
    except ImportError:
        return
    error = consume_evaluation_infrastructure_error()
    if error is not None:
        with open(os.path.join(where, EVALUATION_FAILED), "w", encoding="utf-8") as f:
            json.dump({"error": type(error).__name__}, f)
        logging.error(f"the upstream's evaluation of {unit} said a dependency of its own failed: {type(error).__name__}")


class Desktop:
    def __init__(self, env, scratch):
        self.env, self.scratch, self.shot = env, scratch, None

    def observation(self):
        return self.shot

    def python(self, code):
        return self.env.controller.execute_python_command(code)

    def upload(self, path, data):
        fd, local = tempfile.mkstemp(dir=self.scratch)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            self.env.setup_controller._upload_file_setup([{"local_path": local, "path": path}])
        finally:
            os.unlink(local)

    def read(self, path):
        return self.env.controller.get_file(path)

    def capture(self):
        png = self.env.controller.get_screenshot()
        if not png:
            raise RuntimeError("the desktop gave no screenshot")
        return png

    def password(self):
        return self.env.client_password


def judged(fd):
    with os.fdopen(fd, "rb") as f:
        said = f.read()
    if not said:
        raise SystemExit(1)
    os.environ.update(json.loads(said))


class Watched:
    def __init__(self, env, desktop, judge=None):
        object.__setattr__(self, "_env", env)
        object.__setattr__(self, "_desktop", desktop)
        object.__setattr__(self, "_judge", judge)

    def step(self, *args, **kwargs):
        out = self._env.step(*args, **kwargs)
        obs = out[0] if isinstance(out, tuple) and out else None
        self._desktop.shot = obs.get("screenshot") if isinstance(obs, dict) else None
        return out

    def evaluate(self, *args, **kwargs):
        if self._judge is not None:
            fd = self._judge
            object.__setattr__(self, "_judge", None)
            judged(fd)
        return self._env.evaluate(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._env, name)

    def __setattr__(self, name, value):
        setattr(self._env, name, value)


class Counted:
    def __init__(self, actions, count):
        self.actions, self.count = actions, count

    def __iter__(self):
        return iter(self.actions)

    def __len__(self):
        return self.count

    def __bool__(self):
        return True


def bridge_class(base):
    class Bridge(base):
        def predict(self, instruction, obs):
            response, actions = super().predict(instruction, obs)
            if isinstance(actions, list) and not actions:
                return response, ["DONE"]
            return response, actions

        def _run(self, call):
            return Counted(super()._run(call), len(call.actions) + bool(call.end))

    return Bridge


class Turn:
    bridge = given = said = None

    def end_turn(self):
        if self.bridge is not None:
            self.bridge.end_turn()

    def close_window(self):
        if self.bridge is not None:
            self.bridge.close_window()
        if self.given and self.said:
            self.said(self.given, ok=self.bridge is not None)


def close_within(env, seconds):
    done = []

    def close():
        env.close()
        done.append(True)

    worker = threading.Thread(target=close, name="desktop-close", daemon=True)
    worker.start()
    worker.join(seconds)
    return bool(done)


def task_date(value):
    if isinstance(value, datetime.date):
        return value.isoformat()[:10]
    try:
        return datetime.date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        return None


def main(argv=None):
    p = argparse.ArgumentParser()
    for name in ("--examples", "--domain", "--task-id", "--result-dir", "--socket", "--ready", "--vm", "--calls",
                 "--password"):
        p.add_argument(name, required=True)
    for name in ("--held", "--prepared", "--given"):
        p.add_argument(name)
    p.add_argument("--lifeline", type=int, required=True)
    p.add_argument("--judge", type=int)
    p.add_argument("--steps", type=int, required=True)
    p.add_argument("--pause", type=float, required=True)
    a = p.parse_args(argv)
    from nullius.standard import computer_port as port
    stop, turn = port.Stop(STOP_SECONDS), Turn()
    turn.given, turn.said = a.given, port.ready
    try:
        stop.install()
        threading.Thread(target=port.lifeline, args=(a.lifeline, turn), name="lifeline", daemon=True).start()
        return run(a, port, stop, turn)
    finally:
        try:
            os.unlink(a.ready)
        except OSError:
            pass
        shutil.rmtree(os.path.dirname(a.socket), ignore_errors=True)


def load(a):
    import lib_run_single
    from desktop_env import desktop_env
    from task_loader import find_task_class_path, load_task_config, resolve_task_json_path
    if not find_task_class_path(a.task_id, a.examples, a.domain, "v2"):
        raise FileNotFoundError("no task class")
    example = load_task_config(resolve_task_json_path(task_id=a.task_id, base_dir=a.examples, domain=a.domain,
                                                      eval_version="v2"),
                               task_id=a.task_id, base_dir=a.examples, domain=a.domain, eval_version="v2")
    return lib_run_single, desktop_env, example, bool(lib_run_single._get_task_phases(example))


def run(a, port, stop, turn):
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s %(levelname)s %(name)s] %(message)s")
    try:
        lib_run_single, desktop_env, example, phased = load(a)
    except (Exception, SystemExit) as e:
        port.ready(a.ready, ok=False, error=f"the task did not load: {type(e).__name__}")
        return 3
    if phased:
        port.ready(a.ready, ok=False, error=MULTI_PHASE)
        return 3
    desktop_env.MAX_RETRIES = SETUP_ATTEMPTS
    where = os.path.join(a.result_dir, a.domain, a.task_id)
    env = bridge = server = None
    said, failed, code = [], "", 0

    def first():
        port.ready(a.ready, ok=True, password=env.client_password or "",
                   date=task_date(getattr(bridge, "task_current_date", None)))
        said.append(True)

    try:
        env = desktop_env.DesktopEnv(path_to_vm=a.vm, action_space="pyautogui", provider_name="docker",
                                     screen_size=SCREEN, headless=True, os_type="Ubuntu", require_a11y_tree=False,
                                     enable_proxy=True, client_password=a.password,
                                     instance_type=example.get("instance_type"), volume_size=example.get("volume_size"),
                                     force_disable_vnc=True, force_disable_recording=True)
        os.makedirs(where, exist_ok=True)
        desktop = Desktop(env, os.path.dirname(os.path.abspath(a.calls)))
        bridge = bridge_class(port.Bridge)(desktop, a.steps, a.pause, SCREEN, FOLDER, on_first=first,
                                           held=port.Held(a.held) if a.held else None, window=bool(a.given),
                                           prepared=a.prepared)
        server = port.Server(bridge, a.socket, a.calls)
        turn.bridge = bridge
        lib_run_single.run_single_example(bridge, Watched(env, desktop, a.judge), example, a.steps, example["instruction"],
                                          argparse.Namespace(sleep_after_execution=a.pause, result_dir=a.result_dir,
                                                             checkpoint_eval_mode="off"),
                                          where, [])
        _evaluated(where, f"{a.domain}__{a.task_id}")
    except SystemExit:
        failed, code = "the runner was stopped", 1
    except BaseException as e:
        failed, code = type(e).__name__, 1
        logging.exception("runner: %s", failed)
    finally:
        with stop.deferred(then_stop=False):
            if not said:
                port.ready(a.ready, ok=False, error="the task's setup: " + (failed or "the loop ended before the "
                                                                                       "agent's first step"))
            if bridge is not None:
                bridge.finish()
            if server is not None:
                server.close()
            if env is not None and not close_within(env, CLOSE_SECONDS):
                sys.stderr.write(f"runner: the desktop did not close within {CLOSE_SECONDS} s\n")
                code = code or 1
    return code


if __name__ == "__main__":
    sys.exit(main())
