import structlog

from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient

logger = structlog.get_logger(__name__).bind(component="github_pr_service")


class GitHubPRService:
    """Opens or updates the pull request that changes `.context/`."""

    async def propose_context_update(
        self,
        repo_full_name: str,
        architecture_md: str,
        ledger_json: str,
        pat: str,
        branch_name: str = "checkcord/update-context",
        pr_title: str = "Update .context/ARCHITECTURE.md",
        pr_body: str | None = None,
        create_if_absent: bool = True,
    ) -> bool:
        """Create or update a Pull Request with updated .context/ files.

        With ``create_if_absent=False`` nothing is written unless the files already exist
        on the default branch or the Checkcord branch exists.
        """
        if not pat:
            logger.error("missing_github_token_cannot_propose_update", repo=repo_full_name)
            return False

        if "/" not in repo_full_name:
            logger.error("invalid_repo_name_format", repo=repo_full_name)
            return False

        owner, repo = repo_full_name.split("/", 1)
        client = GitHubRestClient(pat)

        repo_details = await client.get_repository(owner, repo)
        if not repo_details:
            logger.error("failed_to_retrieve_repository_details", repo=repo_full_name)
            return False
        default_branch = repo_details.get("default_branch", "main")

        default_branch_sha = await client.get_ref(owner, repo, f"heads/{default_branch}")
        if not default_branch_sha:
            logger.error(
                "failed_to_get_default_branch_sha",
                repo=repo_full_name,
                branch=default_branch,
            )
            return False

        existing_branch_sha = await client.get_ref(owner, repo, f"heads/{branch_name}")

        files_to_update = {
            ".context/ARCHITECTURE.md": architecture_md.encode("utf-8"),
            ".context/ledger.json": ledger_json.encode("utf-8"),
        }

        if not create_if_absent and not existing_branch_sha:
            for path in files_to_update:
                if await client.get_file_info(owner, repo, path, default_branch):
                    break
            else:
                logger.info("no_context_to_write_skipping", repo=repo_full_name)
                return True

        # Check if default branch (main) already has the desired content
        main_already_has_content = True
        for path, content in files_to_update.items():
            file_info = await client.get_file_info(owner, repo, path, default_branch)
            main_sha = file_info.get("sha") if file_info else None
            main_content = None
            if main_sha:
                main_content = await client.get_file_content(owner, repo, path, default_branch)
            if main_content != content:
                main_already_has_content = False
                break

        if main_already_has_content:
            logger.info(
                "default_branch_already_has_latest_context_skipping",
                repo=repo_full_name,
                branch=default_branch,
            )
            return True

        # Check if the existing PR branch already matches the desired content
        branch_already_up_to_date = False
        if existing_branch_sha:
            try:
                all_match = True
                for path, content in files_to_update.items():
                    file_info = await client.get_file_info(owner, repo, path, branch_name)
                    existing_sha = file_info.get("sha") if file_info else None
                    existing_content = None
                    if existing_sha:
                        existing_content = await client.get_file_content(
                            owner, repo, path, branch_name
                        )
                    if existing_content != content:
                        all_match = False
                        break
                if all_match:
                    branch_already_up_to_date = True
                    logger.info(
                        "branch_already_contains_matching_content_skipping_rebase",
                        repo=repo_full_name,
                        branch=branch_name,
                    )
            except Exception as e:
                logger.warning(
                    "failed_to_check_existing_branch_contents", error=str(e), repo=repo_full_name
                )
                branch_already_up_to_date = False

        if not branch_already_up_to_date:
            if not existing_branch_sha:
                logger.info(
                    "creating_new_branch_for_context_update",
                    repo=repo_full_name,
                    branch=branch_name,
                )
                created = await client.create_ref(
                    owner, repo, f"heads/{branch_name}", default_branch_sha
                )
                if not created:
                    logger.error("failed_to_create_branch", repo=repo_full_name, branch=branch_name)
                    return False
            else:
                logger.info(
                    "force_updating_existing_branch_to_default_branch_sha",
                    repo=repo_full_name,
                    branch=branch_name,
                    default_branch_sha=default_branch_sha,
                )
                updated = await client.update_ref(
                    owner, repo, f"heads/{branch_name}", default_branch_sha, force=True
                )
                if not updated:
                    logger.error("failed_to_rebase_branch", repo=repo_full_name, branch=branch_name)
                    return False

            for path, content in files_to_update.items():
                file_info = await client.get_file_info(owner, repo, path, default_branch)
                existing_sha = file_info.get("sha") if file_info else None
                existing_content = None
                if existing_sha:
                    existing_content = await client.get_file_content(
                        owner, repo, path, default_branch
                    )

                if existing_content == content:
                    logger.info(
                        "file_unchanged_relative_to_main_skipping_commit",
                        repo=repo_full_name,
                        path=path,
                    )
                    continue

                logger.info(
                    "committing_file_update", repo=repo_full_name, path=path, sha=existing_sha
                )
                commit_sha = await client.create_or_update_file(
                    owner=owner,
                    repo=repo,
                    path=path,
                    message=f"chore: update {path} from Checkcord",
                    content=content,
                    branch=branch_name,
                    sha=existing_sha,
                )
                if not commit_sha:
                    logger.error("failed_to_commit_file", repo=repo_full_name, path=path)
                    return False

        open_prs = await client.list_pull_requests(
            owner, repo, state="open", head=f"{owner}:{branch_name}"
        )
        matching_pr = None
        for pr in open_prs:
            if pr.get("head", {}).get("ref") == branch_name:
                matching_pr = pr
                break

        if matching_pr:
            logger.info(
                "pr_already_exists_skipping_creation",
                repo=repo_full_name,
                pr_number=matching_pr.get("number"),
                pr_url=matching_pr.get("html_url"),
            )
            return True

        logger.info("creating_new_pr_for_context_update", repo=repo_full_name)
        if pr_body is None:
            pr_body = (
                "`.context/ARCHITECTURE.md` is a file Checkcord generates from its decision "
                "records. It lists the records that everyone with access to this repository "
                "may read. Merging it is optional."
            )
        pr_details = await client.create_pull_request(
            owner=owner,
            repo=repo,
            title=pr_title,
            head=branch_name,
            base=default_branch,
            body=pr_body,
        )

        if not pr_details:
            logger.error("failed_to_create_pull_request", repo=repo_full_name)
            return False

        logger.info(
            "pr_created_successfully",
            repo=repo_full_name,
            pr_number=pr_details.get("number"),
            pr_url=pr_details.get("html_url"),
        )
        return True
