"""Phase 5: script/template static analysis and regression comparison."""
import pytest

from mock_polarion import repo_fixture
from pavs.app import build_context
from pavs.domain.enums import TestStatus as S
from pavs.engines.analysis.analyzer import SourceFile, analyze_files, api_index, find_node, mask_js, release_code
from pavs.engines.analysis.sources import from_folder, velocity_from_page_xml
from pavs.infra.vault import MemoryBackend
from pavs.services.regression import diff_runs, summarize

URL = "https://mock.test/polarion"


def run_js(code, target="2410", name="t.js", syntax=False):
    f, _ = analyze_files([SourceFile(name, "js", code)], target, check_syntax=syntax)
    return f


def rules(findings):
    return {x.rule for x in findings}


# ---------------------------------------------------------------- JavaScript rules (each is a bug documented in the user's notes)
@pytest.mark.parametrize("code,rule", [
    ('var password = "hunter22x";', "S02"),
    ('importPackage(Packages.java.io);', "S03"),
    ('var v = wi.getCustomField("version") + 1;', "S05"),
    ('if (user.getId() === "\\tmTest") { x(); }', "S06"),
    ('if (user.getId() === " mTest") { x(); }', "S06"),
    ('for (var k in doc.getContainedWorkItems()) { x(k); }', "S07"),
    ('var a = wi.getAvailableActions(); wi.performAction(a[0].getActionId());', "S08"),
    ('var t = context.getTarget(); t.save();', "S09"),
    ('switch (k) { case "a": r = 1; break; }', "S10"),
    ('var w = new java.io.BufferedWriter(new java.io.FileWriter("x")); w.write("y");', "S11"),
    ('throw new PolarionScriptingException("no");\n"done";', "S12"),
    ('eval("1+1");', "S13"),
])
def test_each_js_rule_fires(code, rule):
    assert rule in rules(run_js(code))


@pytest.mark.parametrize("code,rule", [
    ('var password = getPassword();', "S02"),
    ('// password = "hunter22x"', "S02"),
    ('var s = "for (k in x)";', "S07"),
    ('var a = wi.getAvailableActions().find(function(x){return x}); if (a === undefined) { return false; } wi.performAction(a.getActionId());', "S08"),
    ('switch (k) { case "a": r = 1; break; default: r = 0; }', "S10"),
    ('var w = new java.io.FileWriter("x"); w.close();', "S11"),
    ('throw new PolarionScriptingException("no");\ntrue;', "S12"),
    ('var n = Number(wi.getCustomField("version")) + 1;', "S05"),
])
def test_rules_do_not_fire_on_correct_code(code, rule):
    assert rule not in rules(run_js(code))


def test_comments_and_strings_are_masked():
    src = 'var a = "x // not a comment"; // real comment\n/* block */ var b = 1;'
    code, strings = mask_js(src)
    assert "real comment" not in code and "block" not in code and "x // not" not in code
    assert strings and strings[0][1] == "x // not a comment" and len(code) == len(src)


@pytest.mark.skipif(find_node() is None, reason="Node.js not installed")
def test_syntax_errors_are_found_with_line_numbers():
    f = run_js("var a = 1;\nvar b = ;\nvar c = 3;", syntax=True)
    s01 = [x for x in f if x.rule == "S01"]
    assert s01 and s01[0].line == 2
    assert not [x for x in run_js("var a = 1;\nreturn a;", syntax=True) if x.rule == "S01"]      # top-level return is legal in scripts


# ---------------------------------------------------------------- version gating from the SDK change log
def test_api_version_gating():
    idx = api_index()
    assert idx["since"]["getScriptManager"] == "2410"
    new_in_2506 = next(n for n, v in sorted(idx["since"].items()) if v == "2506")
    assert "S04" in rules(run_js(f"x.{new_in_2506}(1);", "2410"))
    assert "S04" not in rules(run_js(f"x.{new_in_2506}(1);", "2606"))
    assert "S04" not in rules(run_js("x.getModule();", "2410"))                    # old method
    assert "S04" in rules(run_js("x.getScriptManager();", "2404"))
    assert "S04" not in rules(run_js("x.getScriptManager();", "2410"))
    assert "S04" in rules(run_js("x.canUseICVA();", "2410"))                        # removed in 2404
    assert "S04" not in rules(run_js("x.addHyperlink(1);", "2410"))                # new in one class, old in another: never flagged
    assert release_code("2410") == "2410" and release_code("3.25.12") == "2512" and release_code(None) is None


def test_duplicated_helpers_across_files():
    files = [SourceFile(f"s{i}.js", "js", "function logDebug(m){ print(m); }") for i in range(3)]
    f, _ = analyze_files(files, "2410", check_syntax=False)
    assert "S14" in rules(f)
    f, _ = analyze_files(files[:2], "2410", check_syntax=False)
    assert "S14" not in rules(f)


# ---------------------------------------------------------------- Velocity rules
def run_vm(code):
    f, _ = analyze_files([SourceFile("t.vm", "vm", code)], "2606", check_syntax=False)
    return f


def test_velocity_rules():
    assert "V01" in rules(run_vm("#if($a)\nx\n"))
    assert "V01" in rules(run_vm("x\n#end\n"))
    assert "V01" not in rules(run_vm("#if($a)x#{else}y#end"))
    assert "V01" not in rules(run_vm('text: "$n#if(!$s.equals(\'\')) $s#end",\n#foreach($i in $l)$i#end'))     # #end followed by a quote (real Polarion template)
    assert "V01" not in rules(run_vm("## #if(commented)\n#* #foreach( *#\nok"))
    assert "V02" in rules(run_vm("#foreach($i in $l)\n#set($o = $i.getOldApi())\n$o\n#end"))
    assert "V02" not in rules(run_vm("#set($o = $x.getOldApi())\n#foreach($i in $l)\n$i\n#end"))
    assert "V03" in rules(run_vm('#set($r = $t.query("type:task AND title:*bug"))'))
    assert "V03" not in rules(run_vm('#set($r = $t.query("type:task AND title:bug*"))'))
    assert "V04" in rules(run_vm("#set($orphan = 5)\ntext"))
    assert "V04" not in rules(run_vm("#set($void = $list.add(1))\n#set($used = 1)\n$used"))
    assert "V05" in rules(run_vm("#nosuchmacro($x)"))
    assert "V05" not in rules(run_vm("#macro(m $a)$a#end\n#m(1)\n#info('x')"))


def test_secrets_are_flagged_without_printing_the_value():
    s02 = [x for x in run_js('var token = "abcd1234secret";') if x.rule == "S02"][0]
    assert "abcd1234secret" not in s02.message and "value not shown" in s02.message


# ---------------------------------------------------------------- sources
def test_velocity_extracted_from_page_xml_and_folder(tmp_path):
    xml = repo_fixture.FILES[f"{repo_fixture.P}/pages/spaces/Reports/Coverage/page.xml"]
    blocks = velocity_from_page_xml(xml)
    assert len(blocks) == 1 and "$transaction.workItems" in blocks[0][1] and "&quot;" not in blocks[0][1]
    (tmp_path / "a.js").write_text("var x = 1;")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "w.vm.txt").write_text("#if($a)x#end")
    (tmp_path / "notes.md").write_text("ignored")
    files = from_folder(tmp_path)
    assert sorted((f.name, f.kind) for f in files) == [("a.js", "js"), ("sub/w.vm.txt", "vm")]


# ---------------------------------------------------------------- inside a test run
@pytest.fixture
def ctx(tmp_path, mock_server):
    _, wsgi = mock_server("2606")
    c = build_context(db_path=tmp_path / "p5.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    out = c.connections.quick_connect(URL, "admin", "admin-test-pw", token="test-token-admin")
    assert out.ok, out.message
    yield c, out.environment
    c.close()


def test_run_analyses_repository_templates_read_only(ctx):
    c, env = ctx
    run = c.testing.run(env.id, "acme_demo", health=False, scripts=True)
    res = {r.check_id: r for r in run.results if r.group == "Scripts"}
    assert res["V01"].status == S.FAIL and "render.vm" in " ".join(res["V01"].evidence)
    assert res["V02"].status == S.PARTIAL_PASS and res["V03"].status == S.PARTIAL_PASS
    assert "Coverage" in " ".join(res["V03"].evidence)                              # found inside the page's Script block


def test_local_folder_is_analysed_without_any_connection(tmp_path):
    c = build_context(db_path=tmp_path / "x.sqlite3", vault_backend=MemoryBackend(), log_to_file=False)
    (tmp_path / "bad.js").write_text('var password = "hunter22x";')
    rec = c.testing.analyze_local_scripts(str(tmp_path), "2410")
    assert rec.kind == "scripts" and rec.verdict == "FAILED" and rec.environment_name == "(local files)"
    assert c.testing.history()[0].id == rec.id
    c.close()


def test_script_folder_added_to_a_run(ctx, tmp_path):
    c, env = ctx
    (tmp_path / "ok.js").write_text("var a = 1;")
    run = c.testing.run(env.id, "acme_demo", health=False, scripts=True, script_folder=str(tmp_path))
    assert any("folder" in " ".join(r.evidence) for r in run.results if r.group == "Scripts")


# ---------------------------------------------------------------- regression
def test_regression_between_runs(ctx):
    c, env = ctx
    first = c.testing.run(env.id, "acme_demo", health=True)
    key = ".polarion/tracker/workflow/workflow.xml"
    original = repo_fixture.FILES[key]
    repo_fixture.FILES[key] = original.replace('to="done"/>\n        <transition action="reopen"',
                                               'to="ghost"/>\n        <transition action="reopen"')
    try:
        c.connections.connect(c.environments.get(env.id))
        second = c.testing.run(env.id, "acme_demo", health=True)
    finally:
        repo_fixture.FILES[key] = original
    changes = c.testing.compare_runs(first.id, second.id)
    reg = [x for x in changes if x.kind == "regression"]
    # one broken status also makes a state unreachable: both are real regressions
    assert {x.name for x in reg} == {"Workflow statuses exist in the status enumeration", "Workflow statuses are reachable"}
    assert all(x.new_status == "fail" for x in reg)
    assert summarize(changes)["regression"] == 2 and changes[0].kind == "regression"      # worst first
    assert c.testing.previous_run(second).id == first.id and c.testing.previous_run(first) is None
    assert {x.kind for x in c.testing.compare_runs(second.id, first.id) if x.kind != "same"} == {"fixed"}


def test_diff_detects_new_removed_and_changed():
    from pavs.domain.testing import CheckResult, TestRunRecord

    def rec(*results):
        r = TestRunRecord(environment_id="e", project_id="p")
        r.results = list(results)
        return r

    def mk(cid, st, actual="a"):
        return CheckResult(check_id=cid, group="Health checks", name=cid, status=st, actual=actual)

    old = rec(mk("H1", S.PASS), mk("H2", S.PASS), mk("H3", S.PARTIAL_PASS, "1"))
    new = rec(mk("H1", S.PASS), mk("H3", S.PARTIAL_PASS, "2"), mk("H4", S.FAIL))
    assert {x.name: x.kind for x in diff_runs(old, new)} == {"H1": "same", "H2": "removed", "H3": "changed", "H4": "new_problem"}
