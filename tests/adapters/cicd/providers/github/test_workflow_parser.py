from checkcord.adapters.cicd.providers.github.workflow_parser import parse_workflow_yaml


def test_parse_workflow_yaml_simple() -> None:
    raw = b"""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Run tests
        run: pytest
"""
    definition = parse_workflow_yaml(".github/workflows/ci.yml", raw)
    assert definition is not None
    assert definition.workflow_path == ".github/workflows/ci.yml"
    assert definition.name == "CI"
    assert len(definition.jobs) == 1
    job = definition.jobs[0]
    assert job.job_id == "test"
    assert job.needs == []
    assert len(job.steps) == 2
    assert job.steps[0].uses == "actions/checkout@v4"
    assert job.steps[1].run == "pytest"


def test_parse_workflow_yaml_needs_dependency_chain() -> None:
    raw = b"""
name: Build and Deploy
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: make build
  test:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: make test
  deploy:
    needs: [build, test]
    runs-on: ubuntu-latest
    steps:
      - run: make deploy
"""
    definition = parse_workflow_yaml(".github/workflows/deploy.yml", raw)
    assert definition is not None
    jobs_by_id = {j.job_id: j for j in definition.jobs}
    assert jobs_by_id["test"].needs == ["build"]
    assert jobs_by_id["deploy"].needs == ["build", "test"]
    assert jobs_by_id["build"].needs == []


def test_parse_workflow_yaml_matrix_build_job_names() -> None:
    raw = b"""
name: Matrix
jobs:
  test:
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    runs-on: ubuntu-latest
    steps:
      - run: pytest
"""
    definition = parse_workflow_yaml(".github/workflows/matrix.yml", raw)
    assert definition is not None
    assert len(definition.jobs) == 1
    assert definition.jobs[0].job_id == "test"


def test_parse_workflow_yaml_reusable_workflow_call() -> None:
    raw = b"""
name: Reusable Caller
jobs:
  call-shared:
    uses: org/repo/.github/workflows/shared.yml@main
"""
    definition = parse_workflow_yaml(".github/workflows/caller.yml", raw)
    assert definition is not None
    assert len(definition.jobs) == 1
    # A reusable workflow call job has no `steps:` key -- must not error.
    assert definition.jobs[0].steps == []


def test_parse_workflow_yaml_invalid_yaml_returns_none() -> None:
    raw = b"not: valid: yaml: [unterminated"
    definition = parse_workflow_yaml(".github/workflows/broken.yml", raw)
    assert definition is None


def test_parse_workflow_yaml_unexpected_top_level_shape_returns_none() -> None:
    raw = b"- just\n- a\n- list\n"
    definition = parse_workflow_yaml(".github/workflows/broken.yml", raw)
    assert definition is None


def test_parse_workflow_yaml_no_jobs_key() -> None:
    raw = b"name: Empty\n"
    definition = parse_workflow_yaml(".github/workflows/empty.yml", raw)
    assert definition is not None
    assert definition.jobs == []


def test_parse_workflow_yaml_single_needs_string_normalized_to_list() -> None:
    raw = b"""
jobs:
  a:
    steps: []
  b:
    needs: a
    steps: []
"""
    definition = parse_workflow_yaml(".github/workflows/single_need.yml", raw)
    assert definition is not None
    jobs_by_id = {j.job_id: j for j in definition.jobs}
    assert jobs_by_id["b"].needs == ["a"]
