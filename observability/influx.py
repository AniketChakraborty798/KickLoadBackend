import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _influx_enabled() -> bool:
    return all(
        [
            os.getenv("INFLUX_URL"),
            os.getenv("INFLUX_TOKEN"),
            os.getenv("INFLUX_ORG"),
            os.getenv("INFLUX_BUCKET"),
        ]
    )


def influx_enabled() -> bool:
    return _influx_enabled()


def _flux_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_common_filters(
    *,
    task_id: Optional[str],
    jtl_filename: Optional[str],
    user_email: Optional[str],
    allow_application_fallback: bool = False,
) -> List[str]:
    filters = []
    if task_id:
        if allow_application_fallback:
            escaped = _flux_escape(str(task_id))
            filters.append(f'(r.task_id == "{escaped}" or r.application == "{escaped}")')
        else:
            filters.append(f'r.task_id == "{_flux_escape(str(task_id))}"')
    if jtl_filename:
        filters.append(f'r.jtl_filename == "{_flux_escape(str(jtl_filename))}"')
    if user_email:
        filters.append(f'r.user_email == "{_flux_escape(str(user_email))}"')
    return filters


def _query_points(
    *,
    field: str,
    label: Optional[str],
    exclude_total: bool,
    task_id: Optional[str],
    jtl_filename: Optional[str],
    user_email: Optional[str],
    range_seconds: int,
) -> List[Dict[str, Any]]:
    if not _influx_enabled():
        return []

    influx_url = os.getenv("INFLUX_URL")
    influx_token = os.getenv("INFLUX_TOKEN")
    influx_org = os.getenv("INFLUX_ORG")
    influx_bucket = os.getenv("INFLUX_BUCKET")

    from influxdb_client import InfluxDBClient

    filters = _build_common_filters(
        task_id=task_id,
        jtl_filename=jtl_filename,
        user_email=user_email,
        allow_application_fallback=True,
    )
    if label is not None:
        filters.append(f'r.label == "{_flux_escape(label)}"')
    if exclude_total:
        filters.append('r.label != "TOTAL"')

    filter_lines = "\n  ".join([f"|> filter(fn: (r) => {f})" for f in filters])

    flux = f"""
from(bucket: "{influx_bucket}")
  |> range(start: -{int(range_seconds)}s)
  |> filter(fn: (r) => r._measurement == "jmeter_summary")
  |> filter(fn: (r) => r._field == "{_flux_escape(field)}")
  {filter_lines}
  |> sort(columns: ["_time"])
"""

    points: List[Dict[str, Any]] = []
    with InfluxDBClient(url=influx_url, token=influx_token, org=influx_org) as client:
        query_api = client.query_api()
        tables = query_api.query(flux)
        for table in tables:
            for record in table.records:
                points.append(
                    {
                        "time": record.get_time().isoformat() if record.get_time() else None,
                        "value": record.get_value(),
                    }
                )

    return points


def _query_per_label_series(
    *,
    field: str,
    task_id: Optional[str],
    jtl_filename: Optional[str],
    user_email: Optional[str],
    range_seconds: int,
) -> List[Dict[str, Any]]:
    if not _influx_enabled():
        return []

    influx_url = os.getenv("INFLUX_URL")
    influx_token = os.getenv("INFLUX_TOKEN")
    influx_org = os.getenv("INFLUX_ORG")
    influx_bucket = os.getenv("INFLUX_BUCKET")

    from influxdb_client import InfluxDBClient

    filters = _build_common_filters(
        task_id=task_id,
        jtl_filename=jtl_filename,
        user_email=user_email,
        allow_application_fallback=True,
    )
    filters.append('r.label != "TOTAL"')
    filter_lines = "\n  ".join([f"|> filter(fn: (r) => {f})" for f in filters])

    flux = f"""
from(bucket: "{influx_bucket}")
  |> range(start: -{int(range_seconds)}s)
  |> filter(fn: (r) => r._measurement == "jmeter_summary")
  |> filter(fn: (r) => r._field == "{_flux_escape(field)}")
  {filter_lines}
  |> group(columns: ["label"])
  |> sort(columns: ["_time"])
"""

    series: List[Dict[str, Any]] = []
    with InfluxDBClient(url=influx_url, token=influx_token, org=influx_org) as client:
        query_api = client.query_api()
        tables = query_api.query(flux)
        for table in tables:
            label = None
            points: List[Dict[str, Any]] = []
            for record in table.records:
                if label is None:
                    label = record.values.get("label")
                points.append(
                    {
                        "time": record.get_time().isoformat() if record.get_time() else None,
                        "value": record.get_value(),
                    }
                )
            if label and points:
                series.append({"label": label, "points": points})

    return series


def query_jmeter_timeseries(
    *,
    task_id: Optional[str] = None,
    jtl_filename: Optional[str] = None,
    user_email: Optional[str] = None,
    range_seconds: int = 86400,
) -> Optional[Dict[str, Any]]:
    if not _influx_enabled():
        return None

    throughput = _query_points(
        field="throughput_rps",
        label="TOTAL",
        exclude_total=False,
        task_id=task_id,
        jtl_filename=jtl_filename,
        user_email=user_email,
        range_seconds=range_seconds,
    )
    avg_response = _query_points(
        field="average_ms",
        label="TOTAL",
        exclude_total=False,
        task_id=task_id,
        jtl_filename=jtl_filename,
        user_email=user_email,
        range_seconds=range_seconds,
    )
    error_pct = _query_points(
        field="error_pct",
        label="TOTAL",
        exclude_total=False,
        task_id=task_id,
        jtl_filename=jtl_filename,
        user_email=user_email,
        range_seconds=range_seconds,
    )
    per_label_avg = _query_per_label_series(
        field="average_ms",
        task_id=task_id,
        jtl_filename=jtl_filename,
        user_email=user_email,
        range_seconds=range_seconds,
    )

    return {
        "throughput_rps": throughput,
        "average_ms": avg_response,
        "error_pct": error_pct,
        "per_label_avg_ms": per_label_avg,
    }


def write_jmeter_summary_to_influx(
    *,
    summary_rows: List[Dict[str, Any]],
    task_id: str,
    user_email: Optional[str] = None,
    jmx_filename: Optional[str] = None,
    jtl_filename: Optional[str] = None,
) -> bool:
    """
    Best-effort write of the parsed JTL summary rows into InfluxDB v2.

    Expected row shape (from `jmeter_core.parse_jtl_summary`):
      {
        "label": "TOTAL" | "<sampler name>",
        "samples": int,
        "average_ms": float,
        "min_ms": int,
        "max_ms": int,
        "stddev_ms": float,
        "error_pct": float,
        "throughput_rps": float,
        "received_kbps": float,
        "sent_kbps": float,
        "avg_bytes": float
      }
    """
    if not _influx_enabled():
        return False

    influx_url = os.getenv("INFLUX_URL")
    influx_token = os.getenv("INFLUX_TOKEN")
    influx_org = os.getenv("INFLUX_ORG")
    influx_bucket = os.getenv("INFLUX_BUCKET")

    from influxdb_client import InfluxDBClient, Point, WritePrecision
    from influxdb_client.client.write_api import SYNCHRONOUS

    now = datetime.now(timezone.utc)

    points = []
    for row in summary_rows or []:
        label = str(row.get("label", "UNKNOWN"))

        p = (
            Point("jmeter_summary")
            .tag("label", label)
            .tag("task_id", str(task_id))
            .time(now, WritePrecision.S)
        )
        if user_email:
            p = p.tag("user_email", str(user_email))
        if jmx_filename:
            p = p.tag("jmx_filename", str(jmx_filename))
        if jtl_filename:
            p = p.tag("jtl_filename", str(jtl_filename))

        for field in [
            "samples",
            "average_ms",
            "min_ms",
            "max_ms",
            "stddev_ms",
            "error_pct",
            "throughput_rps",
            "received_kbps",
            "sent_kbps",
            "avg_bytes",
        ]:
            if field in row and row[field] is not None:
                p = p.field(field, row[field])

        points.append(p)

    if not points:
        return False

    with InfluxDBClient(url=influx_url, token=influx_token, org=influx_org) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=influx_bucket, org=influx_org, record=points)

    return True

