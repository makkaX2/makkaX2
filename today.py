from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import requests

PROFILE_USERNAME = "makkaX2"
GRAPHQL_URL = "https://api.github.com/graphql"
CACHE_VERSION = 1
REQUEST_TIMEOUT_SECONDS = 30
MAX_REQUEST_ATTEMPTS = 3

ROOT = Path(__file__).resolve().parent
README_PATH = ROOT / "README.md"
DARK_SVG_PATH = ROOT / "dark_mode.svg"
LIGHT_SVG_PATH = ROOT / "light_mode.svg"
CACHE_PATH = ROOT / "cache" / "stats.json"


class ProfileUpdateError(RuntimeError):
    """A deliberately sanitized error safe to print in a public Actions log."""


@dataclass(frozen=True)
class Repository:
    name_with_owner: str
    is_private: bool
    head_oid: str | None
    total_commits: int
    stars: int = 0

    @property
    def owner_and_name(self) -> tuple[str, str]:
        owner, name = self.name_with_owner.split("/", 1)
        return owner, name

    @classmethod
    def from_graphql(cls, node: dict[str, Any]) -> Repository:
        try:
            name_with_owner = node["nameWithOwner"]
            is_private = node["isPrivate"]
            stars = node["stargazers"]["totalCount"]
            default_branch = node.get("defaultBranchRef")
            if default_branch is None:
                head_oid = None
                total_commits = 0
            else:
                target = default_branch.get("target")
                if target is None:
                    head_oid = None
                    total_commits = 0
                else:
                    head_oid = target["oid"]
                    total_commits = target["history"]["totalCount"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileUpdateError("GitHub returned malformed repository data") from exc

        if not isinstance(name_with_owner, str) or "/" not in name_with_owner:
            raise ProfileUpdateError("GitHub returned an invalid repository identifier")
        if not isinstance(is_private, bool):
            raise ProfileUpdateError("GitHub returned an invalid repository visibility")
        if head_oid is not None and not isinstance(head_oid, str):
            raise ProfileUpdateError("GitHub returned an invalid repository revision")
        if not isinstance(total_commits, int) or total_commits < 0:
            raise ProfileUpdateError("GitHub returned an invalid commit count")
        if not isinstance(stars, int) or stars < 0:
            raise ProfileUpdateError("GitHub returned an invalid star count")

        return cls(name_with_owner, is_private, head_oid, total_commits, stars)


@dataclass(frozen=True)
class UserMetadata:
    node_id: str
    followers: int


@dataclass(frozen=True)
class ContributionTotals:
    commits: int = 0
    additions: int = 0
    deletions: int = 0

    def __add__(self, other: ContributionTotals) -> ContributionTotals:
        return ContributionTotals(
            commits=self.commits + other.commits,
            additions=self.additions + other.additions,
            deletions=self.deletions + other.deletions,
        )


@dataclass(frozen=True)
class ProfileStats:
    public_repositories: int
    private_repositories: int
    contributed_repositories: int
    stars: int
    commits: int
    followers: int
    additions: int
    deletions: int

    @property
    def lines_of_code(self) -> int:
        return self.additions - self.deletions


class ProfileClient(Protocol):
    def get_user_metadata(self, login: str) -> UserMetadata: ...

    def list_owned_repositories(self, login: str) -> list[Repository]: ...

    def list_contributed_repositories(self, login: str) -> list[Repository]: ...

    def scan_repository(self, repository: Repository, user_node_id: str) -> ContributionTotals: ...


class GitHubGraphQL:
    """Small GraphQL client whose errors never expose request variables or tokens."""

    USER_QUERY = """
    query ProfileUser($login: String!) {
      user(login: $login) {
        id
        followers { totalCount }
      }
    }
    """

    OWNED_REPOSITORIES_QUERY = """
    query OwnedRepositories($login: String!, $cursor: String) {
      user(login: $login) {
        repositories(
          first: 100
          after: $cursor
          ownerAffiliations: [OWNER]
          orderBy: {field: NAME, direction: ASC}
        ) {
          nodes {
            nameWithOwner
            isPrivate
            stargazers { totalCount }
            defaultBranchRef {
              target {
                ... on Commit {
                  oid
                  history { totalCount }
                }
              }
            }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """

    CONTRIBUTED_REPOSITORIES_QUERY = """
    query ContributedRepositories($login: String!, $cursor: String) {
      user(login: $login) {
        repositoriesContributedTo(
          first: 100
          after: $cursor
          includeUserRepositories: false
          contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]
          orderBy: {field: NAME, direction: ASC}
        ) {
          nodes {
            nameWithOwner
            isPrivate
            stargazers { totalCount }
            defaultBranchRef {
              target {
                ... on Commit {
                  oid
                  history { totalCount }
                }
              }
            }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """

    REPOSITORY_HISTORY_QUERY = """
    query RepositoryHistory($owner: String!, $name: String!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        defaultBranchRef {
          target {
            ... on Commit {
              oid
              history(first: 100, after: $cursor) {
                nodes {
                  additions
                  deletions
                  author { user { id } }
                }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
      }
    }
    """

    def __init__(self, token: str, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def execute(self, operation: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            try:
                response = self._session.post(
                    GRAPHQL_URL,
                    json={"query": query, "variables": variables},
                    headers=self._headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                if attempt + 1 == MAX_REQUEST_ATTEMPTS:
                    raise ProfileUpdateError(f"{operation} failed after network retries") from exc
                time.sleep(2**attempt)
                continue

            if response.status_code == 200:
                break
            if response.status_code in {429, 502, 503, 504} and attempt + 1 < MAX_REQUEST_ATTEMPTS:
                retry_after = response.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                time.sleep(min(delay, 15))
                continue
            raise ProfileUpdateError(f"{operation} failed with HTTP {response.status_code}")
        else:  # pragma: no cover - loop either breaks or raises
            raise ProfileUpdateError(f"{operation} failed")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProfileUpdateError(f"{operation} returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("errors"):
            raise ProfileUpdateError(f"{operation} returned GraphQL errors")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProfileUpdateError(f"{operation} returned no data")
        return data

    def get_user_metadata(self, login: str) -> UserMetadata:
        data = self.execute("ProfileUser", self.USER_QUERY, {"login": login})
        try:
            user = data["user"]
            node_id = user["id"]
            followers = user["followers"]["totalCount"]
        except (KeyError, TypeError) as exc:
            raise ProfileUpdateError("ProfileUser returned malformed data") from exc
        if not isinstance(node_id, str) or not isinstance(followers, int):
            raise ProfileUpdateError("ProfileUser returned invalid values")
        return UserMetadata(node_id=node_id, followers=followers)

    def _list_repositories(
        self,
        login: str,
        operation: str,
        query: str,
        connection_name: str,
    ) -> list[Repository]:
        cursor: str | None = None
        repositories: list[Repository] = []
        while True:
            data = self.execute(operation, query, {"login": login, "cursor": cursor})
            try:
                connection = data["user"][connection_name]
                nodes = connection["nodes"]
                page_info = connection["pageInfo"]
            except (KeyError, TypeError) as exc:
                raise ProfileUpdateError(f"{operation} returned malformed data") from exc
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise ProfileUpdateError(f"{operation} returned invalid pagination")
            repositories.extend(Repository.from_graphql(node) for node in nodes if node is not None)
            if not page_info.get("hasNextPage"):
                return repositories
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise ProfileUpdateError(f"{operation} returned an invalid cursor")

    def list_owned_repositories(self, login: str) -> list[Repository]:
        return self._list_repositories(
            login,
            "OwnedRepositories",
            self.OWNED_REPOSITORIES_QUERY,
            "repositories",
        )

    def list_contributed_repositories(self, login: str) -> list[Repository]:
        return self._list_repositories(
            login,
            "ContributedRepositories",
            self.CONTRIBUTED_REPOSITORIES_QUERY,
            "repositoriesContributedTo",
        )

    def scan_repository(self, repository: Repository, user_node_id: str) -> ContributionTotals:
        if repository.head_oid is None:
            return ContributionTotals()

        owner, name = repository.owner_and_name
        cursor: str | None = None
        totals = ContributionTotals()
        while True:
            data = self.execute(
                "RepositoryHistory",
                self.REPOSITORY_HISTORY_QUERY,
                {"owner": owner, "name": name, "cursor": cursor},
            )
            try:
                repository_data = data["repository"]
                default_branch = repository_data["defaultBranchRef"] if repository_data else None
                target = default_branch["target"] if default_branch else None
            except (KeyError, TypeError) as exc:
                raise ProfileUpdateError("RepositoryHistory returned malformed data") from exc
            if target is None:
                return ContributionTotals()
            if target.get("oid") != repository.head_oid:
                raise ProfileUpdateError("A repository changed while its history was being scanned")

            try:
                history = target["history"]
                nodes = history["nodes"]
                page_info = history["pageInfo"]
            except (KeyError, TypeError) as exc:
                raise ProfileUpdateError("RepositoryHistory returned invalid history data") from exc
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise ProfileUpdateError("RepositoryHistory returned invalid pagination")

            for node in nodes:
                if not isinstance(node, dict):
                    raise ProfileUpdateError("RepositoryHistory returned an invalid commit")
                author = node.get("author") or {}
                user = author.get("user") or {}
                if user.get("id") != user_node_id:
                    continue
                additions = node.get("additions")
                deletions = node.get("deletions")
                if not isinstance(additions, int) or not isinstance(deletions, int):
                    raise ProfileUpdateError("RepositoryHistory returned invalid line counts")
                totals += ContributionTotals(1, additions, deletions)

            if not page_info.get("hasNextPage"):
                return totals
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise ProfileUpdateError("RepositoryHistory returned an invalid cursor")


def _repo_key(token: str, name_with_owner: str) -> str:
    return hmac.new(
        token.encode("utf-8"),
        b"profile-readme:repository\0" + name_with_owner.casefold().encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _head_key(token: str, head_oid: str | None) -> str:
    value = head_oid or "empty"
    return hmac.new(
        token.encode("utf-8"),
        b"profile-readme:revision\0" + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def load_cache(path: Path = CACHE_PATH) -> dict[str, dict[str, int | str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return {}
    raw_repositories = payload.get("repositories")
    if not isinstance(raw_repositories, dict):
        return {}

    validated: dict[str, dict[str, int | str]] = {}
    for key, value in raw_repositories.items():
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            continue
        if not isinstance(value, dict):
            continue
        head = value.get("head")
        numeric = {field: value.get(field) for field in ("total_commits", "commits", "additions", "deletions")}
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{64}", head):
            continue
        if any(not isinstance(item, int) or item < 0 for item in numeric.values()):
            continue
        validated[key] = {"head": head, **numeric}
    return validated


def _deduplicate(repositories: list[Repository]) -> dict[str, Repository]:
    return {repository.name_with_owner.casefold(): repository for repository in repositories}


def collect_stats(
    client: ProfileClient,
    token: str,
    old_cache: dict[str, dict[str, int | str]],
) -> tuple[ProfileStats, dict[str, Any]]:
    metadata = client.get_user_metadata(PROFILE_USERNAME)
    owned = _deduplicate(client.list_owned_repositories(PROFILE_USERNAME))
    contributed = _deduplicate(client.list_contributed_repositories(PROFILE_USERNAME))
    for key in owned:
        contributed.pop(key, None)

    all_repositories = dict(owned)
    all_repositories.update(contributed)

    aggregate = ContributionTotals()
    new_entries: dict[str, dict[str, int | str]] = {}
    for repository in sorted(all_repositories.values(), key=lambda item: item.name_with_owner.casefold()):
        repository_key = _repo_key(token, repository.name_with_owner)
        revision_key = _head_key(token, repository.head_oid)
        cached = old_cache.get(repository_key)
        if (
            cached is not None
            and cached.get("head") == revision_key
            and cached.get("total_commits") == repository.total_commits
        ):
            contribution = ContributionTotals(
                commits=int(cached["commits"]),
                additions=int(cached["additions"]),
                deletions=int(cached["deletions"]),
            )
        else:
            contribution = client.scan_repository(repository, metadata.node_id)

        aggregate += contribution
        new_entries[repository_key] = {
            "head": revision_key,
            "total_commits": repository.total_commits,
            "commits": contribution.commits,
            "additions": contribution.additions,
            "deletions": contribution.deletions,
        }

    owned_values = list(owned.values())
    stats = ProfileStats(
        public_repositories=sum(not repository.is_private for repository in owned_values),
        private_repositories=sum(repository.is_private for repository in owned_values),
        contributed_repositories=len(contributed),
        stars=sum(repository.stars for repository in owned_values if not repository.is_private),
        commits=aggregate.commits,
        followers=metadata.followers,
        additions=aggregate.additions,
        deletions=aggregate.deletions,
    )
    cache_payload = {"version": CACHE_VERSION, "repositories": new_entries}
    return stats, cache_payload


ASCII_LOGO = (
    "",
    "         .=+++++=-:---:.",
    "       .**-.   .:==-:-=+*-",
    "      .##               :#+",
    "    =*+-.                -@:",
    "   *#.   -%-             .*#-",
    "  .@-     =%+              -%-",
    "   #*     -%*               #*",
    "    +#=  -%=    :+++++=    =%:",
    "     *%   .      ...... :+**.",
    "     :%+               -@=",
    "      .+*+----=-.   .:+#-",
    "         .----:=+++++=-",
    "",
)

CARD_WIDTH = 985
CARD_HEIGHT = 435
CONTENT_LEFT = 390
CONTENT_RIGHT = 960
MONOSPACE_ADVANCE = 9.2

THEMES = {
    "dark": {
        "background": "#161b22",
        "foreground": "#c9d1d9",
        "key": "#ffa657",
        "value": "#a5d6ff",
        "add": "#3fb950",
        "delete": "#f85149",
        "comment": "#616e7f",
    },
    "light": {
        "background": "#f6f8fa",
        "foreground": "#24292f",
        "key": "#953800",
        "value": "#0a3069",
        "add": "#1a7f37",
        "delete": "#cf222e",
        "comment": "#c2cfde",
    },
}


def _xml(value: object) -> str:
    return html.escape(str(value), quote=False)


def _number(value: int) -> str:
    return f"{value:,}"


def _simple_row(y: int, label: str, value: str) -> str:
    leader_start = CONTENT_LEFT + round((len(label) + 4) * MONOSPACE_ADVANCE)
    leader_end = CONTENT_RIGHT - round(len(value) * MONOSPACE_ADVANCE) - 9
    leader = ""
    if leader_end > leader_start + 8:
        leader = (
            f'<line x1="{leader_start}" y1="{y - 5}" x2="{leader_end}" y2="{y - 5}" '
            'class="leader"/>'
        )
    return (
        '<g class="data-row">'
        f'<text x="{CONTENT_LEFT}" y="{y}"><tspan class="cc">. </tspan>'
        f'<tspan class="key">{_xml(label)}</tspan><tspan>:</tspan></text>'
        f"{leader}"
        f'<text x="{CONTENT_RIGHT}" y="{y}" text-anchor="end" '
        f'class="value aligned-value">{_xml(value)}</text>'
        "</g>"
    )


def _section(y: int, label: str) -> str:
    rule_start = CONTENT_LEFT + round((len(label) + 3) * MONOSPACE_ADVANCE) + 7
    return (
        f'<text x="{CONTENT_LEFT}" y="{y}">- {_xml(label)}</text>'
        f'<line x1="{rule_start}" y1="{y - 5}" x2="{CONTENT_RIGHT}" y2="{y - 5}" class="rule"/>'
    )


def _stats_row(y: int, plain_text: str, markup: str) -> str:
    text_start = CONTENT_RIGHT - round(len(plain_text) * MONOSPACE_ADVANCE)
    leader_end = text_start - 9
    leader = ""
    if leader_end > CONTENT_LEFT + 28:
        leader = (
            f'<line x1="{CONTENT_LEFT + 19}" y1="{y - 5}" x2="{leader_end}" y2="{y - 5}" '
            'class="leader"/>'
        )
    return (
        f'<text x="{CONTENT_LEFT}" y="{y}" class="cc">. </text>'
        f"{leader}"
        f'<text x="{CONTENT_RIGHT}" y="{y}" text-anchor="end" class="aligned-stats">{markup}</text>'
    )


def render_svg(theme_name: str, stats: ProfileStats) -> str:
    try:
        theme = THEMES[theme_name]
    except KeyError as exc:
        raise ValueError(f"Unknown theme: {theme_name}") from exc

    logo_rows = "\n".join(
        f'<tspan x="44" y="{88 + index * 20}">{_xml(line)}</tspan>'
        for index, line in enumerate(ASCII_LOGO)
    )
    public_text = (
        f"Public: {_number(stats.public_repositories)} | "
        f"Private: {_number(stats.private_repositories)} | "
        f"Contributed: {_number(stats.contributed_repositories)}"
    )
    public_markup = (
        '<tspan class="key">Public</tspan>: '
        f'<tspan class="value">{_number(stats.public_repositories)}</tspan> | '
        '<tspan class="key">Private</tspan>: '
        f'<tspan class="value">{_number(stats.private_repositories)}</tspan> | '
        '<tspan class="key">Contributed</tspan>: '
        f'<tspan class="value">{_number(stats.contributed_repositories)}</tspan>'
    )
    activity_text = (
        f"Stars: {_number(stats.stars)} | Commits: {_number(stats.commits)} | "
        f"Followers: {_number(stats.followers)}"
    )
    activity_markup = (
        '<tspan class="key">Stars</tspan>: '
        f'<tspan class="value">{_number(stats.stars)}</tspan> | '
        '<tspan class="key">Commits</tspan>: '
        f'<tspan class="value">{_number(stats.commits)}</tspan> | '
        '<tspan class="key">Followers</tspan>: '
        f'<tspan class="value">{_number(stats.followers)}</tspan>'
    )
    loc_text = (
        f"Lines of Code: {_number(stats.lines_of_code)} "
        f"({_number(stats.additions)}++, {_number(stats.deletions)}--)"
    )
    loc_markup = (
        '<tspan class="key">Lines of Code</tspan>: '
        f'<tspan class="value">{_number(stats.lines_of_code)}</tspan> ('
        f'<tspan class="add">{_number(stats.additions)}++</tspan>, '
        f'<tspan class="delete">{_number(stats.deletions)}--</tspan>)'
    )
    info_rows = [
        (
            f'<text x="{CONTENT_LEFT}" y="27">makkaX2@mxka</text>'
            f'<line x1="506" y1="22" x2="{CONTENT_RIGHT}" y2="22" class="rule"/>'
        ),
        _simple_row(51, "OS", "Windows 11, Android 16, Arch Linux"),
        _simple_row(75, "Birthday", "June 1"),
        _simple_row(99, "IDE", "VS Code"),
        _simple_row(132, "Languages.Programming", "Python, Java, Kotlin, Swift"),
        _simple_row(156, "Languages.Computer", "HTML, CSS, LaTeX"),
        _simple_row(180, "Languages.Real", "Russian, Latvian"),
        _simple_row(213, "Hobby", "Telegram Bot Development"),
        _section(251, "Contact"),
        _simple_row(276, "Telegram", "@nufxa"),
        _simple_row(300, "Discord", "mxkaq7"),
        _section(342, "GitHub Stats"),
        _stats_row(367, public_text, public_markup),
        _stats_row(393, activity_text, activity_markup),
        _stats_row(419, loc_text, loc_markup),
    ]

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve" font-family="ConsolasFallback,Consolas,'Liberation Mono',monospace" width="{CARD_WIDTH}" height="{CARD_HEIGHT}" viewBox="0 0 {CARD_WIDTH} {CARD_HEIGHT}" font-size="16" role="img" aria-labelledby="title desc">
  <title id="title">makkaX2 GitHub profile</title>
  <desc id="desc">Terminal-style profile card with Codex ASCII art and automatically updated GitHub statistics.</desc>
  <style>
    @font-face {{
      src: local('Consolas'), local('Consolas Bold');
      font-family: 'ConsolasFallback';
      font-display: swap;
      size-adjust: 109%;
    }}
    .key {{ fill: {theme['key']}; }}
    .value {{ fill: {theme['value']}; }}
    .add {{ fill: {theme['add']}; }}
    .delete {{ fill: {theme['delete']}; }}
    .cc {{ fill: {theme['comment']}; }}
    .ascii {{ font-size: 15px; }}
    .leader {{ stroke: {theme['comment']}; stroke-width: 2; stroke-linecap: round; stroke-dasharray: 1 6; }}
    .rule {{ stroke: {theme['comment']}; stroke-width: 1; }}
    text, tspan {{ white-space: pre; }}
  </style>
  <rect width="{CARD_WIDTH}" height="{CARD_HEIGHT}" fill="{theme['background']}" rx="15"/>
  <text fill="{theme['foreground']}" class="ascii">
{logo_rows}
  </text>
  <g fill="{theme['foreground']}">
{chr(10).join(info_rows)}
  </g>
</svg>
"""


README_MARKER = re.compile(r"<!-- stats-updated: \d{4}-\d{2}-\d{2} -->")


def update_readme_marker(readme: str, updated_on: date) -> str:
    marker = f"<!-- stats-updated: {updated_on.isoformat()} -->"
    if not README_MARKER.search(readme):
        raise ProfileUpdateError("README update marker is missing")
    return README_MARKER.sub(marker, readme, count=1)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def run_update(
    client: ProfileClient,
    token: str,
    root: Path = ROOT,
    updated_on: date | None = None,
) -> ProfileStats:
    readme_path = root / "README.md"
    cache_path = root / "cache" / "stats.json"
    old_cache = load_cache(cache_path)
    stats, cache_payload = collect_stats(client, token, old_cache)

    if updated_on is None:
        updated_on = datetime.now(timezone.utc).date()
    try:
        current_readme = readme_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileUpdateError("README could not be read") from exc

    outputs = {
        root / "dark_mode.svg": render_svg("dark", stats),
        root / "light_mode.svg": render_svg("light", stats),
        cache_path: json.dumps(cache_payload, indent=2, sort_keys=True) + "\n",
        readme_path: update_readme_marker(current_readme, updated_on),
    }
    for path, content in outputs.items():
        _atomic_write(path, content)
    return stats


def main() -> int:
    token = os.environ.get("README_STATS_TOKEN", "").strip()
    if not token:
        print("README_STATS_TOKEN is required", file=sys.stderr)
        return 2

    try:
        stats = run_update(GitHubGraphQL(token), token)
    except ProfileUpdateError as exc:
        print(f"Profile update failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - never leak request state from an unexpected error.
        print(f"Profile update failed unexpectedly ({type(exc).__name__})", file=sys.stderr)
        return 1

    print(
        "Profile updated:",
        f"{stats.public_repositories} public repos,",
        f"{stats.private_repositories} private repos,",
        f"{stats.contributed_repositories} contributed repos,",
        f"{stats.commits} commits",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
