"""Manual ratings: append-only store and the `run.py rate` command."""
import argparse
import json

from common.config import Settings
from history.store import AnswerHistory


def test_latest_rating_wins_and_files_are_never_truncated(tmp_path):
    h = AnswerHistory(tmp_path)
    ids = [h.add_answer({"question": f"q{i}", "answer": "a", "model": "m", "prompt": "strict"}) for i in range(150)]
    h.rate(ids[0], 2, ["unsupported"])
    h.rate(ids[0], 4, [])
    assert len(h.answers()) == 150
    assert h.ratings()[ids[0]]["score"] == 4
    g = h.summary()["groups"][0]
    assert g["answers"] == 150 and g["rated"] == 1 and g["flags"]["unsupported"] == 0.0


def test_rate_command_imports_a_run_once_and_continues(tmp_path, monkeypatch):
    import cli

    data = tmp_path / "data"
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings.model_construct(data_dir=data)))
    run = tmp_path / "generation_en_x.json"
    rows = [{"id": "s02", "lang": "en", "question": "How?", "answer": f"ans {i}", "refused": False, "model": "m"} for i in range(1)]
    rows.append({"id": "n01", "lang": "en", "question": "Payroll?", "answer": "I don't have enough information", "refused": True, "model": "m"})
    run.write_text(json.dumps({"config": {"generation": {"prompt": "strict"}}, "rows": rows}))

    answers = iter(["4 incomplete # no menu path", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    cli.cmd_rate(argparse.Namespace(run=str(run), rater="dmitrii"))
    h = AnswerHistory(data / "history")
    assert len(h.answers()) == 2 and len(h.ratings()) == 1
    r = next(iter(h.ratings().values()))
    assert r["score"] == 4 and r["flags"] == ["incomplete"] and r["comment"] == "no menu path" and r["rater"] == "dmitrii"

    answers = iter(["5"])
    cli.cmd_rate(argparse.Namespace(run=str(run), rater=""))  # second run: only the unrated answer is asked
    assert len(h.answers()) == 2 and len(h.ratings()) == 2
