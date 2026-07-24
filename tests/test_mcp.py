from __future__ import annotations

import base64
from pathlib import Path

import anyio
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import BlobResourceContents
from pydantic import AnyUrl

from fiscus.mcp_server import _issue_reporting_instruction, create_server
from fiscus.service import FiscusService


def test_mcp_issue_reporting_instruction_resolves_live_state_file() -> None:
    instruction = _issue_reporting_instruction()
    expected = Path(__file__).parents[1] / "do" / "state.md"

    if expected.is_file():
        assert str(expected) in instruction
        assert "before ending the turn" in instruction
    else:
        assert "when a Fiscus checkout is available" in instruction
    assert "missing capability" in instruction


def test_mcp_issue_reporting_instruction_has_public_checkout_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(Path, "is_file", lambda _: False)

    instruction = _issue_reporting_instruction()

    assert "when a Fiscus checkout is available" in instruction
    assert "missing capability" in instruction


def test_mcp_lists_core_tools_and_serves_artifact_resource(tmp_path: Path) -> None:
    async def exercise() -> None:
        with FiscusService(data_dir=tmp_path) as service:
            binary = b"\x00fiscus\xffartifact"
            stored = service.cache.store(
                "test.binary",
                {},
                "release:1",
                binary,
                {
                    "source_url": "https://example.test/artifact.bin",
                    "retrieved_at_utc": "2026-07-21T00:00:00+00:00",
                    "source_metadata": {},
                },
                data_file="artifact.bin",
            )
            server = create_server(service)
            async with create_connected_server_and_client_session(server) as client:
                listed = await client.list_tools()
                assert {tool.name for tool in listed.tools} == {
                    "find_series",
                    "get_series",
                    "fiscaldata_query",
                    "source_caveats",
                    "get_file",
                    "z1_series",
                    "soma_holdings",
                    "provenance",
                }
                get_series_tool = next(tool for tool in listed.tools if tool.name == "get_series")
                assert "vintage_as_of" in get_series_tool.inputSchema["properties"]
                result = await client.call_tool(
                    "find_series",
                    {"query": "TGA Wednesday level", "limit": 2},
                )
                assert result.structuredContent is not None
                assert result.structuredContent["results"][0]["id"] == "fred.wdtgal"

                templates = await client.list_resource_templates()
                assert {str(template.uriTemplate) for template in templates.resourceTemplates} == {
                    "fiscus-cache://artifact/{cache_key}"
                }
                resource = await client.read_resource(
                    AnyUrl("fiscus-cache://artifact/" + stored.descriptor.cache_key[:20])
                )
                assert len(resource.contents) == 1
                content = resource.contents[0]
                assert isinstance(content, BlobResourceContents)
                assert content.mimeType == "application/octet-stream"
                assert base64.b64decode(content.blob) == binary

    anyio.run(exercise)
