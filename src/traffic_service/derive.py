# traffic-service/src/traffic_service/derive.py
"""The v2 derivation: the index math as pure pandas over caller-supplied frames.

The chain, pinned end to end by `tests/test_derive.py`, which reproduces the shipped corpus
CSVs (the only specification of this math that exists — nothing in the repo writes them):

    rows    observed_m  = green_m + yellow_m + red_m + dark_red_m
            gridlock_eq = 1*yellow_m + 2*red_m + 3*dark_red_m
            ci_raw      = gridlock_eq / capacity_m
            ci          = clip((ci_raw - freeflow) / (1 - freeflow) * 100, 0, 100)
    daily   ci = coverage-weighted mean of the day's row CIs, w = observed_m / capacity_m
    areas   ci = mean over days of each day's PLAIN mean row ci
    cities  ci = area CIs weighted by capacity_m; rank = ci descending, 1-based

`capacity_m` (K = p99 of observed metres) and `freeflow` (the p20 floor) are FROZEN per-area
numbers: they arrive as the `floors_df` argument (`traffic_areas.csv`'s shape:
`city, area, capacity_m, freeflow`) and are carried through, never re-derived here — the spec
seeds the service from that file, so a floor or K review is a re-seed, not a code change.

Every function is pure pandas on its arguments: no file I/O, no database, no module state. The
night job builds the frames from `measurements` joined to `zones` (which already holds `city`
and `area_no`, so no name is ever re-derived from a filename); the oracle test builds them from
the CSVs, and both must land on the same numbers.

Input rows are never assumed unique: the historical `traffic_clean.csv` floors its `time` to
the hour and carries duplicate `(city, area, date, time)` keys (44 of them survive into the
published `traffic_rows.csv`). The derivation works on whatever rows it is given, in the order
it is given them; grouped outputs come out sorted by their group keys, which is exactly the
shipped files' order.
"""
from __future__ import annotations

import pandas as pd

__all__ = [
    "CLEAN_COLUMNS",
    "areas_from_rows",
    "cities_from_areas",
    "daily_from_rows",
    "rows_from_clean",
]

# The 24 columns of the clean frame, in the shipped order. The night job projects its
# `measurements`-joined-to-`zones` read onto exactly these before calling anything here.
CLEAN_COLUMNS = (
    "city", "area", "date", "time",
    "total_px_length", "total_skel_px",
    "green_m", "green_mean_w", "green_highway_m", "green_normal_m",
    "yellow_m", "yellow_mean_w", "yellow_highway_m", "yellow_normal_m",
    "red_m", "red_mean_w", "red_highway_m", "red_normal_m",
    "dark_red_m", "dark_red_mean_w", "dark_red_highway_m", "dark_red_normal_m",
    "total_highway_m", "total_normal_m",
)

# The frozen floors frame's columns, in the shipped order.
FLOOR_COLUMNS = ("city", "area", "capacity_m", "freeflow")


def rows_from_clean(clean_df, floors_df) -> pd.DataFrame:
    """The row-level index, from the clean frame plus the frozen per-area floors.

    Joins the floors on `(city, area)` and drops what has no floors row — that INNER join is
    the quiet-area exclusion, so the six unpublished areas' 1,453 clean rows fall out here.
    Emits `city, area, date, time, observed_m, gridlock_eq, ci_raw, freeflow, ci` in that
    order, preserving the input row order (pandas keeps the left keys through the merge);
    duplicate `(city, area, date, time)` keys pass through untouched, never deduplicated.
    """
    floors = floors_df[list(FLOOR_COLUMNS)]
    merged = clean_df.merge(floors, on=["city", "area"], how="inner")
    observed_m = (merged["green_m"] + merged["yellow_m"]
                  + merged["red_m"] + merged["dark_red_m"])
    gridlock_eq = (merged["yellow_m"] * 1.0 + merged["red_m"] * 2.0
                   + merged["dark_red_m"] * 3.0)
    ci_raw = gridlock_eq / merged["capacity_m"]
    ci = ((ci_raw - merged["freeflow"]) / (1.0 - merged["freeflow"]) * 100.0).clip(0.0, 100.0)
    return pd.DataFrame({
        "city": merged["city"],
        "area": merged["area"],
        "date": merged["date"],
        "time": merged["time"],
        "observed_m": observed_m,
        "gridlock_eq": gridlock_eq,
        "ci_raw": ci_raw,
        "freeflow": merged["freeflow"],
        "ci": ci,
    })


def daily_from_rows(rows_df, floors_df) -> pd.DataFrame:
    """Per-city-per-day index: the coverage-weighted mean of the day's row indices.

    `sum(w * ci) / sum(w)` with `w = observed_m / capacity_m` — the row's observed metres as a
    share of the area's frozen capacity. The floors frame is required here because `rows_df`
    carries `freeflow` but not K. Measured against the shipped `traffic_daily.csv`: only this
    weight reproduces all 648 values (plain mean 0/648, `w = observed_m` 24/648, `w = ci_raw`
    and `w = gridlock_eq` 0/648), so the weight is not a knob. Emits `city, date, ci` sorted
    by `(city, date)`, which is the shipped order.
    """
    floors = floors_df[["city", "area", "capacity_m"]]
    merged = rows_df.merge(floors, on=["city", "area"], how="inner")
    weight = merged["observed_m"] / merged["capacity_m"]
    merged = merged.assign(weight=weight, weighted=merged["ci"] * weight)
    grouped = merged.groupby(["city", "date"], sort=True)
    ci = (grouped["weighted"].sum() / grouped["weight"].sum()).rename("ci")
    return ci.reset_index()[["city", "date", "ci"]]


def areas_from_rows(rows_df, floors_df) -> pd.DataFrame:
    """Per-area index: the mean over days of each day's PLAIN mean row ci.

    The two-step mean is deliberate and pinned by the oracle: it is NOT a plain mean of all the
    area's rows (error up to 1.39 against the shipped files — later days carry more rows) and
    NOT a coverage-weighted mean of the daily means (error up to 1.81). `capacity_m` and
    `freeflow` are carried through from `floors_df` verbatim; emitting them is the whole point
    of passing the frame in, and this function never re-derives K or the floor. `n_rows` is the
    area's row count, `n_days` its distinct dates. Emits `city, area, capacity_m, freeflow, ci,
    n_rows, n_days` sorted by `(city, area)`, which is the shipped order.
    """
    daily = rows_df.groupby(["city", "area", "date"], sort=True)["ci"].mean()
    ci = daily.groupby(["city", "area"]).mean().rename("ci")
    grouped = rows_df.groupby(["city", "area"], sort=True)
    out = ci.to_frame()
    out = out.join(grouped.size().rename("n_rows"))
    out = out.join(grouped["date"].nunique().rename("n_days"))
    out = out.reset_index().merge(floors_df[list(FLOOR_COLUMNS)], on=["city", "area"], how="inner")
    return out[["city", "area", "capacity_m", "freeflow", "ci", "n_rows", "n_days"]]


def cities_from_areas(areas_df) -> pd.DataFrame:
    """Per-city index: the area CIs weighted by `capacity_m`, plus the descending 1-based rank.

    Emits `city, ci, rank` sorted by city (the shipped order). No ties in the shipped data, so
    the rank tie method is unobservable against the oracle; `min` is used so a tie would share
    a rank rather than being split arbitrarily.
    """
    weighted = areas_df.assign(weighted=areas_df["ci"] * areas_df["capacity_m"])
    grouped = weighted.groupby("city", sort=True)
    ci = (grouped["weighted"].sum() / grouped["capacity_m"].sum()).rename("ci")
    rank = ci.rank(ascending=False, method="min").astype("int64")
    return pd.DataFrame({"city": ci.index, "ci": ci.to_numpy(), "rank": rank.to_numpy()})
