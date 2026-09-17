from pipeline.run import _parse_args, main


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
