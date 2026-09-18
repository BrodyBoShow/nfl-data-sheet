from pipeline.orchestration.dispatcher import _ANALYSTS, _COLLECTORS
from pipeline.run import _JOBS, _parse_args, main


def test_parse_args_seasons_and_datasets():
    argv = ["nflverse_bulk", "--force", "--seasons", "2018-2025", "--datasets", "team_week"]
    assert _parse_args(argv) == ("nflverse_bulk", True, None, None, "2018-2025", "team_week")


def test_parse_args_rejects_unknown_job():
    assert _parse_args(["not_a_job"]) is None


def test_parse_args_rejects_no_job():
    assert _parse_args(["--force"]) is None


def test_datasets_rejected_for_non_nflverse_bulk_job(capsys):
    assert main(["id_spine", "--datasets", "team_week"]) == 1
    assert "--datasets is only valid for the nflverse_bulk job" in capsys.readouterr().err


def test_seasons_rejected_for_job_that_isnt_nflverse_bulk_or_id_spine(capsys):
    assert main(["efficiency", "--seasons", "2018-2025"]) == 1
    assert "--seasons is only valid for the nflverse_bulk/id_spine jobs" in capsys.readouterr().err


def test_all_dispatcher_jobs_are_registered_in_run_py():
    """Guards against pipeline.run's manual-CLI job list drifting out of sync with the
    dispatcher's automatically-run _COLLECTORS/_ANALYSTS -- every job the dispatcher runs
    on its tick must also be reachable by name through `uv run python -m pipeline.run`
    for local debugging/backfills, or the CLI's own usage message silently stops
    mentioning it."""
    for job in (*_COLLECTORS, *_ANALYSTS):
        assert job.name in _JOBS, (
            f"{job.name!r} runs in the dispatcher but isn't registered in pipeline.run._JOBS"
        )
        assert _JOBS[job.name].name == job.name
