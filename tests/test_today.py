from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date

import pytest

import today


def repository_node(
    name: str,
    *,
    private: bool = False,
    head: str | None = "head",
    commits: int = 1,
    language: str | None = "Python",
) -> dict:
    default_branch = None
    if head is not None:
        default_branch = {"target": {"oid": head, "history": {"totalCount": commits}}}
    return {
        "nameWithOwner": name,
        "isPrivate": private,
        "primaryLanguage": None if language is None else {"name": language},
        "defaultBranchRef": default_branch,
    }


class PaginatedGraphQL(today.GitHubGraphQL):
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def execute(self, operation: str, query: str, variables: dict) -> dict:
        self.calls.append(variables)
        return self.pages.pop(0)


def test_owned_repositories_are_paginated() -> None:
    client = PaginatedGraphQL(
        [
            {
                "user": {
                    "repositories": {
                        "nodes": [repository_node("makkaX2/one")],
                        "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                    }
                }
            },
            {
                "user": {
                    "repositories": {
                        "nodes": [repository_node("makkaX2/two", private=True)],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        ]
    )

    repositories = client.list_owned_repositories(today.PROFILE_USERNAME)

    assert [repository.name_with_owner for repository in repositories] == ["makkaX2/one", "makkaX2/two"]
    assert client.calls == [
        {"login": "makkaX2", "cursor": None},
        {"login": "makkaX2", "cursor": "next"},
    ]


def test_organization_private_repositories_are_paginated() -> None:
    client = PaginatedGraphQL(
        [
            {
                "organization": {
                    "repositories": {
                        "nodes": [repository_node("example-org/one", private=True)],
                        "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                    }
                }
            },
            {
                "organization": {
                    "repositories": {
                        "nodes": [repository_node("example-org/two", private=True)],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        ]
    )

    repositories = client.list_accessible_private_repositories("example-org")

    assert [repository.name_with_owner for repository in repositories] == [
        "example-org/one",
        "example-org/two",
    ]
    assert client.calls == [
        {"owner": "example-org", "cursor": None},
        {"owner": "example-org", "cursor": "next"},
    ]


def test_repository_scan_ignores_unlinked_and_other_authors() -> None:
    repository = today.Repository("makkaX2/project", False, "same-head", 4)
    client = PaginatedGraphQL(
        [
            {
                "repository": {
                    "defaultBranchRef": {
                        "target": {
                            "oid": "same-head",
                            "history": {
                                "nodes": [
                                    {"additions": 10, "deletions": 2, "author": {"user": {"id": "me"}}},
                                    {"additions": 50, "deletions": 3, "author": {"user": None}},
                                ],
                                "pageInfo": {"hasNextPage": True, "endCursor": "page-2"},
                            },
                        }
                    }
                }
            },
            {
                "repository": {
                    "defaultBranchRef": {
                        "target": {
                            "oid": "same-head",
                            "history": {
                                "nodes": [
                                    {"additions": 8, "deletions": 1, "author": {"user": {"id": "other"}}},
                                    {"additions": 7, "deletions": 4, "author": {"user": {"id": "me"}}},
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            },
                        }
                    }
                }
            },
        ]
    )

    assert client.scan_repository(repository, "me") == today.ContributionTotals(2, 17, 6)


def test_empty_repository_does_not_request_commit_history() -> None:
    client = PaginatedGraphQL([])
    repository = today.Repository("makkaX2/empty", False, None, 0)

    assert client.scan_repository(repository, "me") == today.ContributionTotals()
    assert client.calls == []


class FakeProfileClient:
    def __init__(
        self,
        owned: list[today.Repository],
        contributed: list[today.Repository],
        contributions: dict[str, today.ContributionTotals],
    ) -> None:
        self.owned = owned
        self.contributed = contributed
        self.contributions = contributions
        self.scanned: list[str] = []

    def get_user_metadata(self, login: str) -> today.UserMetadata:
        assert login == "makkaX2"
        return today.UserMetadata("user-node")

    def list_owned_repositories(self, login: str) -> list[today.Repository]:
        return self.owned

    def list_contributed_repositories(self, login: str) -> list[today.Repository]:
        return self.contributed

    def scan_repository(self, repository: today.Repository, user_node_id: str) -> today.ContributionTotals:
        assert user_node_id == "user-node"
        self.scanned.append(repository.name_with_owner)
        return self.contributions[repository.name_with_owner]


class FakeManagedClient:
    def __init__(
        self,
        repositories: list[today.Repository],
        contributions: dict[str, today.ContributionTotals],
    ) -> None:
        self.repositories = repositories
        self.contributions = contributions
        self.scanned: list[str] = []

    def list_accessible_private_repositories(self, owner: str) -> list[today.Repository]:
        assert owner == "example-org"
        return self.repositories

    def scan_repository(self, repository: today.Repository, user_node_id: str) -> today.ContributionTotals:
        assert user_node_id == "user-node"
        self.scanned.append(repository.name_with_owner)
        return self.contributions[repository.name_with_owner]


def make_profile_client(private_head: str = "private-head") -> FakeProfileClient:
    owned = [
        today.Repository("makkaX2/public", False, "public-head", 2, "Python"),
        today.Repository("makkaX2/private", True, private_head, 1, "Java"),
    ]
    contributed = [today.Repository("someone/shared", False, "shared-head", 3, "Kotlin")]
    contributions = {
        "makkaX2/public": today.ContributionTotals(2, 20, 4),
        "makkaX2/private": today.ContributionTotals(1, 10, 2),
        "someone/shared": today.ContributionTotals(3, 30, 6),
    }
    return FakeProfileClient(owned, contributed, contributions)


def test_stats_split_private_data_and_reuse_safe_cache() -> None:
    token = "secret-token-value"
    first_client = make_profile_client()

    stats, cache = today.collect_stats(first_client, token, {})

    assert stats == today.ProfileStats(1, 1, 1, 6, 3, 3, 60, 12)
    assert stats.lines_of_code == 48
    assert sorted(first_client.scanned) == ["makkaX2/private", "makkaX2/public", "someone/shared"]
    serialized = json.dumps(cache)
    assert token not in serialized
    assert "makkaX2/private" not in serialized
    assert "someone/shared" not in serialized

    second_client = make_profile_client()
    repeated_stats, repeated_cache = today.collect_stats(second_client, token, cache["repositories"])
    assert repeated_stats == stats
    assert repeated_cache == cache
    assert second_client.scanned == []

    changed_client = make_profile_client(private_head="new-private-head")
    changed_client.owned[1] = replace(changed_client.owned[1], total_commits=2)
    today.collect_stats(changed_client, token, cache["repositories"])
    assert changed_client.scanned == ["makkaX2/private"]

    rotated_token_client = make_profile_client()
    today.collect_stats(rotated_token_client, "rotated-token", cache["repositories"])
    assert sorted(rotated_token_client.scanned) == [
        "makkaX2/private",
        "makkaX2/public",
        "someone/shared",
    ]


def test_additional_private_source_is_merged_and_cached_independently() -> None:
    personal_token = "personal-token-value"
    organization_token = "organization-token-value"
    organization_repository = today.Repository(
        "example-org/managed",
        True,
        "organization-head",
        659,
        "Python",
    )
    organization_totals = today.ContributionTotals(492, 367_981, 145_559)
    personal_client = make_profile_client()
    organization_client = FakeManagedClient(
        [organization_repository],
        {organization_repository.name_with_owner: organization_totals},
    )

    stats, cache = today.collect_stats(
        personal_client,
        personal_token,
        {},
        [(organization_client, organization_token, "example-org")],
    )

    assert stats == today.ProfileStats(1, 2, 1, 498, 4, 3, 368_041, 145_571)
    assert organization_client.scanned == ["example-org/managed"]
    serialized = json.dumps(cache)
    for secret in (personal_token, organization_token, "example-org/managed"):
        assert secret not in serialized

    repeated_personal = make_profile_client()
    repeated_organization = FakeManagedClient(
        [organization_repository],
        {organization_repository.name_with_owner: organization_totals},
    )
    repeated_stats, repeated_cache = today.collect_stats(
        repeated_personal,
        personal_token,
        cache["repositories"],
        [(repeated_organization, organization_token, "example-org")],
    )
    assert repeated_stats == stats
    assert repeated_cache == cache
    assert repeated_personal.scanned == []
    assert repeated_organization.scanned == []

    rotated_organization = FakeManagedClient(
        [organization_repository],
        {organization_repository.name_with_owner: organization_totals},
    )
    today.collect_stats(
        make_profile_client(),
        personal_token,
        cache["repositories"],
        [(rotated_organization, "rotated-organization-token", "example-org")],
    )
    assert rotated_organization.scanned == ["example-org/managed"]


def test_empty_additional_source_does_not_publish_partial_stats() -> None:
    with pytest.raises(today.ProfileUpdateError, match="no private repositories"):
        today.collect_stats(
            make_profile_client(),
            "personal-token",
            {},
            [(FakeManagedClient([], {}), "organization-token", "example-org")],
        )


def test_rendered_svg_is_valid_and_contains_only_requested_profile_fields() -> None:
    stats = today.ProfileStats(4, 2, 3, 25, 6, 5, 1000, 250)
    for theme in ("dark", "light"):
        svg = today.render_svg(theme, stats)
        root = ET.fromstring(svg)
        assert root.attrib["width"] == "985"
        assert root.attrib["height"] == "435"
        text = "".join(root.itertext())
        for expected in (
            "makkaX2@mxka",
            "Windows 11, Android 16, Arch Linux",
            "Birthday",
            "June 1",
            "Python, Java, Kotlin, Swift",
            "HTML, CSS, LaTeX",
            "Russian, Latvian",
            "Telegram Bot Development",
            "@nufxa",
            "mxkaq7",
            "Public",
            "Private",
            "Contributed",
            "Commits",
            "Active Repos",
            "Languages",
        ):
            assert expected in text
        for forbidden in ("Host", "Kernel", "Email", "LinkedIn", "Hardware", "Stars", "Followers"):
            assert forbidden not in text
        assert ".-+*****+*##*++++*##=." in text
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        aligned_values = root.findall(".//svg:text[@class='value aligned-value']", namespace)
        assert len(aligned_values) == 9
        assert all(value.attrib["x"] == "960" for value in aligned_values)
        assert all(value.attrib["text-anchor"] == "end" for value in aligned_values)
        aligned_stats = root.findall(".//svg:text[@class='aligned-stats']", namespace)
        assert len(aligned_stats) == 3
        assert all(value.attrib["x"] == "960" for value in aligned_stats)

    assert len(today.ASCII_LOGO) == 19
    assert max(map(len, today.ASCII_LOGO)) <= 44


def test_api_failure_does_not_replace_existing_outputs(tmp_path) -> None:
    (tmp_path / "cache").mkdir()
    (tmp_path / "README.md").write_text("profile\n<!-- stats-updated: 2026-08-10 -->\n", encoding="utf-8")
    (tmp_path / "dark_mode.svg").write_text("old-dark", encoding="utf-8")
    (tmp_path / "light_mode.svg").write_text("old-light", encoding="utf-8")

    class FailingClient:
        def get_user_metadata(self, login: str) -> today.UserMetadata:
            raise today.ProfileUpdateError("safe failure")

    with pytest.raises(today.ProfileUpdateError, match="safe failure"):
        today.run_update(FailingClient(), "token", tmp_path, date(2026, 8, 11))

    assert (tmp_path / "dark_mode.svg").read_text(encoding="utf-8") == "old-dark"
    assert (tmp_path / "light_mode.svg").read_text(encoding="utf-8") == "old-light"
    assert "2026-08-10" in (tmp_path / "README.md").read_text(encoding="utf-8")
    assert not (tmp_path / "cache" / "stats.json").exists()


def test_run_update_writes_all_outputs_after_success(tmp_path) -> None:
    (tmp_path / "cache").mkdir()
    (tmp_path / "README.md").write_text("profile\n<!-- stats-updated: 2026-08-10 -->\n", encoding="utf-8")
    client = make_profile_client()

    stats = today.run_update(client, "token", tmp_path, date(2026, 8, 11))

    assert stats.private_repositories == 1
    assert "2026-08-11" in (tmp_path / "README.md").read_text(encoding="utf-8")
    ET.parse(tmp_path / "dark_mode.svg")
    ET.parse(tmp_path / "light_mode.svg")
    cache_text = (tmp_path / "cache" / "stats.json").read_text(encoding="utf-8")
    assert "makkaX2/private" not in cache_text
    assert json.loads(cache_text)["version"] == today.CACHE_VERSION
