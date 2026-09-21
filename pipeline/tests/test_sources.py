"""Third-party discovery channels."""

import httpx
import pytest

from airadar.sources.ecosystems import EcosystemsClient, discover_via_dependencies, repo_from_url
from airadar.sources.huggingface import HuggingFaceClient, _links_from_item


async def _no_sleep(_seconds):
    return None


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/langchain-ai/langchain", "langchain-ai/langchain"),
        ("http://github.com/acme/thing.git", "acme/thing"),
        ("https://www.github.com/acme/thing/", "acme/thing"),
        ("https://gitlab.com/acme/thing", None),
        ("https://github.com/acme/thing/tree/main", None),  # deep links are not repo roots
        ("", None),
        (None, None),
    ],
)
def test_repo_from_url(url, expected):
    assert repo_from_url(url) == expected


async def test_dependent_repos_paginates_and_dedupes():
    pages = {
        1: [{"repository_url": f"https://github.com/o/r{i}"} for i in range(100)],
        2: [
            {"repository_url": "https://github.com/o/r0"},  # duplicate across pages
            {"repository_url": "https://gitlab.com/o/elsewhere"},  # not GitHub
            {"repository_url": None},  # no repo at all
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json=pages.get(page, []))

    async with EcosystemsClient(transport=httpx.MockTransport(handler), sleep=_no_sleep) as client:
        found = await client.dependent_repos("pypi", "torch")

    assert len(found) == 100
    assert "o/r0" in found
    assert client.stats.requests == 2  # short second page ends pagination


async def test_ecosystems_survives_an_unavailable_service():
    """A free community API going down must not take the discovery run with it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    async with EcosystemsClient(transport=httpx.MockTransport(handler), sleep=_no_sleep) as client:
        found, stats = await discover_via_dependencies(packages={"pypi": ("torch",)}, client=client)

    assert found == {}
    assert stats.errors == 1


async def test_unknown_ecosystem_is_a_programming_error():
    async with EcosystemsClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])),
        sleep=_no_sleep,
    ) as client:
        with pytest.raises(ValueError, match="unknown ecosystem"):
            await client.dependent_repos("cargo", "candle")


def test_huggingface_links_are_read_from_card_metadata():
    item = {
        "id": "meta/llama",
        "cardData": {
            "repository": "https://github.com/meta-llama/llama",
            "homepage": "https://ai.meta.com",
        },
    }
    assert _links_from_item(item) == {"meta-llama/llama"}


def test_huggingface_item_without_links_yields_nothing():
    assert _links_from_item({"id": "x/y", "cardData": {}}) == set()
    assert _links_from_item({"id": "x/y"}) == set()


async def test_huggingface_paginates_models_and_spaces():
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        skip = int(request.url.params.get("skip", 0))
        if skip > 0:
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[{"cardData": {"repository": "https://github.com/acme/model"}}],
        )

    async with HuggingFaceClient(transport=httpx.MockTransport(handler), sleep=_no_sleep) as client:
        found = await client.linked_repos("models", max_pages=3)

    assert found == {"acme/model"}
    assert seen_paths == ["/api/models"]  # short page stops pagination


async def test_huggingface_rejects_unknown_kind():
    async with HuggingFaceClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])),
        sleep=_no_sleep,
    ) as client:
        with pytest.raises(ValueError, match="unknown Hub kind"):
            await client.linked_repos("datasets")


async def test_which_package_produced_a_sighting_is_kept():
    """It used to be dropped here, and every one of the 29 bellwethers collapsed
    into the single string "ecosystems" as the discovery source. The package is
    the whole value of this channel for classification: a repository that
    imports `torch` is a machine-learning project whatever its description
    says, and in whatever language it does not say it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") != "1":
            return httpx.Response(200, json=[])
        package = request.url.path.rsplit("/", 2)[-2]
        return httpx.Response(
            200,
            json=[
                {"repository_url": "https://github.com/acme/both"},
                {"repository_url": f"https://github.com/acme/only-{package}"},
            ],
        )

    async with EcosystemsClient(transport=httpx.MockTransport(handler), sleep=_no_sleep) as client:
        found, _ = await discover_via_dependencies(
            packages={"pypi": ("torch", "transformers")}, client=client
        )

    assert found["acme/both"] == {("pypi", "torch"), ("pypi", "transformers")}
    assert found["acme/only-torch"] == {("pypi", "torch")}
