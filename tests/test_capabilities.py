from pavs.domain.enums import CapabilityStatus as S, ProviderKind as P, ProviderState as St, Verification as V
from pavs.polarion.adapters import AdapterManager, FutureAdapter
from pavs.polarion.capabilities import CapabilityRegistry, ResolutionContext
from pavs.polarion.version.release import Release

REG = CapabilityRegistry.load()


def _by_id(records):
    return {r.capability_id: r for r in records}


def test_2410_rest_on_uses_soap_for_custom_fields():
    ctx = ResolutionContext(release=Release.parse("2410"),
                            providers={P.REST: St.AUTHENTICATED, P.SOAP: St.AUTHENTICATED})
    recs = _by_id(REG.resolve(ctx))
    cf = recs["customfields.definitions"]
    assert cf.provider == P.SOAP and cf.status == S.PARTIAL
    assert recs["server.metadata"].status == S.UNSUPPORTED      # /metadata only from 2512
    assert recs["projects.list"].provider == P.REST


def test_2512_uses_rest_for_custom_fields():
    ctx = ResolutionContext(release=Release.parse("2512"),
                            providers={P.REST: St.AUTHENTICATED, P.SOAP: St.AUTHENTICATED})
    recs = _by_id(REG.resolve(ctx))
    assert recs["customfields.definitions"].provider == P.REST
    assert recs["customfields.definitions"].status == S.SUPPORTED
    assert recs["server.metadata"].status == S.SUPPORTED


def test_rest_disabled_falls_back_to_soap_everywhere():
    ctx = ResolutionContext(release=Release.parse("2410"),
                            providers={P.REST: St.DISABLED, P.SOAP: St.AUTHENTICATED})
    recs = _by_id(REG.resolve(ctx))
    assert recs["workitems.query"].provider == P.SOAP
    assert recs["permissions.check"].status == S.SUPPORTED


def test_not_signed_in_means_needs_permission():
    ctx = ResolutionContext(release=Release.parse("2606"),
                            providers={P.REST: St.AVAILABLE, P.SOAP: St.AVAILABLE})
    recs = _by_id(REG.resolve(ctx))
    assert recs["projects.list"].status == S.NEEDS_PERMISSION


def test_svn_only_capabilities_need_opt_in():
    base = {P.REST: St.AUTHENTICATED, P.SOAP: St.AUTHENTICATED}
    off = _by_id(REG.resolve(ResolutionContext(release=Release.parse("2606"), providers=base)))
    assert off["permissions.config"].status == S.UNSUPPORTED
    assert "not enabled" in off["permissions.config"].detail
    on = _by_id(REG.resolve(ResolutionContext(release=Release.parse("2606"),
                                              providers={**base, P.SVN: St.AUTHENTICATED}, svn_enabled=True)))
    assert on["permissions.config"].status == S.SUPPORTED and on["permissions.config"].provider == P.SVN


def test_live_spec_beats_release_number():
    # Server claims 2410 but its live spec contains the 2512 custom-field endpoint.
    live = {("GET", "/projects/{}/customfields/{}/{}"), ("GET", "/projects")}
    ctx = ResolutionContext(release=Release.parse("2410"), live_rest=live,
                            providers={P.REST: St.AUTHENTICATED, P.SOAP: St.AUTHENTICATED})
    recs = _by_id(REG.resolve(ctx))
    assert recs["customfields.definitions"].provider == P.REST
    assert recs["customfields.definitions"].verification == V.DETECTED
    assert recs["users.current"].provider == P.SOAP   # GET /user missing from live spec


def test_soap_wsdl_missing_operation_is_detected():
    ctx = ResolutionContext(release=Release.parse("2606"), providers={P.SOAP: St.AUTHENTICATED},
                            soap_ops={"Security": {"getGlobalRoles"}})
    recs = _by_id(REG.resolve(ctx))
    assert recs["roles.global"].verification == V.DETECTED
    assert recs["permissions.check"].status == S.UNSUPPORTED
    assert "WSDL" in recs["permissions.check"].detail


def test_adapter_selection():
    mgr = AdapterManager()
    assert [r.code for r in mgr.known_releases] == ["2410", "2506", "2512", "2606"]
    a, notes = mgr.select(Release.parse("2512"))
    assert a.name == "Polarion 2512" and not notes and not a.validated
    a, notes = mgr.select(Release.parse("2503"))          # not a real release
    assert a.name == "Polarion 2410" and "nearest lower" in notes[0]
    a, notes = mgr.select(Release.parse("2603"))
    assert a.name == "Polarion 2512"
    a, _ = mgr.select(Release.parse("2712"))
    assert isinstance(a, FutureAdapter) and "newer" in a.warnings()[0]
    a, _ = mgr.select(None)
    assert isinstance(a, FutureAdapter)
    a, _ = AdapterManager(validated={"2606"}).select(Release.parse("2606"))
    assert a.validated and not a.warnings()


def test_2410_adapter_adds_quirk_notes():
    a, _ = AdapterManager().select(Release.parse("2410"))
    ctx = ResolutionContext(release=Release.parse("2410"), providers={P.SOAP: St.AUTHENTICATED})
    rec = a.adjust(_by_id(REG.resolve(ctx))["customfields.definitions"])
    assert "2512" in rec.detail
    assert a.custom_fields_via_rest() is False and a.save_hooks_scoped_by_type() is False
