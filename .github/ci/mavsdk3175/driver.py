#!/usr/bin/env python3
"""Draft fork-only validation driver. It never commits, pushes, or dispatches."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

BASE = "6b5940a83da831cc04c1e42286dc12b987bc242d"
PROTO = "df90b7ebba3c9c73d2e43c87885709fd34c7eb25"
REPO = "dhairyajangir/MAVSDK"
BRANCH = "ci/mavsdk3175-profile-validation-20261112"
IMAGE = "docker.io/mavsdk/mavsdk-ubuntu-26.04@sha256:833cc96c63600976475f02a58110c22d8073bf2a1987cf047786ff206795eab5"
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_LOG = 1024 * 1024
MAX_SOURCE = 4 * 1024 * 1024
MANIFEST = ".github/ci/mavsdk3175/manifest.json"
PROBE_FIELDS = {"baseline_sdk", "candidate_sdk", "candidate_install", "deps",
                "candidate_build", "output_json", "scratch"}
ROOT_KEYS = {"schema_version", "frozen", "repository", "baseline_sdk_commit",
             "baseline_proto_commit", "container_image", "patches",
             "candidate_expected_files", "proto_expected_files",
             "baseline_expected_files", "harness_files",
             "regeneration_allowlist", "baseline_harness_cases",
             "baseline_harness_failures", "candidate_harness_cases", "candidate_server_required_cases",
             "extra_python_requirements", "probe_support_files", "probes"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def relative(value: str) -> Path:
    p = Path(value)
    if not value or p.is_absolute() or ".." in p.parts or ".git" in p.parts:
        raise ValueError(f"Unsafe relative path: {value!r}")
    return p


def contained(root: Path, value: str) -> Path:
    path = root / relative(value)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path leaves root: {value!r}")
    return path


def git(root: Path, *args: str) -> bytes:
    # Per-command trust only, never git config --global or credential settings.
    return subprocess.run(["git", "-c", f"safe.directory={root}", "-C", str(root),
                           *args], check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout


def snapshot(root: Path) -> dict[str, object]:
    result = {}
    for repo, prefix in ((root, ""), (root / "proto", "proto/")):
        names = git(repo, "ls-files", "-z", "--cached", "--others",
                    "--exclude-standard").split(b"\0")
        for raw in names:
            if not raw:
                continue
            name = os.fsdecode(raw)
            relative(name)
            p = repo / name
            if p.is_symlink():
                result[prefix + name] = {"symlink": os.readlink(p)}
            elif p.is_file():
                result[prefix + name] = {"sha256": digest(p), "bytes": p.stat().st_size}
    return result


def tree_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def prefix_snapshot(prefix: Path) -> dict[str, object]:
    result = {}
    for p in sorted(prefix.rglob("*")):
        name = str(p.relative_to(prefix))
        if p.is_symlink():
            result[name] = {"symlink": os.readlink(p)}
        elif p.is_file():
            result[name] = {"sha256": digest(p), "bytes": p.stat().st_size}
    return result


def check_files(root: Path, expected: dict[str, str]) -> None:
    if not expected:
        raise ValueError("A required expected-files map is empty")
    for name, wanted in expected.items():
        if not SHA256.fullmatch(wanted):
            raise ValueError(f"Unfrozen file hash: {name}")
        p = contained(root, name)
        if not p.is_file() or digest(p) != wanted:
            raise ValueError(f"Source file hash mismatch: {name}")


class Driver:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.out = args.output.resolve()
        self.work = args.work.resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        self.records = []
        self.failures = []
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith("GTEST_"):
                del self.env[key]
        self.env.update(CI="1", PYTHON_CPU_COUNT="2", CMAKE_BUILD_PARALLEL_LEVEL="2", MAKEFLAGS="-j2",
                        MAVSDK_GENERATE_JOBS="2", CTEST_PARALLEL_LEVEL="2",
                        MAVSDK_REGENERATE_IN_CONTAINER="0",
                        GRADLE_OPTS="-Dorg.gradle.workers.max=2 -Dorg.gradle.parallel=false -Dorg.gradle.daemon=false")
        if hasattr(os, "sched_getaffinity"):
            available = sorted(os.sched_getaffinity(0))
            os.sched_setaffinity(0, available[:2])
            self.affinity = sorted(os.sched_getaffinity(0))
        else:
            raise RuntimeError("Linux CPU-affinity control is required")

    def phase(self, name, function, *, soft=False):
        record = {"phase": name, "state": "running", "started_unix": time.time()}
        self.records.append(record)
        path = self.out / "phases" / f"{len(self.records):02d}-{name}.json"
        save(path, record)
        save(self.out / "progress.json", self.records)
        print(f"START {name}", flush=True)
        try:
            value = function()
            record.update(state="passed", result=value)
        except Exception as error:
            record.update(state="failed", error=f"{type(error).__name__}: {error}")
            self.failures.append(name)
            if not soft:
                raise
        finally:
            record["finished_unix"] = time.time()
            save(path, record)
            save(self.out / "progress.json", self.records)
            print(f"END {name}: {record['state']}", flush=True)
        return record.get("result")

    def run(self, name, command, cwd, *, env=None, accepted=(0,), timeout=3600):
        """Stream/hash all output; retain a bounded beginning and failure tail."""
        log = self.out / "logs" / f"{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        effective = dict(self.env)
        if env:
            effective.update(env)
        info = {"argv": [str(x) for x in command], "cwd": str(cwd),
                "timeout_seconds": timeout, "accepted_exit_codes": list(accepted),
                "started_unix": time.time()}
        command_path = self.out / "commands" / f"{name}.json"
        save(command_path, info)
        process = subprocess.Popen(command, cwd=cwd, env=effective,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        total = 0
        first = bytearray()
        tail = bytearray()
        all_hash = hashlib.sha256()
        timed_out = False
        try:
            while selector.get_map() or process.poll() is None:
                if time.time() - info["started_unix"] > timeout and not timed_out:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                for key, _ in selector.select(0.25):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    all_hash.update(chunk)
                    if len(first) < MAX_LOG * 3 // 4:
                        first.extend(chunk[: MAX_LOG * 3 // 4 - len(first)])
                    tail.extend(chunk)
                    del tail[:max(0, len(tail) - MAX_LOG // 4)]
            code = process.wait()
        finally:
            selector.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if total <= len(first):
            log.write_bytes(first)
        elif total <= MAX_LOG:
            # Tail overlaps first for small outputs; retain exactly once.
            log.write_bytes(first + tail[-(total - len(first)):])
        else:
            log.write_bytes(first + b"\n[bounded log: middle omitted; full-output hash in command receipt]\n" + tail)
        info.update(exit_code=code, timed_out=timed_out, output_bytes=total,
                    output_sha256=all_hash.hexdigest(), retained_log_sha256=digest(log),
                    log_truncated=total > MAX_LOG, finished_unix=time.time())
        save(command_path, info)
        if timed_out or code not in accepted:
            raise RuntimeError(f"{name} failed: exit={code}, timeout={timed_out}; see {log.name}")
        return info

    def validate(self):
        carrier = os.environ.get("CARRIER_SHA", "")
        if not SHA40.fullmatch(carrier) or os.environ.get("GITHUB_REPOSITORY") != REPO:
            raise ValueError("Exact carrier SHA and fork repository are required")
        event = os.environ.get("GITHUB_EVENT_NAME")
        if event == "push":
            if os.environ.get("GITHUB_REF") != "refs/heads/" + BRANCH:
                raise ValueError("Push is not the single approved helper branch")
            if carrier != os.environ.get("GITHUB_SHA"):
                raise ValueError("Push carrier must equal the triggering commit")
        elif event != "workflow_dispatch":
            raise ValueError("Only the approved push or manual dispatch is allowed")
        payload = self.args.payload.resolve()
        baseline = self.args.baseline.resolve()
        if git(payload, "rev-parse", "HEAD").decode().strip() != carrier:
            raise ValueError("Payload checkout does not equal candidate_ref")
        helper_root = Path(__file__).resolve().parents[3]
        if git(helper_root, "rev-parse", "HEAD").decode().strip() != os.environ.get("HELPER_SHA"):
            raise ValueError("Executing helper checkout does not equal workflow commit")
        if git(baseline, "rev-parse", "HEAD").decode().strip() != BASE:
            raise ValueError("Baseline SDK checkout mismatch")
        if git(baseline / "proto", "rev-parse", "HEAD").decode().strip() != PROTO:
            raise ValueError("Baseline proto checkout mismatch")
        if git(baseline, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("Baseline is not initially clean")
        manifest_file = contained(payload, MANIFEST)
        manifest = json.loads(manifest_file.read_text())
        if set(manifest) != ROOT_KEYS or manifest["schema_version"] != 1 or manifest["frozen"] is not True:
            raise ValueError("Manifest is draft, wrong version, or has missing/unknown keys")
        for name, wanted in (("repository", REPO), ("baseline_sdk_commit", BASE),
                             ("baseline_proto_commit", PROTO), ("container_image", IMAGE)):
            if manifest[name] != wanted:
                raise ValueError(f"Manifest contract mismatch: {name}")
        image = json.loads(self.args.image_proof.read_text())
        wanted_digest = IMAGE.split("@", 1)[1]
        if (image["requested_image"] != IMAGE or image["architecture"] != "amd64"
                or image["os"] != "linux"
                or not any(x.endswith("@" + wanted_digest) for x in image["repo_digests"])):
            raise ValueError("Actual Docker image proof does not match the pinned image")
        for patch in manifest["patches"]:
            if set(patch) != {"role", "path", "sha256", "paths"}:
                raise ValueError("Unknown patch fields")
            if patch["role"] not in {"native", "api-tests", "proto", "baseline-tests"}:
                raise ValueError("Unknown patch role")
            self.verified_payload(patch)
            if not patch["paths"] or len(set(patch["paths"])) != len(patch["paths"]):
                raise ValueError("Patch paths must be explicit and unique")
            for name in patch["paths"]:
                relative(name)
                if patch["role"] == "proto" and name != "protos/ftp/ftp.proto":
                    raise ValueError("Proto patch may change only FTP schema")
                if patch["role"] == "baseline-tests" and not name.startswith("cpp/src/system_tests/"):
                    raise ValueError("Baseline overlay must contain only existing-API system-test sources")
                if patch["role"] in {"native", "api-tests"}:
                    if not name.startswith("cpp/src/") or "/templates/" in name or "/generated/" in name:
                        raise ValueError("SDK payload outside native/test scope")
                    if name in {"cpp/src/mavsdk/plugins/ftp/ftp.cpp",
                                "cpp/src/mavsdk/plugins/ftp/include/plugins/ftp/ftp.hpp",
                                "cpp/src/mavsdk_server/src/plugins/ftp/ftp_service_impl.hpp"}:
                        raise ValueError("Preliminary generated C++ is forbidden in payload patches")
        roles = [p["role"] for p in manifest["patches"]]
        if roles.count("native") != 1 or roles.count("proto") != 1 or roles.count("baseline-tests") != 1:
            raise ValueError("Exactly one native, proto and baseline-test patch is required")
        for item in manifest["harness_files"]:
            if set(item) != {"path", "sha256", "destination"}:
                raise ValueError("Unknown harness fields")
            self.verified_payload(item)
            relative(item["destination"])
        for key in ("candidate_expected_files", "proto_expected_files", "baseline_expected_files"):
            if not manifest[key]:
                raise ValueError(f"Missing expected files: {key}")
            for name, sha in manifest[key].items():
                relative(name)
                if not SHA256.fullmatch(sha):
                    raise ValueError(f"Unfrozen {key} hash: {name}")
        for name in ("baseline_harness_cases", "baseline_harness_failures", "candidate_harness_cases"):
            if not manifest[name] or len(manifest[name]) != len(set(manifest[name])):
                raise ValueError(f"Missing/duplicate exact harness cases: {name}")
        if not set(manifest["baseline_harness_failures"]) < set(manifest["baseline_harness_cases"]):
            raise ValueError("Baseline requires both expected runtime failures and controls")
        if not set(manifest["baseline_harness_cases"]) <= set(manifest["candidate_harness_cases"]):
            raise ValueError("Candidate must include the same baseline cases")
        server_cases = manifest["candidate_server_required_cases"]
        if (len(server_cases) != 4 or len(set(server_cases)) != 4
                or any(not name.startswith("FtpServiceImplEnumTest.") for name in server_cases)):
            raise ValueError("The four frozen ordinary server-runner regression cases are required")
        required_probes = {p["name"] for p in manifest["probes"] if p.get("required") is True}
        if not {"frontend-contract", "python-ftp-transport", "grpc-ftp-transport"} <= required_probes:
            raise ValueError("Frozen owner source, Python/UDP and actual gRPC transport probes are required")
        for support in manifest["probe_support_files"]:
            if set(support) != {"path", "sha256"}:
                raise ValueError("Unknown probe support-file fields")
            self.verified_payload(support)
        for probe in manifest["probes"]:
            if set(probe) != {"name", "path", "sha256", "argv", "required"}:
                raise ValueError("Unknown probe fields")
            if not re.fullmatch(r"[a-z0-9-]+", probe["name"]):
                raise ValueError("Unsafe probe name")
            self.verified_payload(probe)
            for argument in probe["argv"]:
                for _, field, spec, conversion in string.Formatter().parse(argument):
                    if field is not None and (field not in PROBE_FIELDS or spec or conversion):
                        raise ValueError("Unknown probe argument placeholder")
        if manifest["extra_python_requirements"] is not None:
            self.verified_payload(manifest["extra_python_requirements"])
        # Carrier is an evidence helper, never silently a production source checkout.
        git(payload, "fetch", "--no-tags", str(baseline), BASE)
        changed = git(payload, "diff", "--name-only", BASE, carrier).decode().splitlines()
        if any(p != ".github/workflows/linux.yml" and
               not p.startswith(".github/ci/mavsdk3175/") for p in changed):
            raise ValueError("Carrier changed files outside the helper workflow/payload directory")
        ignored = ["cpp/build/profile-helper/CMakeCache.txt",
                   "cpp/build-docs/install/docs/index.html", "venv/bin/python3",
                   "c/build/profile-helper/install/lib/libcmavsdk.so",
                   "py/mavsdk/mavsdk/lib/libcmavsdk.so",
                   "py/mavsdk/mavsdk.egg-info/PKG-INFO",
                   "py/mavsdk/mavsdk/__pycache__/__init__.pyc"]
        ignored_proof = {}
        for name in ignored:
            ignored_proof[name] = git(baseline, "check-ignore", "-v", name).decode().strip()
        save(self.out / "runtime-path-ignore-proof.json", ignored_proof)
        description = git(baseline, "describe", "--tags", "--abbrev=8", "--dirty", "--always").decode().strip()
        if not re.match(r"v[0-9]+\.[0-9]+\.[0-9]+", description):
            raise ValueError("Baseline version tags are missing; upstream uses full history/tags")
        self.manifest = manifest
        self.payload = payload
        self.baseline = baseline
        save(self.out / "input-manifest.json", manifest)
        save(self.out / "container-image.json", image)
        return {"carrier_commit": carrier, "baseline_commit": BASE,
                "baseline_proto_commit": PROTO, "git_describe": description, "manifest_sha256": digest(manifest_file),
                "driver_sha256": digest(Path(__file__)), "cpu_affinity": self.affinity,
                "workflow_commit": os.environ.get("HELPER_SHA"),
                "workflow_file_sha256": digest(Path(__file__).parents[2] / "workflows/linux.yml"),
                "carrier_helper_paths": changed}

    def verified_payload(self, item):
        if not SHA256.fullmatch(item["sha256"]):
            raise ValueError(f"Unfrozen payload hash: {item['path']}")
        p = contained(self.args.payload.resolve(), item["path"])
        if not p.is_file() or digest(p) != item["sha256"]:
            raise ValueError(f"Payload SHA mismatch: {item['path']}")
        return p

    def apply(self, root, patch):
        p = self.verified_payload(patch)
        actual = git(root, "apply", "--numstat", str(p)).decode().splitlines()
        names = [row.split("\t", 2)[2] for row in actual]
        if sorted(names) != sorted(patch["paths"]):
            raise ValueError(f"Patch path manifest mismatch: {patch['path']}")
        git(root, "apply", "--check", "--whitespace=error-all", str(p))
        git(root, "apply", "--whitespace=error-all", str(p))
        target = self.out / "input-patches" / Path(patch["path"]).name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)

    def prepare(self):
        self.candidate = self.work / "candidate"
        self.run("clone-candidate", ["git", "clone", "--no-hardlinks",
                 str(self.baseline), str(self.candidate)], self.work, timeout=300)
        git(self.candidate, "checkout", "--detach", BASE)
        self.run("candidate-submodules", ["git", "-C", str(self.candidate),
                 "submodule", "update", "--init", "--recursive"], self.work, timeout=900)
        if git(self.candidate / "proto", "rev-parse", "HEAD").decode().strip() != PROTO:
            raise ValueError("Candidate proto baseline mismatch")
        for p in self.manifest["patches"]:
            if p["role"] != "baseline-tests":
                self.apply(self.candidate / "proto" if p["role"] == "proto" else self.candidate, p)
        check_files(self.candidate, self.manifest["candidate_expected_files"])
        check_files(self.candidate / "proto", self.manifest["proto_expected_files"])
        sdk_paths = {x for p in self.manifest["patches"] if p["role"] in {"native", "api-tests"} for x in p["paths"]}
        if sdk_paths != set(self.manifest["candidate_expected_files"]):
            raise ValueError("Every candidate SDK payload path must have an exact expected file hash")
        self.harness = self.work / "harness"
        for item in self.manifest["harness_files"]:
            destination = contained(self.harness, item["destination"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.verified_payload(item), destination)
        for item in self.manifest["harness_files"]:
            target = contained(self.out / "standalone-harness", item["destination"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.verified_payload(item), target)
        for item in [*self.manifest["probe_support_files"], *self.manifest["probes"]]:
            target = self.out / "owner-probes" / Path(item["path"]).name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise ValueError("Duplicate owner-probe artifact filename")
            shutil.copyfile(self.verified_payload(item), target)
        if not (self.harness / "CMakeLists.txt").is_file():
            raise ValueError("Missing standalone harness CMakeLists.txt")
        self.baseline_build = self.baseline / "cpp/build/profile-helper"
        self.candidate_build = self.candidate / "cpp/build/profile-helper"
        cpp_payloads = sorted(str(self.candidate / name) for name in sdk_paths
                              if name.endswith((".c", ".cpp", ".h", ".hpp")))
        if cpp_payloads:
            self.run("payload-cpp-style-dry-run", ["clang-format-21", "--dry-run",
                     "--Werror", *cpp_payloads], self.candidate, timeout=300)
        description = git(self.candidate, "describe", "--tags", "--abbrev=8", "--dirty", "--always").decode().strip()
        return {"candidate_pre_generation_snapshot": tree_digest(snapshot(self.candidate)),
                "candidate_git_describe": description, "payload_cpp_style_paths": cpp_payloads}

    def configure(self, label, source, build, *, superbuild):
        args = ["cmake", "-S", str(source / "cpp"), "-B", str(build),
                "-DCMAKE_BUILD_TYPE=Debug", "-DBUILD_MAVSDK_SERVER=ON",
                "-DBUILD_TESTING=ON", "-DWERROR=ON",
                "-DCMAKE_INSTALL_PREFIX=" + str(build / "install"),
                "-DSUPERBUILD=" + ("ON" if superbuild else "OFF")]
        if not superbuild:
            args += ["-DCMAKE_PREFIX_PATH=" + str(self.deps)]
        return self.run(label + "-configure", args, source, timeout=3600)

    def generation(self):
        self.configure("baseline", self.baseline, self.baseline_build, superbuild=True)
        self.deps = self.baseline_build / "third_party/install"
        for executable in ("protoc", "grpc_cpp_plugin"):
            if not (self.deps / "bin" / executable).is_file():
                raise ValueError(f"Missing official generator: {executable}")
        self.run("protoc-version", [str(self.deps / "bin/protoc"), "--version"],
                 self.baseline, timeout=30)
        version = (self.out / "logs/protoc-version.log").read_text().strip()
        if version != "libprotoc 29.1":
            raise ValueError(f"Wrong actual protoc version: {version}")
        packages = {}
        for pattern, wanted in (("*protobuf*config-version.cmake", "5.29.1"),
                                ("*gRPCConfigVersion.cmake", "1.68.2")):
            found = list(self.deps.rglob(pattern))
            if len(found) != 1:
                raise ValueError(f"Cannot uniquely identify {pattern}")
            content = found[0].read_text()
            if not re.search(r'set\(PACKAGE_VERSION\s+"?' + re.escape(wanted) + r'(?:\.0)?"?\)', content):
                raise ValueError(f"Unexpected package version in {found[0].name}")
            packages[wanted] = {"path": str(found[0]), "sha256": digest(found[0])}
        self.prefix_before = prefix_snapshot(self.deps)
        save(self.out / "dependency-prefix-manifest.json", self.prefix_before)
        save(self.out / "generator-identity.json", {
            "protoc_version": version, "protoc_sha256": digest(self.deps / "bin/protoc"),
            "grpc_cpp_plugin_sha256": digest(self.deps / "bin/grpc_cpp_plugin"),
            "package_version_files": packages, "prefix_tree_sha256": tree_digest(self.prefix_before)})
        self.run("baseline-full-generation", ["tools/regenerate_all.bash", "--no-container",
                 "--build-dir", str(self.baseline_build)], self.baseline, timeout=1800)
        if git(self.baseline, "status", "--porcelain", "--untracked-files=all"):
            self.export_patch(self.baseline, "baseline-generation")
            raise ValueError("Official full baseline generation is not clean; not behavioral RED")
        self.run("baseline-generation-python-freeze", [str(self.baseline / "venv/bin/python3"),
                 "-m", "pip", "freeze", "--all"], self.baseline, timeout=120)
        self.baseline_generated = snapshot(self.baseline)
        link = self.candidate_build / "third_party/install"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(self.deps, target_is_directory=True)
        before = snapshot(self.candidate)
        self.run("candidate-full-generation-1", ["tools/regenerate_all.bash", "--no-container",
                 "--build-dir", str(self.candidate_build)], self.candidate, timeout=1800)
        after = snapshot(self.candidate)
        changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
        allowed = self.manifest["regeneration_allowlist"] + list(self.manifest["candidate_expected_files"])
        if any(not any(fnmatch.fnmatchcase(p, rule) for rule in allowed) for p in changed):
            raise ValueError("Official generation changed paths outside the frozen attributable allowlist")
        self.run("candidate-full-generation-2", ["tools/regenerate_all.bash", "--no-container",
                 "--build-dir", str(self.candidate_build)], self.candidate, timeout=1800)
        final = snapshot(self.candidate)
        if after != final:
            raise ValueError("Second complete official generation is not idempotent")
        # regenerate_all intentionally tolerates fix_style.py's change exit code.
        # After idempotence, require a direct successful style pass as well.
        self.run("candidate-cpp-style-check", [str(self.candidate / "venv/bin/python3"),
                 "tools/fix_style.py", "--quiet", "--cores", "2", "."], self.candidate / "cpp", timeout=300)
        if snapshot(self.candidate) != final:
            raise ValueError("Explicit final C++ style check changed generated source")
        self.run("candidate-generation-python-freeze", [str(self.candidate / "venv/bin/python3"),
                 "-m", "pip", "freeze", "--all"], self.candidate, timeout=120)
        self.generated_snapshot = final
        save(self.out / "candidate-generated-source-manifest.json", final)
        save(self.out / "generation-changes.json", {"observed_paths": changed,
             "observed_count": len(changed), "candidate_source_sha256": tree_digest(final),
             "second_pass_identical": True, "baseline_clean": True})
        self.export_patch(self.candidate, "candidate-generated-sdk")
        self.export_patch(self.candidate / "proto", "candidate-proto")
        for p in changed:
            source = contained(self.candidate, p)
            if source.is_file() and not source.is_symlink():
                if source.stat().st_size > MAX_SOURCE:
                    raise ValueError(f"Generated file exceeds bounded artifact limit: {p}")
                target = self.out / "changed-generated-source" / relative(p)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        return {"observed_regeneration_paths": changed,
                "idempotent_source_sha256": tree_digest(final)}

    def export_patch(self, root, name):
        # A private temporary index captures untracked new sources; no commits.
        fd, index_name = tempfile.mkstemp(dir=self.work, prefix="evidence-index-")
        os.close(fd)
        os.unlink(index_name)
        env = dict(self.env, GIT_INDEX_FILE=index_name)
        try:
            for args in (["read-tree", "HEAD"], ["add", "-A", "--", "."]):
                subprocess.run(["git", "-c", f"safe.directory={root}", "-C",
                                str(root), *args], env=env, check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            patch = subprocess.run(["git", "-c", f"safe.directory={root}", "-C", str(root),
                                    "diff", "--cached", "--binary", "HEAD", "--", "."],
                                   env=env, check=True, stdout=subprocess.PIPE).stdout
            if len(patch) > 16 * MAX_SOURCE:
                raise ValueError("Source evidence patch is unexpectedly large")
            path = self.out / "source-patches" / (name + ".patch")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(patch)
        finally:
            Path(index_name).unlink(missing_ok=True)

    def binary_identity(self, label, build):
        binaries = {}
        for p in sorted(build.rglob("*")):
            if p.is_file() and (p.name in {"unit_tests_runner", "system_tests_runner",
                   "unit_tests_mavsdk_server", "mavsdk_server", "ftp_result_tests"}
                   or p.name.startswith("libmavsdk.so")):
                binaries[str(p.relative_to(build))] = {"resolved_path": str(p.resolve()),
                    "sha256": digest(p), "bytes": p.stat().st_size}
        save(self.out / (label + "-binary-identity.json"), binaries)
        if not binaries:
            raise ValueError("No expected native binaries were built")
        return binaries

    def gtest(self, label, binary, cwd, *, cases=None, failures=()):
        output = self.out / "junit" / (label + ".xml")
        output.parent.mkdir(parents=True, exist_ok=True)
        binary_hash = digest(binary)
        # Exit1 is collected, never treated as success. Preserve native failure
        # inventory before the phase raises, so later independent gates can run.
        info = self.run(label, [str(binary), "--gtest_output=xml:" + str(output)],
                        cwd, accepted=(0, 1), timeout=900)
        receipt = {"binary": str(binary), "binary_sha256": binary_hash,
                   "exit_code": info["exit_code"], "case_names": [],
                   "not_run_case_names": [], "skipped_case_names": [],
                   "failure_names": [], "baseline_behavioral_red": False,
                   "status": "failed"}
        runtime_path = self.out / (label + "-runtime.json")
        try:
            root = ET.parse(output).getroot()
            found = {}
            not_run = []
            skipped = []
            for case in root.iter("testcase"):
                name = case.attrib.get("classname", "") + "." + case.attrib["name"]
                if name in found:
                    raise ValueError("Duplicate runtime test name")
                found[name] = bool(case.findall("failure") or case.findall("error"))
                if case.attrib.get("status") == "notrun":
                    not_run.append(name)
                if case.findall("skipped"):
                    skipped.append(name)
            actual_failures = sorted(k for k, v in found.items() if v)
            receipt.update(tests=len(found), failure_names=actual_failures,
                           case_names=sorted(found), not_run_case_names=sorted(not_run),
                           skipped_case_names=sorted(skipped),
                           executed=len(found) - len(set(not_run)),
                           passed=len(found) - len(set(not_run) | set(skipped) | set(actual_failures)),
                           xml_sha256=digest(output))
            issues = []
            if not found or (cases is not None and set(found) != set(cases)):
                issues.append("Runtime inventory differs from the frozen harness inventory")
            if cases is not None and (not_run or skipped):
                issues.append("Focused harness cases did not all execute without skips")
            if actual_failures != sorted(failures):
                issues.append("Unexpected runtime failure identities")
            wanted_exit = 1 if failures else 0
            if info["exit_code"] != wanted_exit:
                issues.append("Runtime exit does not match expected success/assertion failures")
            receipt.update(status="failed" if issues else "passed", gate_issues=issues,
                           baseline_behavioral_red=bool(failures) and not issues)
            save(runtime_path, receipt)
            if issues:
                raise RuntimeError("; ".join(issues) + "; runtime receipt saved")
        except Exception as error:
            receipt["error"] = f"{type(error).__name__}: {error}"
            save(runtime_path, receipt)
            raise
        return receipt

    def full_native(self, label, source, build):
        if label == "candidate":
            self.configure(label, source, build, superbuild=False)
        self.run(label + "-build", ["cmake", "--build", str(build), "--parallel", "2"],
                 source, timeout=2400)
        identities = self.binary_identity(label, build)
        for suffix, path in (
            ("unit", "src/unit_tests/unit_tests_runner"),
            ("system", "src/system_tests/system_tests_runner"),
            ("server", "src/mavsdk_server/test/unit_tests_mavsdk_server")):
            self.phase(label + "-" + suffix + "-tests",
                       lambda s=suffix, p=path: self.gtest(label + "-" + s, build / p, source / "cpp"),
                       soft=True)
        self.run(label + "-install", ["cmake", "--build", str(build), "--target",
                 "install", "--parallel", "2"], source, timeout=600)
        return identities

    def required_cases(self, label, required):
        runtime_path = self.out / (label + "-runtime.json")
        runtime = json.loads(runtime_path.read_text()) if runtime_path.is_file() else {}
        passed = (set(runtime.get("case_names", ()))
                  - set(runtime.get("not_run_case_names", ()))
                  - set(runtime.get("skipped_case_names", ()))
                  - set(runtime.get("failure_names", ())))
        missing_or_failed = sorted(set(required) - passed)
        receipt = {"required_case_names": sorted(required),
                   "observed_passed_required": sorted(set(required) & passed),
                   "missing_not_run_skipped_or_failed": missing_or_failed,
                   "runtime_receipt_present": runtime_path.is_file()}
        save(self.out / (label + "-required-cases.json"), receipt)
        if missing_or_failed:
            raise RuntimeError("Required default-runner regressions did not all execute and pass")
        return receipt

    def focused_harness(self, label, source, profile):
        build = self.work / ("harness-" + label)
        command = ["cmake", "-S", str(self.harness), "-B", str(build),
                   "-DMAVSDK_SOURCE=" + str(source),
                   "-DTEST_SOURCE=" + str(source / "cpp/src/system_tests"),
                   "-DPROFILE_TESTS=" + ("ON" if profile else "OFF"),
                   "-DCMAKE_PREFIX_PATH=" + str(self.deps),
                   "-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_CXX_FLAGS_DEBUG=-g0",
                   "-DENABLE_CPPTRACE=OFF", "-DCCACHE=OFF"]
        self.run(label + "-harness-configure", command, self.work, timeout=300)
        self.run(label + "-harness-build", ["cmake", "--build", str(build),
                 "--target", "ftp_result_tests", "--parallel", "1"], self.work, timeout=900)
        self.binary_identity(label + "-harness", build)
        return self.gtest(label + "-harness", build / "ftp_result_tests", source / "cpp",
                          cases=self.manifest[label + "_harness_cases"],
                          failures=() if profile else self.manifest["baseline_harness_failures"])

    def python_bindings(self):
        source = self.candidate
        c_build = source / "c/build/profile-helper"
        install = c_build / "install"
        self.run("c-bindings-configure", ["cmake", "-S", str(source / "c"), "-B", str(c_build),
                 "-DBUILD_SHARED_LIBS=ON", "-DCMAKE_INSTALL_PREFIX=" + str(install),
                 "-DCMAKE_PREFIX_PATH=" + str(self.candidate_build / "install") + ";" + str(self.deps)],
                 source, timeout=300)
        self.run("c-bindings-install", ["cmake", "--build", str(c_build),
                 "--target", "install", "--parallel", "2"], source, timeout=600)
        lib_dir = source / "py/mavsdk/mavsdk/lib"
        lib_dir.mkdir(parents=True, exist_ok=True)
        libs = list((install / "lib").glob("libcmavsdk.so*"))
        if not libs:
            raise ValueError("C wrapper install has no libcmavsdk.so")
        identity = {}
        for p in libs:
            destination = lib_dir / p.name
            if p.is_symlink():
                destination.symlink_to(os.readlink(p))
            else:
                shutil.copyfile(p, destination)
            identity[p.name] = {"path": str(destination), "sha256": digest(destination.resolve())}
        save(self.out / "python-staged-native.json", identity)
        venv = self.work / "python-runtime"
        self.run("python-runtime-venv", ["python3", "-m", "venv", str(venv)], self.work, timeout=120)
        self.python = venv / "bin/python"
        self.run("python-runtime-install", [str(self.python), "-m", "pip", "install",
                 "-r", str(source / "py/requirements-dev.txt"), "-e", str(source / "py/mavsdk")],
                 source, timeout=600)
        self.run("python-default-runtime-freeze", [str(self.python), "-m", "pip", "freeze", "--all"],
                 source, timeout=120)
        self.runtime_env = {"LD_LIBRARY_PATH": str(self.candidate_build / "install/lib")}
        self.run("python-default-tests", [str(self.python), "-m", "pytest", "-v",
                 "--junitxml=" + str(self.out / "junit/python-default.xml")],
                 source / "py/test", env=self.runtime_env, timeout=300)
        extra = self.manifest["extra_python_requirements"]
        if extra:
            frozen = self.verified_payload(extra)
            shutil.copyfile(frozen, self.out / "probe-requirements.txt")
            self.run("python-probe-install", [str(self.python), "-m", "pip", "install",
                     "-r", str(frozen)], source, timeout=600)
        self.run("python-probe-runtime-freeze", [str(self.python), "-m", "pip", "freeze", "--all"],
                 source, timeout=120)
        return identity

    def probes(self):
        results = []
        for probe in self.manifest["probes"]:
            report = self.out / "probes" / (probe["name"] + ".json")
            scratch = self.work / ("probe-" + probe["name"])
            scratch.mkdir(parents=True, exist_ok=True)
            values = {"baseline_sdk": str(self.baseline), "candidate_sdk": str(self.candidate),
                      "candidate_install": str(self.candidate_build / "install"),
                      "candidate_build": str(self.candidate_build), "deps": str(self.deps),
                      "output_json": str(report), "scratch": str(scratch)}
            report.parent.mkdir(parents=True, exist_ok=True)
            script = self.verified_payload(probe)
            result = self.phase("probe-" + probe["name"], lambda p=probe, s=script: self.run(
                p["name"], [str(self.python), str(s), *(x.format_map(values) for x in p["argv"])],
                scratch, env=self.runtime_env, timeout=300), soft=not probe["required"])
            results.append({"name": probe["name"], "command": result,
                            "report_sha256": digest(report) if report.is_file() else None})
            if probe["required"] and not report.is_file():
                raise ValueError("Required probe did not produce its bound JSON receipt")
        return results

    def execute(self):
        self.phase("validate-inputs", self.validate)
        versions = {}
        for name, argv in (("cmake", ["cmake", "--version"]),
                           ("compiler", ["c++", "--version"]),
                           ("python", ["python3", "--version"]),
                           ("clang-format", ["clang-format-21", "--version"]),
                           ("java", ["java", "-version"]), ("doxygen", ["doxygen", "--version"])):
            self.phase("version-" + name, lambda n=name, a=argv: self.run("version-" + n, a, self.work, timeout=30))
            binary = Path(shutil.which(argv[0])).resolve()
            versions[name] = {"path": str(binary), "sha256": digest(binary)}
        save(self.out / "tool-binary-identities.json", versions)
        self.phase("python-worker-budget", lambda: self.run("python-worker-budget",
            ["python3", "-c", "import multiprocessing; n=multiprocessing.cpu_count(); print(n); assert n <= 2"],
            self.work, timeout=30))
        self.phase("prepare-views", self.prepare)
        self.phase("official-full-generation-and-idempotence", self.generation)
        self.phase("baseline-default-native", lambda: self.full_native(
            "baseline", self.baseline, self.baseline_build))
        self.phase("candidate-default-native", lambda: self.full_native(
            "candidate", self.candidate, self.candidate_build))
        self.phase("candidate-system-required-cases", lambda: self.required_cases(
            "candidate-system", self.manifest["candidate_harness_cases"]), soft=True)
        self.phase("candidate-server-required-cases", lambda: self.required_cases(
            "candidate-server", self.manifest["candidate_server_required_cases"]), soft=True)
        for p in self.manifest["patches"]:
            if p["role"] == "baseline-tests":
                self.apply(self.baseline, p)
        check_files(self.baseline, self.manifest["baseline_expected_files"])
        self.phase("baseline-standalone-runtime", lambda: self.focused_harness("baseline", self.baseline, False))
        self.phase("candidate-standalone-runtime", lambda: self.focused_harness("candidate", self.candidate, True))
        self.phase("candidate-c-and-python-default-tests", self.python_bindings)
        self.phase("owner-frontend-and-transport-probes", self.probes)
        if snapshot(self.candidate) != self.generated_snapshot:
            raise ValueError("Candidate source changed after generation/build/runtime gates")
        prefix_after = prefix_snapshot(self.deps)
        save(self.out / "dependency-prefix-final.json", prefix_after)
        if prefix_after != self.prefix_before:
            raise ValueError("Shared third-party install changed during reuse")
        self.export_patch(self.baseline, "baseline-test-overlay")
        return {"candidate_source_sha256": tree_digest(self.generated_snapshot),
                "dependency_prefix_unchanged": True, "failed_phases": self.failures}


def main():
    parser = argparse.ArgumentParser()
    for name in ("baseline", "payload", "output", "work", "image-proof"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    driver = None
    result = None
    error = None
    try:
        driver = Driver(args)
        result = driver.execute()
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(error, file=sys.stderr, flush=True)
    finally:
        args.output.mkdir(parents=True, exist_ok=True)
        save(args.output / "final-summary.json", {
            "status": "failed" if error or (driver and driver.failures) else "passed",
            "error": error, "result": result,
            "phase_receipts": driver.records if driver else [],
            "baseline_compile_failure_is_behavioral_red": False,
            "scope": "One exact helper carrier, clean pinned baseline, explicit patched/generated candidate; no hardware or platform-matrix claim."})
        manifest = {}
        for path in sorted(args.output.rglob("*")):
            if path.is_file() and path.name != "artifact-sha256.json":
                if ".git" in path.parts:
                    raise RuntimeError("Forbidden Git metadata in artifact directory")
                manifest[str(path.relative_to(args.output))] = {"sha256": digest(path),
                                                                "bytes": path.stat().st_size}
        save(args.output / "artifact-sha256.json", manifest)
    return 1 if error or (driver and driver.failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
