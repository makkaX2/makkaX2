# Automatic profile updates

Public statistics and daily updates work automatically with the repository-scoped `GITHUB_TOKEN`. Complete these optional steps once to include all accessible private repositories as well.

1. In GitHub, open **Settings > Developer settings > Personal access tokens > Fine-grained tokens**.
2. Create a read-only token owned by `makkaX2` with access to all personal repositories that should be counted.
3. Grant repository **Contents: Read-only** access. GitHub includes **Metadata: Read-only** automatically; no Followers or Starring permission is needed.
4. Open the `makkaX2/makkaX2` repository, then **Settings > Secrets and variables > Actions > New repository secret**.
5. Name the secret `README_STATS_TOKEN` and paste the token there. Never commit or share the token.
6. Open **Actions > README build > Run workflow** and run it on `main`. When the secret is absent, the workflow safely falls back to `GITHUB_TOKEN` instead of leaving the card at zero-value placeholders.

The workflow uses the read-only token only for GitHub GraphQL statistics. Commits are pushed separately with the repository-scoped `GITHUB_TOKEN`. Private repository names are not written to SVG, README, cache, or workflow logs. A token rotation causes one full rescan and then incremental caching resumes.

To include selected private repositories owned by an organization, create a second read-only fine-grained token with that organization as its resource owner. Give it access only to the repositories that should be counted, grant **Contents: Read-only**, and save it as the repository secret `README_STATS_ORG_TOKEN`. Keep the personal `README_STATS_TOKEN` as well: the workflow merges both sources and removes duplicates.

The organization may require an owner to approve its token before it can read private repositories. The second token is never used for git pushes, and rotating either token invalidates only that source's HMAC-keyed cache entries.
