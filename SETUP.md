# Automatic profile updates

The profile card is committed with zero-value placeholders. Complete these steps once to enable private statistics and daily updates.

1. In GitHub, open **Settings > Developer settings > Personal access tokens > Fine-grained tokens**.
2. Create a read-only token owned by `makkaX2` with access to all personal repositories that should be counted.
3. Grant repository **Contents: Read-only** access. GitHub includes **Metadata: Read-only** automatically. Grant read-only account access for **Profile**, **Followers**, and **Starring** if those controls are shown.
4. Open the `makkaX2/makkaX2` repository, then **Settings > Secrets and variables > Actions > New repository secret**.
5. Name the secret `README_STATS_TOKEN` and paste the token there. Never commit or share the token.
6. Open **Actions > README build > Run workflow** and run it on `main`.

The workflow uses the read-only token only for GitHub GraphQL statistics. Commits are pushed separately with the repository-scoped `GITHUB_TOKEN`. Private repository names are not written to SVG, README, cache, or workflow logs. A token rotation causes one full rescan and then incremental caching resumes.

Private organization repositories are counted only when that organization allows the fine-grained token. GitHub may require an organization owner to approve it.
