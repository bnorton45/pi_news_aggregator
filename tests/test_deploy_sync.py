"""The Helm chart embeds copies of authored files (helm can't read outside the chart
dir). These tests fail CI the moment a copy drifts from its source of truth."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHART_FILES = ROOT / "deploy" / "helm" / "news-aggregator" / "files"


def test_chart_schema_matches_libs_schema() -> None:
    src = (ROOT / "libs" / "schema" / "schema.sql").read_text()
    copy = (CHART_FILES / "schema.sql").read_text()
    assert copy == src, "chart files/schema.sql drifted from libs/schema/schema.sql — re-copy it"


def test_chart_readonly_role_matches_libs_schema() -> None:
    src = (ROOT / "libs" / "schema" / "readonly_role.sh").read_text()
    copy = (CHART_FILES / "readonly_role.sh").read_text()
    assert copy == src, "chart files/readonly_role.sh drifted from libs/schema/readonly_role.sh"


def test_chart_retrain_role_matches_libs_schema() -> None:
    src = (ROOT / "libs" / "schema" / "retrain_role.sh").read_text()
    copy = (CHART_FILES / "retrain_role.sh").read_text()
    assert copy == src, "chart files/retrain_role.sh drifted from libs/schema/retrain_role.sh"


def test_chart_nats_conf_matches_policies() -> None:
    src = (ROOT / "deploy" / "policies" / "nats-accounts.conf").read_text()
    copy = (CHART_FILES / "nats-accounts.conf").read_text()
    assert (
        copy == src
    ), "chart files/nats-accounts.conf drifted from deploy/policies/nats-accounts.conf"
