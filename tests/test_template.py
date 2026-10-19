"""Static checks that template.yaml and the code agree (routes, env vars, references, least privilege).

Requires PyYAML (``pip install pyyaml``); skipped otherwise. Complements ``cfn-lint template.yaml``
and ``sam validate --lint``.
"""
import os
import re

import pytest
from helpers import ROOT, load_app

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

PSEUDO = {"AWS::StackName", "AWS::AccountId", "AWS::Region", "AWS::Partition", "AWS::URLSuffix"}


def _template():
    if yaml is None:
        pytest.skip("PyYAML not installed")

    class Loader(yaml.SafeLoader):
        pass

    def tag(name):
        def construct(loader, node):
            if isinstance(node, yaml.ScalarNode):
                return {name: loader.construct_scalar(node)}
            if isinstance(node, yaml.SequenceNode):
                return {name: loader.construct_sequence(node, deep=True)}
            return {name: loader.construct_mapping(node, deep=True)}
        return construct

    for t in ("Ref", "Sub", "GetAtt", "Not", "Equals", "If", "Join", "Select", "Split"):
        Loader.add_constructor(f"!{t}", tag(t))
    with open(os.path.join(ROOT, "template.yaml")) as f:
        return yaml.load(f, Loader=Loader)


def _walk(node):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _functions(t):
    return {k: v for k, v in t["Resources"].items() if v["Type"] == "AWS::Serverless::Function"}


def _statements(fn):
    out = []
    for p in fn["Properties"].get("Policies", []):
        if isinstance(p, dict) and "Statement" in p:
            out.extend(p["Statement"])
    return out


def test_references_resolve():
    t = _template()
    known = set(t["Resources"]) | set(t.get("Parameters", {})) | PSEUDO | {"DocApiApiKey"}
    for node in _walk(t):
        if isinstance(node, dict) and "Ref" in node and isinstance(node["Ref"], str):
            assert node["Ref"] in known, node
        if isinstance(node, dict) and "GetAtt" in node:
            target = node["GetAtt"].split(".")[0] if isinstance(node["GetAtt"], str) else node["GetAtt"][0]
            assert target in known, node
        if isinstance(node, dict) and "Sub" in node and isinstance(node["Sub"], str):
            for ref in re.findall(r"\$\{([^}!]+)\}", node["Sub"]):
                assert ref.split(".")[0] in known, ref


def test_every_api_route_is_wired_both_ways():
    t = _template()
    for name, fn in _functions(t).items():
        events = [e["Properties"] for e in fn["Properties"].get("Events", {}).values() if e["Type"] == "Api"]
        if not events:
            continue
        component = fn["Properties"]["CodeUri"].strip("/").split("/")[-1]
        routes = set(load_app(component).ROUTES)
        declared = {(e["Method"].upper(), e["Path"]) for e in events}
        assert declared == routes, f"{name}: template {declared ^ routes}"


def test_handlers_exist():
    t = _template()
    for name, fn in _functions(t).items():
        props = fn["Properties"]
        module = props["Handler"].split(".")[0]
        assert os.path.exists(os.path.join(ROOT, props["CodeUri"], f"{module}.py")), name
    layer = t["Resources"]["SharedLayer"]["Properties"]["ContentUri"]
    assert os.path.exists(os.path.join(ROOT, layer, "docintel_common", "__init__.py"))


def test_environment_variables_used_in_code_are_defined():
    t = _template()
    defined = set(t["Globals"]["Function"]["Environment"]["Variables"])
    for fn in _functions(t).values():
        defined |= set(fn["Properties"].get("Environment", {}).get("Variables", {}))
    optional = {"AWS_LAMBDA_FUNCTION_NAME", "QA_TOP_K", "COMPARE_MAX_CHARS", "MAX_SEARCH_CHUNKS"}  # have defaults
    used = set()
    for dirpath, _, files in os.walk(os.path.join(ROOT, "src")):
        for f in files:
            if f.endswith(".py"):
                src = open(os.path.join(dirpath, f)).read()
                used |= set(re.findall(r"os\.environ(?:\.get)?[\[(]\"([A-Z_]+)\"", src))
    from docintel_common.aws import TABLE_ENV
    used |= set(TABLE_ENV.values())
    missing = used - defined - optional
    assert not missing, missing


def test_least_privilege_boundaries():
    t = _template()
    fns = _functions(t)

    def grants(fn_name, action, resource_fragment=""):
        for st in _statements(fns[fn_name]):
            actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
            resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            if action in actions and any(resource_fragment in str(r) for r in resources):
                return True
        return False

    pii_fns = [n for n in fns if grants(n, "comprehend:DetectPiiEntities")]
    assert pii_fns == ["PrivacyFunction"]
    assert [n for n in fns if grants(n, "s3:PutObject", "redacted/")] == ["PrivacyFunction"]
    assert [n for n in fns if grants(n, "s3:PutObject", "results/")] == ["ProcessorFunction"]
    assert [n for n in fns if grants(n, "s3:PutObject", "exports/")] == ["ExportFunction"]
    for name, fn in fns.items():
        for st in _statements(fn):
            acts = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
            assert "s3:DeleteObject" not in acts and "s3:*" not in acts and "dynamodb:*" not in acts, name
            if "bedrock:InvokeModel" in acts:
                assert st["Resource"] != "*", f"{name}: Bedrock should be scoped to model ARNs"


def test_only_uploads_trigger_processing():
    t = _template()
    cfg = t["Resources"]["DocsBucket"]["Properties"]["NotificationConfiguration"]["QueueConfigurations"]
    assert [r["Value"] for c in cfg for r in c["Filter"]["S3Key"]["Rules"]] == ["uploads/"]


def test_data_stores_encrypted_and_ttl():
    t = _template()
    for name in ("DocsTable", "AppTable", "ChunksTable"):
        assert t["Resources"][name]["Properties"]["SSESpecification"] == {"SSEEnabled": True}
    assert t["Resources"]["AppTable"]["Properties"]["TimeToLiveSpecification"]["AttributeName"] == "expiresAt"
    assert t["Resources"]["DocsBucket"]["Properties"]["BucketEncryption"]
