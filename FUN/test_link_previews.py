"""Compatibility regression: direct external previews only; no Render hosting."""
import httpx
import pytest
import main
import link_previews

@pytest.mark.parametrize("url",["https://telegra.ph/file/a.jpg","https://graph.org/file/a.jpg","https://files.catbox.moe/abc123.jpg"])
async def test_legacy_resolver_returns_supported_direct_url(url):
    assert await link_previews.resolve_url({"cover":{"url":url}})==url

async def test_legacy_render_link_is_not_selected_or_served():
    assert await link_previews.resolve_url({"cover":{"url":"https://example.onrender.com/previews/old"}}) is None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url="https://test") as client:
        assert (await client.get("/previews/"+"a"*48)).status_code==404
        assert (await client.get("/previews/"+"a"*48+".jpg")).status_code==404
