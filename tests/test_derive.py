import pathlib

import pandas as pd
import pytest

from traffic_service import derive

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "data engineering"


@pytest.fixture(scope="module")
def shipped():
    """The shipped v2 chain. It is the only specification of the v2 math that exists, so the
    test compares against it directly rather than against numbers copied into this file."""
    return {name: pd.read_csv(CORPUS / f"traffic_{name}.csv")
            for name in ("clean", "rows", "daily", "areas", "cities")}


def test_the_row_math_reproduces_the_shipped_rows(shipped):
    got = derive.rows_from_clean(shipped["clean"], shipped["areas"])
    want = shipped["rows"]
    assert len(got) == len(want) == 21450
    for col in ("observed_m", "gridlock_eq", "ci_raw", "freeflow", "ci"):
        assert got[col].to_numpy() == pytest.approx(want[col].to_numpy(), rel=1e-9, abs=1e-9), col
    assert list(got.columns) == list(want.columns)


def test_the_daily_aggregation_reproduces_the_shipped_daily(shipped):
    got = derive.daily_from_rows(shipped["rows"], shipped["areas"])
    want = shipped["daily"]
    assert len(got) == len(want) == 648
    assert got.ci.to_numpy() == pytest.approx(want.ci.to_numpy(), rel=1e-9, abs=1e-9)


def test_the_area_aggregation_reproduces_the_shipped_areas(shipped):
    got = derive.areas_from_rows(shipped["rows"], shipped["areas"])
    want = shipped["areas"]
    assert len(got) == len(want) == 89
    assert got.ci.to_numpy() == pytest.approx(want.ci.to_numpy(), rel=1e-9, abs=1e-9)
    # `.all()`: a bare `assert arr == arr` raises ValueError on multi-element numpy arrays.
    assert (got.n_rows.to_numpy() == want.n_rows.to_numpy()).all()
    assert (got.n_days.to_numpy() == want.n_days.to_numpy()).all()


def test_the_city_aggregation_reproduces_the_shipped_cities(shipped):
    got = derive.cities_from_areas(shipped["areas"])
    want = shipped["cities"]
    assert len(got) == len(want) == 27
    assert got.ci.to_numpy() == pytest.approx(want.ci.to_numpy(), rel=1e-9, abs=1e-9)
    # Bracket access: `df.rank` resolves to the DataFrame method, not the column.
    assert (got["rank"].to_numpy() == want["rank"].to_numpy()).all()


def test_the_six_quiet_areas_are_excluded_from_rows(shipped):
    """They are captured but not published (plan 1): absent from traffic_areas.csv is exactly
    what marks them, so the row step must drop their 1,453 rows."""
    got = derive.rows_from_clean(shipped["clean"], shipped["areas"])
    quiet = {("Alger", 14), ("Alger", 15), ("Alger", 16),
             ("Constantine", 2), ("Constantine", 3), ("Annaba", 3)}
    assert not any((c, a) in quiet for c, a in zip(got.city, got.area))
    assert len(shipped["clean"]) - len(got) == 1453
