"""GitHub stand-in: the REST routes Checkcord calls and signed webhook deliveries."""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import hmac
import json
import time
import uuid
import zlib
from collections.abc import Coroutine
from typing import Any

import aiohttp
from aiohttp import web

from sim import company
from sim.world.state import Deliveries, GitCommit, GitRepo, PullRequest

PREFIX = "/github"
PAGE = 5


def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def _blob_sha(text: str) -> str:
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _not_found() -> web.Response:
    return web.json_response(
        {"message": "Not Found", "documentation_url": "https://docs.github.com/rest"}, status=404
    )


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def patch_for(before: str | None, after: str | None, path: str) -> tuple[str, int, int]:
    a = (before or "").splitlines(keepends=True)
    b = (after or "").splitlines(keepends=True)
    lines = list(difflib.unified_diff(a, b, fromfile=f"a/{path}", tofile=f"b/{path}", n=3))
    body = [ln for ln in lines if not ln.startswith(("---", "+++"))]
    adds = sum(1 for ln in body if ln.startswith("+"))
    dels = sum(1 for ln in body if ln.startswith("-"))
    return "".join(ln if ln.endswith("\n") else ln + "\n" for ln in body), adds, dels


class GitHub:
    def __init__(self, token: str, deliveries: Deliveries, checkcord_url: str) -> None:
        self.token = token
        self.deliveries = deliveries
        self.checkcord_url = checkcord_url.rstrip("/")
        self.repos: dict[str, GitRepo] = {}
        self._event_id = 9_000_000
        self._comment_id = 1_000
        self._tasks: set[asyncio.Task[Any]] = set()
        for r in company.REPOS:
            repo = GitRepo(r.full_name, r.private, "main", list(r.collaborators))
            sha = self._commit(repo, dict(r.files), None, "Initial import", "ingrid-vale")
            repo.refs["heads/main"] = sha
            self.repos[r.full_name] = repo

    def _later(self, job: Coroutine[Any, Any, Any]) -> None:
        """Send a webhook after the current response, as GitHub does."""

        async def run() -> None:
            await asyncio.sleep(0.5)
            await job

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _commit(
        self,
        repo: GitRepo,
        tree: dict[str, str],
        parent: str | None,
        message: str,
        author: str,
        at: float | None = None,
    ) -> str:
        seed = f"{repo.full_name}{parent}{message}{time.time_ns()}{len(repo.commits)}"
        sha = hashlib.sha1(seed.encode()).hexdigest()
        repo.commits[sha] = GitCommit(sha, tree, parent, message, author, at or time.time())
        return sha

    def routes(self, app: web.Application) -> None:
        r = app.router
        base = PREFIX + "/repos/{owner}/{name}"
        r.add_get(base, self.get_repo)
        r.add_get(base + "/events", self.list_events)
        r.add_get(base + "/collaborators", self.collaborators)
        r.add_get(base + "/pulls", self.list_pulls)
        r.add_post(base + "/pulls", self.create_pull)
        r.add_get(base + "/pulls/{number}", self.get_pull)
        r.add_get(base + "/pulls/{number}/files", self.pull_files)
        r.add_get(base + "/commits", self.list_commits)
        r.add_get(base + "/commits/{sha}", self.get_commit)
        r.add_get(base + "/commits/{sha}/pulls", self.commit_pulls)
        r.add_get(base + "/commits/{sha}/check-runs", self.check_runs)
        r.add_get(base + "/check-runs/{id}/annotations", self.annotations)
        r.add_get(base + "/actions/runs/{id}/jobs", self.run_jobs)
        r.add_get(base + "/actions/jobs/{id}/logs", self.job_logs)
        r.add_post(base + "/statuses/{sha}", self.create_status)
        r.add_get(base + "/issues/{number}/comments", self.list_comments)
        r.add_post(base + "/issues/{number}/comments", self.create_comment)
        r.add_patch(base + "/issues/comments/{id}", self.update_comment)
        r.add_get(base + "/git/ref/{ref:.+}", self.get_ref)
        r.add_post(base + "/git/refs", self.create_ref)
        r.add_patch(base + "/git/refs/{ref:.+}", self.update_ref)
        r.add_get(base + "/git/trees/{ref:.+}", self.get_tree)
        r.add_get(base + "/contents/{path:.*}", self.get_contents)
        r.add_put(base + "/contents/{path:.+}", self.put_contents)
        r.add_get(PREFIX + "/logs/{id}", self.raw_log)
        r.add_get(PREFIX + "/users/{login}", self.get_user)

    def _auth(self, request: web.Request) -> GitRepo | web.Response:
        auth = request.headers.get("Authorization", "")
        if auth not in (f"Bearer {self.token}", f"token {self.token}"):
            return web.json_response({"message": "Bad credentials"}, status=401)
        repo = self.repos.get(f"{request.match_info['owner']}/{request.match_info['name']}")
        return repo if repo is not None else _not_found()

    def _resolve(self, repo: GitRepo, ref: str) -> str | None:
        for candidate in (ref, f"heads/{ref}", ref.removeprefix("refs/")):
            if candidate in repo.refs:
                return repo.refs[candidate]
        return ref if ref in repo.commits else None

    def _repo_wire(self, repo: GitRepo) -> dict[str, Any]:
        owner, name = repo.full_name.split("/")
        return {
            "id": zlib.crc32(repo.full_name.encode()) % 10**8,
            "name": name,
            "full_name": repo.full_name,
            "private": repo.private,
            "visibility": "private" if repo.private else "public",
            "owner": {"login": owner, "type": "Organization"},
            "default_branch": repo.default_branch,
            "html_url": f"https://github.example/{repo.full_name}",
        }

    def _user(self, login: str) -> dict[str, Any]:
        return {"login": login, "id": zlib.crc32(login.encode()) % 10**7, "type": "User"}

    def _pull_wire(self, repo: GitRepo, pr: PullRequest) -> dict[str, Any]:
        return {
            "number": pr.number,
            "state": pr.state,
            "title": pr.title,
            "body": pr.body,
            "user": self._user(pr.author),
            "head": {"ref": pr.head_ref, "sha": pr.head_sha, "repo": self._repo_wire(repo)},
            "base": {"ref": pr.base_ref, "sha": pr.base_sha, "repo": self._repo_wire(repo)},
            "merged": pr.merged,
            "merge_commit_sha": pr.merge_commit_sha,
            "merged_at": _iso(pr.merged_at) if pr.merged_at else None,
            "created_at": _iso(pr.created_at),
            "html_url": f"https://github.example/{repo.full_name}/pull/{pr.number}",
        }

    def _paged(self, request: web.Request, items: list[Any]) -> web.Response:
        per_page = min(int(request.query.get("per_page", "30")), PAGE)
        page = max(int(request.query.get("page", "1")), 1)
        chunk = items[(page - 1) * per_page : page * per_page]
        headers = {}
        if page * per_page < len(items):
            headers["Link"] = f'<{request.url.update_query(page=page + 1)}>; rel="next"'
        return web.json_response(chunk, headers=headers)

    async def get_repo(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        return repo if isinstance(repo, web.Response) else web.json_response(self._repo_wire(repo))

    async def list_events(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        return self._paged(request, list(reversed(repo.events))[:300])

    async def get_user(self, request: web.Request) -> web.Response:
        login = request.match_info["login"]
        found = [p for p in company.PEOPLE if p.github_login == login]
        if not found:
            return _not_found()
        return web.json_response(
            {**self._user(login), "name": found[0].name, "email": found[0].github_email}
        )

    async def collaborators(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        rows = [{**self._user(c), "permissions": {"pull": True}} for c in repo.collaborators]
        return self._paged(request, rows)

    def _pr(self, repo: GitRepo, request: web.Request) -> PullRequest | None:
        return repo.pulls.get(int(request.match_info["number"]))

    async def list_pulls(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        state = request.query.get("state", "open")
        head = request.query.get("head")
        rows = [
            self._pull_wire(repo, p)
            for p in repo.pulls.values()
            if (state == "all" or p.state == state)
            and (head is None or head.split(":")[-1] == p.head_ref)
        ]
        return self._paged(request, rows)

    async def create_pull(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        body = await request.json()
        head_sha = self._resolve(repo, body["head"])
        base_sha = self._resolve(repo, body.get("base", repo.default_branch))
        if head_sha is None or base_sha is None:
            return web.json_response({"message": "Validation Failed"}, status=422)
        for p in repo.pulls.values():
            if p.head_ref == body["head"] and p.state == "open":
                return web.json_response(
                    {"message": "Validation Failed", "errors": [{"message": "exists"}]}, status=422
                )
        pr = PullRequest(
            repo.next_number,
            body.get("title", ""),
            body.get("body", ""),
            "checkcord[bot]",
            body["head"],
            body.get("base", repo.default_branch),
            head_sha,
            base_sha,
            by_checkcord=True,
        )
        repo.next_number += 1
        repo.pulls[pr.number] = pr
        self._later(self.send(repo.full_name, "pull_request", self._pr_payload(repo, pr, "opened")))
        return web.json_response(self._pull_wire(repo, pr), status=201)

    async def get_pull(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        pr = self._pr(repo, request)
        return web.json_response(self._pull_wire(repo, pr)) if pr else _not_found()

    def _diff(self, repo: GitRepo, base: str, head: str) -> list[dict[str, Any]]:
        a, b = repo.commits[base].tree, repo.commits[head].tree
        out = []
        for path in sorted(set(a) | set(b)):
            if a.get(path) == b.get(path):
                continue
            patch, adds, dels = patch_for(a.get(path), b.get(path), path)
            status = "added" if path not in a else "removed" if path not in b else "modified"
            out.append(
                {
                    "sha": _blob_sha(b.get(path, "")),
                    "filename": path,
                    "status": status,
                    "additions": adds,
                    "deletions": dels,
                    "changes": adds + dels,
                    "patch": patch,
                }
            )
        return out

    async def pull_files(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        pr = self._pr(repo, request)
        if pr is None:
            return _not_found()
        return self._paged(request, self._diff(repo, pr.base_sha, pr.head_sha))

    def _commit_wire(self, repo: GitRepo, c: GitCommit) -> dict[str, Any]:
        files = self._diff(repo, c.parent, c.sha) if c.parent else []
        adds = sum(f["additions"] for f in files)
        dels = sum(f["deletions"] for f in files)
        return {
            "sha": c.sha,
            "commit": {
                "message": c.message,
                "author": {"name": c.author, "date": _iso(c.at)},
                "committer": {"name": c.author, "date": _iso(c.at)},
            },
            "author": self._user(c.author),
            "parents": [{"sha": c.parent}] if c.parent else [],
            "stats": {"additions": adds, "deletions": dels, "total": adds + dels},
            "files": files,
        }

    async def list_commits(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        since = request.query.get("since")
        rows = sorted(repo.commits.values(), key=lambda c: c.at, reverse=True)
        if since:
            rows = [c for c in rows if _iso(c.at) >= since]
        return self._paged(request, [self._commit_wire(repo, c) for c in rows])

    async def get_commit(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        sha = self._resolve(repo, request.match_info["sha"])
        if sha is None:
            return web.json_response({"message": "No commit found"}, status=422)
        return web.json_response(self._commit_wire(repo, repo.commits[sha]))

    async def commit_pulls(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        sha = request.match_info["sha"]
        rows = [
            self._pull_wire(repo, p)
            for p in repo.pulls.values()
            if sha in (p.head_sha, p.merge_commit_sha)
        ]
        return web.json_response(rows)

    async def check_runs(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        return web.json_response({"total_count": 0, "check_runs": []})

    async def annotations(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        return repo if isinstance(repo, web.Response) else web.json_response([])

    async def run_jobs(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        return web.json_response({"total_count": 0, "jobs": []})

    async def job_logs(self, request: web.Request) -> web.StreamResponse:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        raise web.HTTPFound(f"{PREFIX}/logs/{request.match_info['id']}")

    async def raw_log(self, request: web.Request) -> web.Response:
        return web.Response(text="", content_type="text/plain")

    async def create_status(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        body = await request.json()
        row = {
            "id": len(repo.statuses) + 1,
            "sha": request.match_info["sha"],
            "state": body.get("state"),
            "context": body.get("context", "default"),
            "description": body.get("description"),
            "target_url": body.get("target_url"),
            "at": time.time(),
        }
        repo.statuses.append(row)
        return web.json_response(row, status=201)

    async def list_comments(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        n = int(request.match_info["number"])
        return self._paged(request, [c for c in repo.comments if c["issue"] == n])

    async def create_comment(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        body = await request.json()
        self._comment_id += 1
        row = {
            "id": self._comment_id,
            "issue": int(request.match_info["number"]),
            "body": body.get("body", ""),
            "user": self._user("checkcord[bot]"),
            "created_at": _iso(time.time()),
            "at": time.time(),
        }
        repo.comments.append(row)
        return web.json_response(row, status=201)

    async def update_comment(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        found = [c for c in repo.comments if c["id"] == int(request.match_info["id"])]
        if not found:
            return _not_found()
        body = await request.json()
        found[0].update(body=body.get("body", ""), updated_at=_iso(time.time()), at=time.time())
        return web.json_response(found[0])

    async def get_ref(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        ref = request.match_info["ref"]
        sha = repo.refs.get(ref)
        if sha is None:
            return _not_found()
        return web.json_response({"ref": f"refs/{ref}", "object": {"sha": sha, "type": "commit"}})

    async def create_ref(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        body = await request.json()
        ref = str(body["ref"]).removeprefix("refs/")
        if ref in repo.refs:
            return web.json_response({"message": "Reference already exists"}, status=422)
        if body["sha"] not in repo.commits:
            return web.json_response({"message": "Object does not exist"}, status=422)
        repo.refs[ref] = body["sha"]
        return web.json_response(
            {"ref": f"refs/{ref}", "object": {"sha": body["sha"], "type": "commit"}}, status=201
        )

    async def update_ref(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        body = await request.json()
        ref = request.match_info["ref"]
        if ref not in repo.refs or body["sha"] not in repo.commits:
            return web.json_response({"message": "Reference does not exist"}, status=422)
        repo.refs[ref] = body["sha"]
        return web.json_response({"ref": f"refs/{ref}", "object": {"sha": body["sha"]}})

    async def get_tree(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        sha = self._resolve(repo, request.match_info["ref"])
        if sha is None:
            return _not_found()
        tree = repo.commits[sha].tree
        rows: list[dict[str, Any]] = []
        dirs: set[str] = set()
        for path in sorted(tree):
            parts = path.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
            rows.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": _blob_sha(tree[path]),
                    "size": len(tree[path].encode()),
                }
            )
        rows += [{"path": d, "mode": "040000", "type": "tree", "sha": _blob_sha(d)} for d in dirs]
        return web.json_response({"sha": sha, "tree": rows, "truncated": False})

    async def get_contents(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        sha = self._resolve(repo, request.query.get("ref", repo.default_branch))
        if sha is None:
            return _not_found()
        tree = repo.commits[sha].tree
        path = request.match_info["path"].strip("/")
        if path in tree:
            text = tree[path]
            if "raw" in request.headers.get("Accept", ""):
                return web.Response(body=text.encode(), content_type="text/plain")
            return web.json_response(
                {
                    "type": "file",
                    "name": path.rsplit("/", 1)[-1],
                    "path": path,
                    "sha": _blob_sha(text),
                    "size": len(text.encode()),
                    "encoding": "base64",
                    "content": base64.b64encode(text.encode()).decode(),
                }
            )
        prefix = path + "/" if path else ""
        children = sorted({p[len(prefix) :].split("/")[0] for p in tree if p.startswith(prefix)})
        if not children:
            return _not_found()
        rows = []
        for child in children:
            full = prefix + child
            kind = "file" if full in tree else "dir"
            rows.append({"type": kind, "name": child, "path": full, "sha": _blob_sha(full)})
        return web.json_response(rows)

    async def put_contents(self, request: web.Request) -> web.Response:
        repo = self._auth(request)
        if isinstance(repo, web.Response):
            return repo
        body = await request.json()
        path = request.match_info["path"].strip("/")
        branch = body.get("branch", repo.default_branch)
        ref = f"heads/{branch}"
        head = repo.refs.get(ref)
        if head is None:
            return _not_found()
        tree = dict(repo.commits[head].tree)
        existed = path in tree
        if existed and body.get("sha") != _blob_sha(tree[path]):
            return web.json_response({"message": "sha does not match"}, status=409)
        text = base64.b64decode(body["content"]).decode()
        tree[path] = text
        sha = self._commit(repo, tree, head, body.get("message", ""), "checkcord[bot]")
        repo.refs[ref] = sha
        repo.content_writes.append(
            {"path": path, "branch": branch, "message": body.get("message"), "text": text}
        )
        for pr in repo.pulls.values():
            if pr.head_ref == branch and pr.state == "open":
                pr.head_sha = sha
                self._later(
                    self.send(
                        repo.full_name, "pull_request", self._pr_payload(repo, pr, "synchronize")
                    )
                )
        return web.json_response(
            {"content": {"path": path, "sha": _blob_sha(text)}, "commit": {"sha": sha}},
            status=200 if existed else 201,
        )

    def _event(self, repo: GitRepo, kind: str, actor: str, payload: dict[str, Any]) -> None:
        self._event_id += 1
        repo.events.append(
            {
                "id": str(self._event_id),
                "type": kind,
                "actor": self._user(actor),
                "repo": {"name": repo.full_name},
                "payload": payload,
                "public": not repo.private,
                "created_at": _iso(time.time()),
            }
        )

    async def send(
        self, repo_name: str, event: str, payload: dict[str, Any], forge: bool = False
    ) -> dict[str, Any]:
        repo = self.repos[repo_name]
        body = json.dumps(payload).encode()
        delivery = str(uuid.uuid4())
        status, text = 0, ""
        if repo.webhook_url and repo.webhook_secret:
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "GitHub-Hookshot/sim",
                "X-GitHub-Event": event,
                "X-GitHub-Delivery": delivery,
                "X-GitHub-Hook-ID": "1",
                "X-Hub-Signature-256": sign(
                    "forged-" + repo.webhook_secret[::-1] if forge else repo.webhook_secret, body
                ),
            }
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(repo.webhook_url, data=body, headers=headers) as resp:
                        status, text = resp.status, await resp.text()
            except aiohttp.ClientError as err:
                text = str(err)
        return self.deliveries.add(
            service="github",
            repo=repo_name,
            event=event,
            action=payload.get("action"),
            merged=(payload.get("pull_request") or {}).get("merged"),
            number=(payload.get("pull_request") or {}).get("number"),
            delivery=delivery,
            forged=forge,
            status=status,
            response=text[:500],
        )

    def _pr_payload(self, repo: GitRepo, pr: PullRequest, action: str) -> dict[str, Any]:
        return {
            "action": action,
            "number": pr.number,
            "pull_request": self._pull_wire(repo, pr),
            "repository": self._repo_wire(repo),
            "sender": self._user(pr.author),
        }

    async def open_pull(
        self,
        repo_name: str,
        author: str,
        title: str,
        body: str,
        changes: dict[str, str | None],
    ) -> dict[str, Any]:
        repo = self.repos[repo_name]
        base_sha = repo.refs["heads/main"]
        tree = dict(repo.commits[base_sha].tree)
        for path, text in changes.items():
            if text is None:
                tree.pop(path, None)
            else:
                tree[path] = text
        branch = f"{author}/change-{repo.next_number}"
        head = self._commit(repo, tree, base_sha, title, author)
        repo.refs[f"heads/{branch}"] = head
        pr = PullRequest(repo.next_number, title, body, author, branch, "main", head, base_sha)
        repo.next_number += 1
        repo.pulls[pr.number] = pr
        payload = self._pr_payload(repo, pr, "opened")
        self._event(repo, "PullRequestEvent", author, {"action": "opened", "number": pr.number})
        delivery = await self.send(repo_name, "pull_request", payload)
        return {"number": pr.number, "head_sha": head, "branch": branch, "delivery": delivery}

    async def synchronize(
        self, repo_name: str, number: int, changes: dict[str, str | None] | None = None
    ) -> dict[str, Any]:
        repo = self.repos[repo_name]
        pr = repo.pulls[number]
        tree = dict(repo.commits[pr.head_sha].tree)
        for path, text in (changes or {}).items():
            if text is None:
                tree.pop(path, None)
            else:
                tree[path] = text
        head = self._commit(repo, tree, pr.head_sha, f"Update {pr.title}", pr.author)
        pr.head_sha = head
        repo.refs[f"heads/{pr.head_ref}"] = head
        payload = self._pr_payload(repo, pr, "synchronize")
        delivery = await self.send(repo_name, "pull_request", payload)
        return {"number": number, "head_sha": head, "delivery": delivery}

    async def close(self, repo_name: str, number: int, merge: bool) -> dict[str, Any]:
        repo = self.repos[repo_name]
        pr = repo.pulls[number]
        pr.state = "closed"
        if merge:
            tree = dict(repo.commits[pr.head_sha].tree)
            sha = self._commit(repo, tree, repo.refs["heads/main"], f"Merge #{number}", pr.author)
            repo.refs["heads/main"] = sha
            pr.merged, pr.merge_commit_sha, pr.merged_at = True, sha, time.time()
        payload = self._pr_payload(repo, pr, "closed")
        delivery = await self.send(repo_name, "pull_request", payload)
        return {"number": number, "merged": pr.merged, "delivery": delivery}

    def received(self, repo_name: str) -> dict[str, Any]:
        repo = self.repos[repo_name]
        return {
            "statuses": repo.statuses,
            "comments": repo.comments,
            "pulls": [
                {**self._pull_wire(repo, p), "by_checkcord": p.by_checkcord}
                for p in repo.pulls.values()
            ],
            "refs": repo.refs,
            "content_writes": repo.content_writes,
        }

    def file_at(self, repo_name: str, ref: str, path: str) -> str | None:
        repo = self.repos[repo_name]
        sha = self._resolve(repo, ref)
        return repo.commits[sha].tree.get(path) if sha else None
