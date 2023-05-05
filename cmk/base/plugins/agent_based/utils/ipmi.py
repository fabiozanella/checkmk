#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypedDict,
    Union,
)

from ..agent_based_api.v1 import check_levels, Metric, Result, State, type_defs

# TODO: Cleanup the whole status text mapping in utils/ipmi.py, ipmi_sensors.include, ipmi.py


@dataclass
class Sensor:
    status_txt: str
    unit: str
    value: Optional[float] = None
    crit_low: Optional[float] = None
    warn_low: Optional[float] = None
    warn_high: Optional[float] = None
    crit_high: Optional[float] = None


Section = Dict[str, Sensor]
IgnoreParams = Mapping[str, Sequence[str]]
StatusTxtMapping = Callable[[str], State]


@dataclass(frozen=True)
class UserLevels:
    upper: tuple[float, float] | None
    lower: tuple[float, float] | None


class DiscoveryParams(TypedDict):
    discovery_mode: Union[
        Tuple[Literal["summarize"], IgnoreParams],
        Tuple[Literal["single"], IgnoreParams],
    ]


def _check_ignores(
    to_check: str,
    ignores: Sequence[str],
) -> bool:
    return any(to_check.startswith(ign) for ign in ignores)


def ignore_sensor(
    sensor_name: str,
    status_txt: str,
    ignore_params: IgnoreParams,
) -> bool:
    """
    >>> ignore_sensor("name", "status", {})
    False
    >>> ignore_sensor("name", "status", {"ignored_sensors": ["name"]})
    True
    >>> ignore_sensor("name", "status", {"ignored_sensorstates": ["status"]})
    True
    """
    return _check_ignores(
        sensor_name,
        ignore_params.get("ignored_sensors", []),
    ) or _check_ignores(
        status_txt,
        ignore_params.get("ignored_sensorstates", []),
    )


def check_ipmi(
    item: str,
    params: Mapping[str, Any],
    section: Section,
    temperature_metrics_only: bool,
    status_txt_mapping: StatusTxtMapping,
) -> type_defs.CheckResult:
    if item in ["Summary", "Summary FreeIPMI"]:
        yield from _check_ipmi_summarized(
            params,
            section,
            status_txt_mapping,
        )
    elif item in section:
        yield from _check_ipmi_detailed(
            item,
            params,
            section[item],
            temperature_metrics_only,
            status_txt_mapping,
        )


def _unit_to_render_func(unit: str) -> Callable[[float], str]:
    unit_suffix = unit and "unspecified" not in unit and " %s" % unit or ""
    unit_suffix = unit_suffix.replace("percent", "%").replace("%", "%%")
    return lambda x: ("%.2f" + unit_suffix) % x


def _compile_user_levels_map(params: Mapping[str, Any]) -> Mapping[str, UserLevels]:
    return {
        sensorname: UserLevels(
            upper=levels.get("upper"),
            lower=levels.get("lower"),
        )
        for sensorname, levels in reversed(params.get("numerical_sensor_levels", []))
    }


def _sensor_levels_to_check_levels(
    sensor_warn: Optional[float],
    sensor_crit: Optional[float],
) -> Optional[Tuple[float, float]]:
    if sensor_crit is None:
        return None
    warn = sensor_warn if sensor_warn is not None else sensor_crit
    return warn, sensor_crit


def _check_status_txt(
    status_txt: str,
    status_txt_mapping: StatusTxtMapping,
    user_configured_states: Iterable[tuple[str, State]],
) -> Result:
    for status_txt_beginning, mon_state in user_configured_states:
        if status_txt.startswith(status_txt_beginning):
            return Result(
                state=mon_state,
                summary=f"Status: {status_txt}",
                details="Monitoring state of sensor status set by user-configured rules",
            )
    return Result(
        state=status_txt_mapping(status_txt),
        summary=f"Status: {status_txt}",
    )


def _check_ipmi_detailed(
    item: str,
    params: Mapping[str, Any],
    sensor: Sensor,
    temperature_metrics_only: bool,
    status_txt_mapping: StatusTxtMapping,
) -> type_defs.CheckResult:
    yield _check_status_txt(
        sensor.status_txt,
        status_txt_mapping,
        (
            (status_txt_beginning, State(mon_state_int))
            for status_txt_beginning, mon_state_int in params.get("sensor_states", [])
        ),
    )

    if sensor.value is None:
        return

    metric = None
    if not temperature_metrics_only:
        metric = Metric(
            item.replace("/", "_"),
            sensor.value,
            levels=(sensor.warn_high, sensor.crit_high),
        )

    # Do not save performance data for FANs. This produces a lot of data and is - in my
    # opinion - useless.
    elif "temperature" in item.lower() or "temp" in item.lower() or sensor.unit == "C":
        metric = Metric(
            "value",
            sensor.value,
            levels=(None, sensor.crit_high),
        )

    sensor_result, *_ = check_levels(
        sensor.value,
        levels_upper=_sensor_levels_to_check_levels(sensor.warn_high, sensor.crit_high),
        levels_lower=_sensor_levels_to_check_levels(sensor.warn_low, sensor.crit_low),
        render_func=_unit_to_render_func(sensor.unit),
    )
    yield Result(
        state=sensor_result.state,
        summary=sensor_result.summary,
    )
    if metric:
        yield metric

    user_levels_map = _compile_user_levels_map(params)
    if levels := user_levels_map.get(item):
        yield from check_levels(
            sensor.value,
            levels_upper=levels.upper,
            levels_lower=levels.lower,
            render_func=_unit_to_render_func(sensor.unit),
            label=item,
        )


def _check_ipmi_summarized(
    params: Mapping[str, Any],
    section: Section,
    status_txt_mapping: StatusTxtMapping,
) -> type_defs.CheckResult:
    yield from _average_ambient_temperature(section)

    yield Result(state=State.OK, summary=f"{len(section)} sensors in total")

    for title, state, texts in zip(
        ("ok", "warning", "critical", "skipped"),
        (State.OK, State.WARN, State.CRIT, State.OK),
        _check_individual_sensors(params, section, status_txt_mapping),
    ):
        if not texts:
            continue
        yield Result(state=state, summary=f"{len(texts)} sensors {title}")
        yield from (Result(state=state, notice=text) for text in texts)


def _check_individual_sensors(
    params: Mapping[str, Any],
    section: Section,
    status_txt_mapping: StatusTxtMapping,
) -> tuple[list[str], list[str], list[str], list[str]]:
    user_levels_map = _compile_user_levels_map(params)

    warn_texts = []
    crit_texts = []
    ok_texts = []
    skipped_texts = []

    for sensor_name, sensor in section.items():
        # Skip datasets which have no valid data (zero value, no unit and state nc)
        if ignore_sensor(sensor_name, sensor.status_txt, params) or (
            sensor.value == 0 and sensor.unit == "" and sensor.status_txt.startswith("nc")
        ):
            skipped_texts.append("%s (%s)" % (sensor_name, sensor.status_txt))
            continue

        txt = "%s (%s)" % (sensor_name, sensor.status_txt)
        sensor_state = status_txt_mapping(sensor.status_txt)
        for wato_status_txt, wato_status in params.get("sensor_states", []):
            if sensor.status_txt.startswith(wato_status_txt):
                sensor_state = State(wato_status)
                break

        if sensor.value is not None and (levels := user_levels_map.get(sensor_name)):
            (sensor_result,) = check_levels(
                sensor.value,
                levels_upper=levels.upper,
                levels_lower=levels.lower,
                render_func=_unit_to_render_func(sensor.unit),
                label=sensor_name,
            )
            sensor_state = State.worst(sensor_state, sensor_result.state)
            txt = sensor_result.summary

        if sensor_state is State.WARN:
            warn_texts.append(txt)
        elif sensor_state is State.CRIT:
            crit_texts.append(txt)
        else:
            ok_texts.append(txt)

    return ok_texts, warn_texts, crit_texts, skipped_texts


def _average_ambient_temperature(section: Section) -> Iterable[Metric]:
    if values := [
        sensor.value
        for sensor_name, sensor in section.items()
        if sensor.value is not None and ("amb" in sensor_name or "Ambient" in sensor_name)
    ]:
        yield Metric("ambient_temp", sum(values) / float(len(values)))
