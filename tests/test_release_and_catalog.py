import pytest

from pavs.polarion.capabilities import CapabilityRegistry, EndpointCatalog
from pavs.polarion.capabilities.catalog import endpoint_key, norm_path
from pavs.polarion.version.release import Release


@pytest.mark.parametrize("text,code,internal", [
    ("2410", "2410", "3.24.10"), ("2506", "2506", "3.25.6"), ("3.25.12", "2512", "3.25.12"),
    ("Polarion 2606", "2606", "3.26.6"), ("3.26.6.1", "2606", "3.26.6"), ("2712", "2712", "3.27.12"),
])
def test_release_parse(text, code, internal):
    r = Release.parse(text)
    assert r.code == code and r.internal == internal


def test_release_order_and_invalid():
    assert Release.parse("2410") < Release.parse("2506") < Release.parse("2512") < Release.parse("2606")
    assert Release.try_parse("1399") is None   # month 99
    assert Release.try_parse("hello") is None
    with pytest.raises(ValueError):
        Release.parse("2613")


def test_norm_path():
    assert norm_path("/polarion/rest/v1/projects/{projectId}/workitems?x=1") == "/projects/{}/workitems"
    assert endpoint_key("get /projects/{a}") == ("GET", "/projects/{}")


def test_catalog_knows_release_history():
    cat = EndpointCatalog.load()
    assert len(cat) > 250
    r2410, r2512 = Release.parse("2410"), Release.parse("2512")
    # custom-field configuration endpoints arrived in 2512
    spec = "GET /projects/{projectId}/customfields/{resourceType}/{targetType}"
    assert cat.exists(spec, r2410) is False
    assert cat.exists(spec, r2512) is True
    # collections arrived in 2410
    assert cat.exists("GET /projects/{projectId}/collections", Release.parse("2404")) is False
    assert cat.exists("GET /projects/{projectId}/collections", r2410) is True
    # old endpoint: present everywhere the log covers
    assert cat.exists("GET /projects", r2410) is True
    # removed in 2506
    removed = "POST /projects/{projectId}/testruns/{testRunId}/actions/exportTestsToExcel"
    assert cat.exists(removed, r2410) is True
    assert cat.exists(removed, Release.parse("2506")) is False
    # unknown endpoint
    assert cat.exists("GET /nope", r2410) is None


def test_every_rest_requirement_in_capabilities_exists_in_catalog():
    """Catches typos in capabilities.yaml."""
    reg = CapabilityRegistry.load()
    for cap in reg.defs:
        for strat in cap.strategies:
            if strat.provider.value == "rest":
                for req in strat.requires:
                    assert reg.catalog.get(req) is not None, f"{cap.id}: {req} not in catalog"
