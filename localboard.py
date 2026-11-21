#!/usr/bin/env python3
"""Score a frozen Git revision on the Docker host using private, fingerprinted inputs."""

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import secrets
import selectors
import shlex
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import fcntl
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


def output(*args, **options):
    return subprocess.check_output(args, **options)


def run(*args, **options):
    subprocess.run(args, check=True, **options)


def fingerprint(directory):
    digest = hashlib.sha256()
    for path in [
        directory / "tests.json",
        directory / "answers.json",
        *sorted((directory / "commits").glob("*.patch")),
    ]:
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def ratios(path):
    values = [[], []]
    for row in path.read_text().splitlines():
        cells = [s.strip().strip("`") for s in row.strip("|").split("|")]
        if len(cells) != 7 or not cells[4].replace(".", "", 1).isdigit():
            continue
        for i, xs in enumerate(values):
            if cells[i + 1].replace(".", "", 1).isdigit():
                xs.append(Decimal(cells[4]) / Decimal(cells[i + 1]))
    return [statistics.median(v) if v else None for v in values]


def matched_ratio(path, heading="Matched commit"):
    pairs = []
    reading = False
    for line in path.read_text().splitlines():
        if line.startswith(f"| {heading} |"):
            reading = True
            continue
        if not line.startswith("|"):
            reading = False
        if reading:
            cells = [s.strip() for s in line.strip("|").split("|")]
            if len(cells) == 4 and all(s.replace(".", "", 1).isdigit() for s in cells[1:3]):
                pairs.append(Decimal(cells[2]) / Decimal(cells[1]))
    return statistics.median(pairs) if pairs else None


def rounded(value):
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def historical_forecast(local, anchors, realized_fraction):
    """Interpolate measured board totals. Discount gains outside the measured range."""
    if not anchors or not 0 < realized_fraction <= 1:
        raise ValueError("invalid historical calibration")
    grouped = {}
    for measured, board in anchors:
        grouped.setdefault(Decimal(str(measured)), []).append(Decimal(str(board)))
    points = sorted((x, statistics.median(y)) for x, y in grouped.items())
    x0, y0 = points[0]
    if local <= x0:
        return y0 + (local - x0) * realized_fraction
    for x, y in points[1:]:
        if local <= x:
            return y0 + (local - x0) * (y - y0) / (x - x0)
        x0, y0 = x, y
    return y0 + local - x0


@contextlib.contextmanager
def container(name, options):
    run("docker", "create", "--name", name, *options, stdout=subprocess.DEVNULL)
    try:
        yield
    finally:
        run("docker", "rm", "-f", name, stdout=subprocess.DEVNULL)


def limited_output(name, seconds, cap):
    deadline = time.monotonic() + seconds
    proc = subprocess.Popen(["docker", "start", "-a", name], stdout=subprocess.PIPE)
    result = bytearray()
    try:
        with selectors.DefaultSelector() as monitor:
            monitor.register(proc.stdout, selectors.EVENT_READ)
            while True:
                left = deadline - time.monotonic()
                if left <= 0 or not monitor.select(left):
                    raise ValueError("rejected: wall timeout")
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                result.extend(chunk)
                if len(result) > cap:
                    raise ValueError("rejected: stdout limit")
        if proc.wait(timeout=max(0, deadline - time.monotonic())):
            raise ValueError("rejected: nonzero container exit")
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return result


def cpu_usage(path):
    stats = dict(line.split() for line in path.read_text().splitlines())
    return int(stats["usage_usec"])


@contextlib.contextmanager
def docker_run_lock(config):
    path = config.get("docker_run_lock") or os.environ.get("BLAST_LOCALBOARD_RUN_LOCK")
    if path is None:
        yield
    else:
        with open(path, "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            yield


def evaluate(config, confdir, archive, work, sha):
    kit = (confdir / config["kit"]).resolve()
    scorer_path = kit / "score.py"
    if hashlib.sha256(scorer_path.read_bytes()).hexdigest() != config["scorer_sha256"]:
        raise ValueError("official scorer fingerprint changed")
    spec = importlib.util.spec_from_file_location("official_score", scorer_path)
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    boards = {}
    for label, spec in config["datasets"].items():
        directory = (confdir / spec["path"]).resolve()
        if fingerprint(directory) != spec["sha256"]:
            raise ValueError(f"{label} fingerprint changed")
        answers = json.loads((directory / "answers.json").read_bytes())["commits"]
        if set(answers) != {p.stem for p in (directory / "commits").glob("*.patch")}:
            raise ValueError(f"{label} has unpaired answers and patches")
        tests = json.loads((directory / "tests.json").read_bytes())["tests"]
        boards[label] = directory, answers, tests

    driver = (
        output("docker", "info", "--format", "{{.CgroupDriver}} {{.CgroupVersion}}")
        .decode()
        .strip()
    )
    if driver != "systemd 2":
        raise ValueError(
            "CPU accounting requires a local Linux Docker daemon with systemd and cgroup v2"
        )
    image = "eagleeye-blast-localboard:" + sha
    iidfile = work / "image.id"
    allowed = sorted(os.sched_getaffinity(0))
    if "cpu_ids" in config:
        allowed = sorted(set(allowed).intersection(config["cpu_ids"]))
    if len(allowed) < 4:
        raise ValueError("four build CPUs are required")
    context = work / "build-context"
    context.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(context, filter="data")
    run(
        "docker",
        "build",
        "--platform",
        "linux/amd64",
        "--cpuset-cpus",
        ",".join(map(str, allowed[:4])),
        "--memory",
        "8g",
        "--memory-swap",
        "8g",
        "--iidfile",
        str(iidfile),
        "-t",
        image,
        str(context),
        stdout=sys.stderr,
        env=dict(os.environ, DOCKER_BUILDKIT="0"),
        timeout=600,
    )
    iid = iidfile.read_text().strip()
    prefix = "blastboard" + secrets.token_hex(8)
    parent = prefix + ".slice"
    flags = [
        "--cpuset-cpus", ",".join(map(str, allowed)),
        "--cgroup-parent",
        parent,
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--cpus",
        "2",
        "--memory",
        "4096m",
        "--memory-swap",
        "4096m",
        "--pids-limit",
        "256",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m,mode=1777",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "65534:65534",
        "-e",
        "HOME=/tmp",
    ]
    if config.get("assertion_report", False):
        flags += ["-e", "BLAST_ASSERTION_REPORT=1"]
    control = prefix + "-control"
    results = {}
    print(f"Frozen commit {sha}, image {iid}", flush=True)
    # A paused sibling keeps the private slice alive after the contestant exits.
    with container(control, flags + ["--entrypoint", "sleep", iid, "infinity"]):
        with docker_run_lock(config):
            run("docker", "start", control, stdout=subprocess.DEVNULL)
            run("docker", "pause", control, stdout=subprocess.DEVNULL)
        stat = Path("/sys/fs/cgroup") / parent / "cpu.stat"
        for label, (directory, answers, tests) in boards.items():
            trial_totals = []
            for trial in range(1, 4):
                name = prefix + "-entry"
                mounts = [
                    "-v",
                    f"{directory}/commits:/data/commits:ro",
                    "-v",
                    f"{directory}/tests.json:/data/tests.json:ro",
                ]
                with docker_run_lock(config), container(name, flags + mounts + [iid]):
                    start = cpu_usage(stat)
                    t = time.monotonic()
                    stdout = limited_output(name, 180, 2_000_000)
                    elapsed = time.monotonic() - t
                    used = cpu_usage(stat) - start
                    state = json.loads(
                        output("docker", "inspect", "-f", "{{json .State}}", name)
                    )
                    if state["ExitCode"] != 0 or state["OOMKilled"]:
                        raise ValueError(
                            f"{label} rejected: exit={state['ExitCode']} oom={state['OOMKilled']}"
                        )
                score = scorer.score_run(bytes(stdout), tests, answers)
                if not score["ok"]:
                    raise ValueError(f"{label} rejected: {score}")
                sim = score["simulated_milliseconds"]
                total = scorer.run_total_microseconds(sim, used)
                trial_totals.append(total)
                if label in config.get("record_datasets", []):
                    import gzip
                    dest = confdir / "localboard-runs" / sha / label
                    dest.mkdir(parents=True, exist_ok=True)
                    (dest / f"{trial}.jsonl.gz").write_bytes(gzip.compress(bytes(stdout), mtime=0))
                print(
                    f"{label} run={trial} n={len(answers)} sim={sim / 1000:.3f}s cpu={used / 1e6:.6f}s "
                    f"misses={score['misses']} total={total / 1e6:.6f}s wall={elapsed:.3f}s stdout={len(stdout)}B",
                    flush=True,
                )
            results[label] = rounded(
                Decimal(statistics.median(trial_totals)) / 1_000_000
            )
            print(f"{label} median={results[label]}s", flush=True)
    ledger = confdir / config["calibration"]
    if "proxy" in results:
        ratio = matched_ratio(ledger, config.get("matched_heading", "Matched commit"))
        if ratio is None:
            print("Prediction unavailable until historical proxy replays are calibrated.")
            return
        for label, title in (("proxy", "Predicted board"), ("shadow", "Disjoint batch sensitivity")):
            if label in results:
                scaled = rounded(results[label] * ratio)
                calibration = config.get("historical_response", {})
                if label in calibration:
                    fraction = Decimal(str(calibration["realized_fraction"]))
                    estimate = historical_forecast(results[label], calibration[label], fraction)
                    print(f"{label} median-ratio estimate = {scaled} s "
                          f"({results[label]} s x {ratio:.6f})")
                    print(f"{title} = {rounded(estimate)} s "
                          f"(historical response, gain fraction {fraction:.4f})")
                else:
                    print(f"{title} = {scaled} s "
                          f"({label} {results[label]} s x median matched ratio {ratio:.6f})")
        return
    public_ratio, blind_ratio = ratios(ledger)
    if public_ratio is None:
        raise ValueError("no completed public calibration pair")
    for label, ratio, title in [
        ("public", public_ratio, "Predicted board"),
        ("blind", blind_ratio, "Mutation sensitivity"),
    ]:
        if ratio is not None and label in results:
            print(
                f"{title} = {rounded(results[label] * ratio)} s ({label} {results[label]} s x median ratio {ratio:.6f})"
            )


def relay(config, archive, work, sha, calibration):
    package = work / "transfer.tar"
    with tarfile.open(package, "w") as tar:
        tar.add(archive, arcname="revision.tar.gz")
        tar.add(Path(__file__).resolve(), arcname="localboard.py")
        tar.add(calibration, arcname="calibration.md")
    # The remote staging tree is private and lives on the team's Docker host.
    script = (
        "set -eu; cd "
        + shlex.quote(config["remote_dir"])
        + "; d=$(mktemp -d .board.XXXXXXXX); "
        'trap \'rm -rf -- "$d"\' EXIT; tar -xf - -C "$d"; '
        'BLAST_LOCALBOARD_RUN_LOCK=/home/szv0080/lab_quiet.lock python3 "$d/localboard.py" '
        '--archive "$d/revision.tar.gz" --sha '
        + shlex.quote(sha)
        + ' --config localboard.json --calibration "$d/calibration.md"'
    )
    with package.open("rb") as bundle:
        run(config.get("ssh", "ssh"), config["ssh_host"], script, stdin=bundle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", nargs="?", default="HEAD")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "localboard.json",
    )
    parser.add_argument("--archive", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--sha", help=argparse.SUPPRESS)
    parser.add_argument(
        "--calibration", type=Path, help="Override the local calibration ledger"
    )
    args = parser.parse_args()
    confdir = args.config.resolve().parent
    config = json.loads(args.config.read_bytes())
    if args.calibration:
        config["calibration"] = str(args.calibration.resolve())
    with tempfile.TemporaryDirectory(dir=confdir, prefix=".localboard-") as temp:
        work = Path(temp)
        if args.archive:
            archive, sha = args.archive, args.sha
        else:
            git = ["git", "-C", str(Path(__file__).resolve().parent)]
            sha = (
                output(*git, "rev-parse", "--verify", args.revision + "^{commit}")
                .decode()
                .strip()
            )
            archive = work / "revision.tar.gz"
            run(*git, "archive", "--format=tar.gz", "-o", str(archive), sha)
        if "ssh_host" in config:
            relay(config, archive, work, sha, confdir / config["calibration"])
        else:
            evaluate(config, confdir, archive, work, sha)


if __name__ == "__main__":
    main()
