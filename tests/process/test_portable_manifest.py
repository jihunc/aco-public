"""Reject cached test images after executable source, configuration or lock drift."""

import json
import runpy
from pathlib import Path

import pytest

api = runpy.run_path("scripts/portable_manifest.py")


def fixture_tree(root: Path) -> None:
    for name in api["ROOT_FILES"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic setup input\n")
    for name in api["ROOT_DIRS"]:
        (root / name).mkdir(exist_ok=True)
    (root / "src/aco").mkdir()
    (root / "src/aco/app.py").write_text("VERSION = 1\n")


@pytest.mark.parametrize("changed", ["src/aco/app.py", "uv.lock", "pnpm-lock.yaml", "compose.product.yaml",
     "tests/fixtures/verification/traceability.md"])
def test_portable_manifest_rejects_source_and_lock_drift(tmp_path: Path, changed: str) -> None:
    fixture_tree(tmp_path)
    (tmp_path / "compose.product.yaml").write_text("synthetic product configuration\n")
    embedded = api["build_manifest"](tmp_path)
    api["verify_manifest"](tmp_path, embedded)
    (tmp_path / changed).write_text("changed relevant input\n")
    with pytest.raises(ValueError, match="explicit rebuild"):
        api["verify_manifest"](tmp_path, embedded)


def test_portable_manifest_rejects_absent_or_corrupt_embedded_metadata(tmp_path: Path) -> None:
    fixture_tree(tmp_path)
    for embedded in ({}, {"schema_version": 1, "files": {"uv.lock": "invented"}, "digest": "invented"}):
        with pytest.raises(ValueError, match="explicit rebuild"):
            api["verify_manifest"](tmp_path, embedded)


def test_manifest_covers_real_executable_and_dependency_inputs() -> None:
    manifest = api["build_manifest"](Path.cwd())
    for required in (
        "src/aco/api/app.py",
        "tests/fixtures/verification/traceability.md",
        "alembic.ini",
        "alembic/versions/r0001_identity.py",
        "alembic/versions/r0002_identity_provenance.py",
        "alembic/versions/r0002a_identity_cost.py",
        "tests/integration/test_portable_egress.py",
        "infra/testing/lock-network.sh",
        "scripts/test-e2e",
        "scripts/test-portable",
        "uv.lock",
        "pnpm-lock.yaml",
        "compose.product.yaml",
        "pyproject.toml",
        "package.json",
        ".dockerignore",
        ".github/workflows/ci.yml",
        "web/next-env.d.ts",
    ):
        assert required in manifest["files"]
    assert json.loads(json.dumps(manifest)) == manifest


def test_only_generated_next_route_bridge_churn_is_normalized(tmp_path: Path) -> None:
    fixture_tree(tmp_path)
    bridge = tmp_path / "web/next-env.d.ts"
    bridge.write_text('import "./.next/types/routes.d.ts";\nimport "./.next/types/root-params.d.ts";\n')
    snapshot = api["build_manifest"](tmp_path)
    bridge.write_text(
        'import "./.next/dev/types/routes.d.ts";\nimport "./.next/dev/types/root-params.d.ts";\n'
    )
    api["verify_manifest"](tmp_path, snapshot)
    bridge.write_text('import "./unexpected-source";\n')
    with pytest.raises(ValueError, match="explicit rebuild"):
        api["verify_manifest"](tmp_path, snapshot)


def test_source_in_cache_named_directory_is_not_silently_excluded(tmp_path: Path) -> None:
    fixture_tree(tmp_path)
    hidden_source = tmp_path / "src/aco/__pycache__/unexpected.py"
    hidden_source.parent.mkdir()
    hidden_source.write_text("RULE = 1\n")
    original = api["build_manifest"](tmp_path)
    hidden_source.write_text("RULE = 2\n")
    with pytest.raises(ValueError, match="explicit rebuild"):
        api["verify_manifest"](tmp_path, original)


@pytest.mark.parametrize("kind", ["file", "directory", "missing", "root-directory"])
def test_source_symlinks_fail_closed(tmp_path: Path, kind: str) -> None:
    fixture_tree(tmp_path)
    original = api["build_manifest"](tmp_path)
    target = tmp_path / "untracked-source"
    if kind == "root-directory":
        (tmp_path / "src").rename(target)
        (tmp_path / "src").symlink_to(target, target_is_directory=True)
    elif kind == "file":
        target.write_text("RULE = 1\n")
    elif kind == "directory":
        target.mkdir()
        (target / "rules.py").write_text("RULE = 1\n")
    if kind != "root-directory":
        (tmp_path / "src/aco/source-link").symlink_to(target, target_is_directory=kind == "directory")
    with pytest.raises(ValueError, match="symlinks.*explicit rebuild"):
        api["verify_manifest"](tmp_path, original)


def test_fixed_milestone_collects_all_offline_backend_groups_and_reset_browser() -> None:
    import shlex

    lines = Path("infra/testing/run-tests.sh").read_text().splitlines()
    backend = [shlex.split(line) for line in lines if line.startswith(".venv/bin/python -m pytest ")]
    assert len(backend) == 1
    required = {
        f"tests/{name}"
        for name in (
            "unit",
            "component",
            "integration",
            "contracts",
            "business_impact",
            "process",
            "evaluations",
        )
    }
    assert required <= set(backend[0]), "Fixed milestone silently omits new owned offline tests"
    assert any(token.startswith("--boundary-report=") for token in backend[0])
    agents = next(line for line in lines if line.startswith("ACO_TEST_PROFILE=synthetic-agents "))
    assert "tests/e2e/demo-reset.spec.ts" in shlex.split(agents)


# Execute the maintained shell with only its absolute checkout root relocated. Every
# fixed command launches a real trusted local process stub, never app/network code.
# Production receives no arbitrary-command/root/skip environment seam.
PREREQUISITES = [
    "ruff", "mypy", "planning", "api_types", "root_types", "web_types", "eslint", "web_build", "migration"
]
INDEPENDENT_GATES = [
    "browser_mock", "browser_synthetic-reminders", "browser_synthetic-agents",
    "browser_synthetic-agent-email", "browser_synthetic-voice", "browser_voice-live-code",
    "browser_synthetic-inbound", "backend", "boundary", "node_component", "node_web",
]


def run_fixed_runner(
    tmp_path: Path, failures: dict[str, int]
) -> tuple[int, str, list[dict[str, object]]]:
    import os
    import shlex
    import subprocess
    import sys

    root = tmp_path / "isolated-runner"
    root.mkdir()
    stub = root / "trusted_stub.py"
    stub.write_text(
        "import json, os, pathlib, sys\n"
        "exe, *args = sys.argv[1:]\n"
        "name = pathlib.Path(exe).name\n"
        "profile = os.environ.get('ACO_TEST_PROFILE', '')\n"
        "if name in ('ruff', 'mypy', 'eslint'): stage = name\n"
        "elif name == 'tsc': stage = 'web_types' if '--project' in args else 'root_types'\n"
        "elif name == 'playwright': stage = 'browser_' + (profile or 'mock')\n"
        "elif name == 'python':\n"
        "    if args[:2] == ['-m', 'pytest']: stage = 'backend'\n"
        "    elif args[0] == 'scripts/migrate.py': stage = 'migration'\n"
        "    elif '--planning' in args: stage = 'planning'\n"
        "    else: stage = 'boundary'\n"
        "elif name == 'node':\n"
        "    if args[0] == '--test': stage = "
        "'node_component' if args[1].startswith('tests/component/') else 'node_web'\n"
        "    elif args[0] == 'web/scripts/generate-api.mjs': stage = 'api_types'\n"
        "    else: stage = 'web_build'\n"
        "else: raise AssertionError('Unexpected executable')\n"
        "with open(os.environ['TRACE_PATH'], 'a') as out:\n"
        "    out.write(json.dumps({'stage': stage, 'args': args, 'profile': profile}) + '\\n')\n"
        "sys.exit(json.loads(os.environ['FAIL_STAGES']).get(stage, 0))\n"
    )
    wrapper = (
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -I -S "
        + shlex.quote(str(stub)) + ' "$0" "$@"\n'
    )
    for relative in (
        ".venv/bin/ruff", ".venv/bin/mypy", ".venv/bin/python", "bin/node",
        "node_modules/.bin/tsc", "node_modules/.bin/playwright", "web/node_modules/.bin/eslint",
    ):
        executable = root / relative
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text(wrapper)
        executable.chmod(0o700)
    # Preserve actual glob expansion as well as each fixed Node argument.
    (root / "web/tests").mkdir(parents=True)
    (root / "web/tests/synthetic.test.mjs").write_text("// trusted synthetic stub target\n")
    source = Path("infra/testing/run-tests.sh").read_text()
    assert source.count("\ncd /aco\n") == 1
    runner = root / "run-tests.sh"
    runner.write_text(source.replace("\ncd /aco\n", "\ncd " + shlex.quote(str(root)) + "\n"))
    trace = root / "trace.jsonl"
    result = subprocess.run(
        ["/bin/sh", str(runner)],
        cwd=root,
        env={
            "PATH": str(root / "bin") + os.pathsep + "/usr/bin:/bin",
            "TRACE_PATH": str(trace), "FAIL_STAGES": json.dumps(failures),
        },
        capture_output=True, text=True, timeout=20,
    )
    assert result.stderr == "", result.stderr
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    return result.returncode, result.stdout, calls


@pytest.mark.parametrize(
    "backend_exit,report_exit,expected",
    [(37, 0, 37), (37, 13, 37), (0, 13, 13), (0, 0, 0)],
)
def test_failed_backend_still_emits_report_and_preserves_exit_status(
    tmp_path: Path, backend_exit: int, report_exit: int, expected: int
) -> None:
    code, output, calls = run_fixed_runner(tmp_path, {"backend": backend_exit, "boundary": report_exit})
    assert [c["stage"] for c in calls] == PREREQUISITES + INDEPENDENT_GATES
    assert code == expected
    assert f"ACO_GATE backend={backend_exit}" in output
    assert f"ACO_GATE boundary={report_exit}" in output
    backend = next(c for c in calls if c["stage"] == "backend")
    assert backend["args"] == [
        "-m", "pytest", "tests/component", "tests/process", "tests/unit", "tests/contracts",
        "tests/integration", "tests/business_impact", "tests/evaluations", "-q",
        "--boundary-report=/tmp/aco-boundary-execution.json",
    ]
    report = next(c for c in calls if c["stage"] == "boundary")
    assert report["args"] == [
        "scripts/check_spec.py", "--mode", "planned", "--boundary-report", "/tmp/aco-boundary-execution.json"
    ]


@pytest.mark.parametrize("failure", INDEPENDENT_GATES)
def test_fixed_runner_collects_every_independent_gate_after_failure(tmp_path: Path, failure: str) -> None:
    code, output, calls = run_fixed_runner(tmp_path, {failure: 41})
    assert code == 41
    assert [c["stage"] for c in calls] == PREREQUISITES + INDEPENDENT_GATES
    for stage in INDEPENDENT_GATES:
        assert f"ACO_GATE {stage}={41 if stage == failure else 0}" in output
    assert next(c for c in calls if c["stage"] == "browser_voice-live-code")["profile"] == "voice-live-code"
    assert next(c for c in calls if c["stage"] == "node_web")["args"] == [
        "--test", "web/tests/synthetic.test.mjs", "tests/voice_live_web_fixture.test.mjs"
    ]


def test_fixed_runner_preserves_first_failure_after_later_failures(tmp_path: Path) -> None:
    code, output, calls = run_fixed_runner(
        tmp_path, {"browser_mock": 42, "browser_synthetic-voice": 43, "backend": 44, "node_web": 45}
    )
    assert code == 42
    assert [c["stage"] for c in calls] == PREREQUISITES + INDEPENDENT_GATES
    for stage, status in (
        ("browser_mock", 42), ("browser_synthetic-voice", 43), ("backend", 44), ("node_web", 45)
    ):
        assert f"ACO_GATE {stage}={status}" in output


@pytest.mark.parametrize("failure", PREREQUISITES)
def test_fixed_runner_prerequisites_remain_fail_closed(tmp_path: Path, failure: str) -> None:
    code, output, calls = run_fixed_runner(tmp_path, {failure: 47})
    assert code == 47
    assert [c["stage"] for c in calls] == PREREQUISITES[: PREREQUISITES.index(failure) + 1]
    assert "ACO_GATE" not in output


def test_fixed_voice_live_code_browser_is_registered_as_offline_profile() -> None:
    import shlex

    commands = [
        shlex.split(line)
        for line in Path("infra/testing/run-tests.sh").read_text().splitlines()
        if line.startswith("ACO_TEST_PROFILE=voice-live-code ")
    ]
    assert commands == [
        [
            "ACO_TEST_PROFILE=voice-live-code",
            "node_modules/.bin/playwright",
            "test",
            "tests/e2e/voice-live-code.spec.ts",
        ]
    ], "The fixed milestone must include the contained Voice browser proof exactly once"


def test_fixed_voice_web_fixture_component_is_registered() -> None:
    import shlex

    commands = [
        shlex.split(line)
        for line in Path("infra/testing/run-tests.sh").read_text().splitlines()
        if line.startswith("node --test ")
    ]
    assert sum(command.count("tests/voice_live_web_fixture.test.mjs") for command in commands) == 1
    assert ["node", "--test", "web/tests/*.test.mjs", "tests/voice_live_web_fixture.test.mjs"] in commands


def test_host_manifest_checker_needs_only_standard_library() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", "import runpy; runpy.run_path('scripts/portable_manifest.py')"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("change", ["mutation", "omission"])
def test_readme_spec_input_invalidates_image_and_boundary_evidence(tmp_path: Path, change: str) -> None:
    from aco.core.evidence import verification_source_digest

    fixture_tree(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("# Public delivery instructions\n")
    snapshot = api["build_manifest"](tmp_path)
    before = verification_source_digest(tmp_path)
    if change == "mutation":
        readme.write_text("# Changed public delivery instructions\n")
    else:
        readme.unlink()
    assert verification_source_digest(tmp_path) != before
    with pytest.raises(ValueError, match="explicit rebuild"):
        api["verify_manifest"](tmp_path, snapshot)


@pytest.mark.parametrize("change", ["mutation", "omission"])
def test_full_regression_workflow_binds_boundary_evidence(tmp_path: Path, change: str) -> None:
    from aco.core.evidence import verification_source_digest

    fixture_tree(tmp_path)
    workflow = tmp_path / ".github/workflows/full-regression.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text("name: Full Regression\non: workflow_dispatch\n")
    before = verification_source_digest(tmp_path)
    if change == "mutation":
        workflow.write_text("name: Changed Full Regression\non: workflow_dispatch\n")
    else:
        workflow.unlink()
    assert verification_source_digest(tmp_path) != before
