#!/usr/bin/env python3
import subprocess
import re
import csv
import os
import json
import shlex
import resource
import time
from resource import getrusage
from pathlib import Path
from typing import List, Dict, Optional, Any, IO, Tuple
import tempfile
import multiprocessing
from dataclasses import dataclass
from threading import Thread
from tracedump.tracedumpd import record  # type: ignore

ROOT = Path(os.path.dirname(os.path.realpath(__file__)))
CPU_COUNT = multiprocessing.cpu_count()
PLATFORM = "amd64-linux.gcc"
parsecmgmt = ROOT.joinpath("bin/parsecmgmt")


class InputPipe:
    def __init__(self, input: Optional[str]):
        self.input = input
        self.read_file: Optional[IO[str]] = None
        self.write_file: Optional[IO[str]] = None

    def _feed_pipe(self):
        if self.write_file:
            self.write_file.write(self.input)
            self.write_file.close()

    def __enter__(self) -> Optional[IO[str]]:
        if not self.input:
            return None
        read_fd, write_fd = os.pipe()
        self.read_file = os.fdopen(read_fd)
        self.write_file = os.fdopen(write_fd, mode="w")
        Thread(target=self._feed_pipe).start()
        return self.read_file

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self.read_file:
            self.read_file.close()
        if self.write_file:
            self.write_file.close()


@dataclass
class Command:
    args: str
    env: Dict[str, str]
    input_archive: Optional[Path]

    def _unpack(self, cwd: str, input_via_stdin: bool) -> Optional[str]:
        if self.input_archive is None:
            return None

        unpack_cmd = [
            "tar",
            "--no-same-owner",
            "-C",
            cwd,
            "-vxf",
            str(self.input_archive),
        ]
        print(f"$ {' '.join(unpack_cmd)}")
        subprocess.run(unpack_cmd, check=True)
        input: Optional[str] = None
        if input_via_stdin:
            with open(Path(cwd).joinpath("input.template")) as f:
                input = f.read()
                return input.replace("NUMPROCS", self.env["NTHREADS"])
        return None

    def _trace_run(
        self, cmd: List[str], stdin: Optional[IO[Any]], cwd: str = "."
    ) -> Tuple["resource._RUsage", float]:
        recording = record(
            record_path=Path(cwd),
            log_path=Path(cwd),
            target=cmd,
            working_directory=Path(cwd),
            extra_env=self.env,
            stdin=stdin,
        )
        return recording.rusage, recording.wall_time

    def _run(
        self, cmd: List[str], stdin: Optional[IO[Any]], cwd: str = "."
    ) -> Tuple["resource._RUsage", float]:
        start = time.time()
        proc = subprocess.Popen(cmd, env=self.env, cwd=cwd, stdin=stdin)
        _, exit_code, rusage = os.wait4(proc.pid, 0)
        wall_time = time.time() - start
        assert exit_code == 0
        return rusage, wall_time

    def run(
        self, simulate: bool = False, cwd: str = ".", trace: bool = False
    ) -> Tuple["resource._RUsage", float]:
        input = None
        args = shlex.split(self.args)
        if args[0].endswith("x264"):
            # https://github.com/cirosantilli/parsec-benchmark#host-x264
            args[0] = "x264"
            args.remove("--b-pyramid")
        cmd = args
        input_via_stdin = False
        if cmd[-2] == "<":
            input_via_stdin = True
            cmd.pop()
            cmd.pop()
        input = self._unpack(cwd, input_via_stdin)
        print(f"$ cd {cwd}")
        print(
            f"$ LD_LIBRARY_PATH={self.env['LD_LIBRARY_PATH']} {' '.join(cmd)}"
            + (" << EOF" if input_via_stdin else "")
        )
        if input:
            print(input, end="")
            print("EOF")
        if simulate:
            # dummy value
            return getrusage(os.getpid()), 0.0
        with InputPipe(input) as read_file:
            if trace:
                return self._trace_run(cmd, cwd=cwd, stdin=read_file)
            else:
                return self._run(cmd, cwd=cwd, stdin=read_file)


@dataclass
class App:
    name: str
    framework: str

    def root_path(self) -> Path:
        if self.framework == "parsec":
            return ROOT.joinpath("pkgs", "apps", self.name)
        elif self.framework == "splash2" or self.framework == "splash2x":
            return ROOT.joinpath("ext", self.framework, "apps", self.name)
        raise Exception(f"unknown framework {self.framework}")

    def source_env(self, path: Path) -> Dict[str, str]:
        wrapper = str(path.joinpath("inst", PLATFORM, "bin", "run.sh"))
        cpu_count = CPU_COUNT
        script = f"""
            export NTHREADS={cpu_count}
            export NUMPROCS={cpu_count}
            export INPUTSIZE=native
            export PARSECPLAT={PLATFORM}
            export PARSECDIR={ROOT}
            export LD_LIBRARY_PATH="$PARSECDIR/pkgs/libs/hooks/inst/$PARSECPLAT/lib:$LD_LIBRARY_PATH"
            source {path.joinpath('parsec', 'native.runconf')}
            # mock eval
            eval(){{
              :
            }}
            if [ -f {wrapper} ]; then
              source {wrapper};
            fi
            export run_exec run_args RUN
            echo __EXPORT_START__
            jq -n env
        """
        proc = subprocess.run(
            ["bash", "-c", script], text=True, stdout=subprocess.PIPE, check=True
        )
        assert proc.stdout is not None

        env_str = ""
        export_starts = False
        for line in proc.stdout.split("\n"):
            if line == "__EXPORT_START__":
                export_starts = True
                continue

            if export_starts:
                env_str += line
        assert env_str != ""
        return json.loads(env_str)

    def command(self) -> Command:
        path = self.root_path()
        env = self.source_env(path)
        assert "run_exec" in env
        assert "run_args" in env
        inputs_dir = path.joinpath("inputs")
        input_archive: Optional[Path] = None
        if inputs_dir.exists():
            size = "test" if self.framework == "splash2" else "native"
            input_archive = inputs_dir.joinpath(f"input_{size}.tar")
        if self.framework != "parsec":
            executable = env["RUN"]
            return Command(args=executable, env=env, input_archive=input_archive)
        executable = str(path.joinpath("inst", PLATFORM, env["run_exec"]))
        return Command(
            args=executable + " " + env["run_args"],
            input_archive=input_archive,
            env=env,
        )


def get_apps() -> List[App]:
    apps = []
    proc = subprocess.run(
        [parsecmgmt, "-a", "status", "-p", "apps"],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout.split("\n"):
        match = re.match(r"\[PARSEC\] (splash2x|parsec|)\.([^ ]+)", line)
        if not match:
            continue
        framework = match.group(1)
        name = match.group(2)
        apps.append(App(name, framework))
    return apps


def main() -> None:
    simulate = False
    benchmarks = {}
    if os.path.exists("benchmarks.json"):
        with open("benchmarks.json") as f:
            benchmarks = json.load(f)

    fields = [
        "ru_idrss",
        "ru_inblock",
        "ru_isrss",
        "ru_ixrss",
        "ru_majflt",
        "ru_maxrss",
        "ru_minflt",
        "ru_msgrcv",
        "ru_msgsnd",
        "ru_nivcsw",
        "ru_nsignals",
        "ru_nswap",
        "ru_nvcsw",
        "ru_oublock",
        "ru_stime",
        "ru_utime",
    ]
    for app in get_apps():
        cmd = app.command()
        if (
            app.name in benchmarks
            or app.framework == "splash2"
            or app.name == "raytrace"
        ):
            print(f"skip {app.name}")
            continue

        with tempfile.TemporaryDirectory() as temp:
            benchmarks[app.name] = []
            for i in range(5):
                for trace in [True, False]:
                    usage, wall_time = cmd.run(simulate=simulate, cwd=temp, trace=trace)
                    usage_dict = {}
                    for field in fields:
                        usage_dict[field] = getattr(usage, field)
                    usage_dict["name"] = app.name
                    usage_dict["type"] = "trace" if trace else "normal"
                    usage_dict["wall_time"] = wall_time
                    benchmarks[app.name].append(usage_dict)
        with open("benchmarks.json", "w") as f:
            json.dump(benchmarks, f)

    with open("benchmarks.csv", "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=["name", "type", "wall_time"] + fields)
        writer.writeheader()

        for app, measurements in benchmarks.items():
            for measurement in measurements:
                writer.writerow(measurement)


if __name__ == "__main__":
    main()
