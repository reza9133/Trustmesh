"""
Guards against the exact failure mode the embedding approach
introduces: someone edits contracts/trustmesh_escrow.py, forgets to
run scripts/sync_escrow_source.py, and TrustMeshRegistry.create_gig()
silently keeps deploying a stale escrow version forever.

This test needs nothing but the standard library and pytest - no
`genlayer` SDK, no `gltest`, no running Studio instance - so it runs
fast as part of any ordinary `pytest` invocation and can gate CI before
anything touches a network.

    pytest tests/test_embedded_source_in_sync.py -v
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ESCROW_PATH = PROJECT_ROOT / "contracts" / "trustmesh_escrow.py"
REGISTRY_PATH = PROJECT_ROOT / "contracts" / "trustmesh_registry.py"

BEGIN_MARKER = "# --- BEGIN embedded child-contract source (auto-generated) -----------\n"
END_MARKER = "'''\n# --- END embedded child-contract source"


def _extract_embedded_escrow_source(registry_text: str) -> str:
    begin_at = registry_text.index(BEGIN_MARKER)
    quote_open = registry_text.index("r'''", begin_at) + len("r'''")
    quote_close = registry_text.index(END_MARKER, quote_open)
    return registry_text[quote_open:quote_close]


def test_embedded_escrow_source_matches_real_file():
    registry_text = REGISTRY_PATH.read_text(encoding="utf-8")
    embedded_source = _extract_embedded_escrow_source(registry_text)
    actual_source = ESCROW_PATH.read_text(encoding="utf-8")

    assert embedded_source == actual_source, (
        "The _TRUSTMESH_ESCROW_SOURCE constant embedded in "
        "trustmesh_registry.py no longer matches contracts/"
        "trustmesh_escrow.py. TrustMeshRegistry.create_gig() would "
        "deploy a stale/incorrect TrustMeshEscrow. Fix this by running:\n"
        "\n"
        "    python3 scripts/sync_escrow_source.py\n"
    )


def test_embedded_escrow_source_starts_with_genvm_version_comment():
    # gl.deploy_contract requires the version comment to be the literal
    # first line of the code it deploys.
    registry_text = REGISTRY_PATH.read_text(encoding="utf-8")
    embedded_source = _extract_embedded_escrow_source(registry_text)
    assert embedded_source.startswith('# { "Depends"')
