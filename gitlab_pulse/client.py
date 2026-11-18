"""GitLab client setup, retries, and top-level project listing."""

import os
import time

import gitlab
import requests

RETRYABLE_EXCEPTIONS = (gitlab.exceptions.GitlabError, requests.exceptions.RequestException)

DEFAULT_GROUP = os.environ.get("GITLAB_GROUP", "audacy-inc")


def get_gitlab_client() -> gitlab.Gitlab:
    """Build an authenticated python-gitlab client from env vars.

    Requires GITLAB_TOKEN. GITLAB_URL defaults to https://gitlab.com.
    """
    print("Connecting to GitLab...")
    url = os.environ.get("GITLAB_URL", "https://gitlab.com")
    token = os.environ.get("GITLAB_TOKEN")

    if not token:
        raise RuntimeError(
            "GITLAB_TOKEN is not set. Add it to your .env file "
            "(see .env.example)."
        )

    return gitlab.Gitlab(url=url, private_token=token)


def test_connection() -> bool:
    """Authenticate against GitLab and print the resulting user identity."""
    print("Testing GitLab connection...")
    gl = get_gitlab_client()

    try:
        gl.auth()
    except gitlab.exceptions.GitlabAuthenticationError as exc:
        print(f"Authentication failed: {exc}")
        return False
    except gitlab.exceptions.GitlabError as exc:
        print(f"Could not reach GitLab: {exc}")
        return False

    user = gl.user
    print("Connected to GitLab successfully.")
    print(f"  URL:      {gl.url}")
    print(f"  User:     {user.username} ({user.name})")
    print(f"  User ID:  {user.id}")
    return True


def _call_with_retries(fn, *args, retries=3, backoff=2.0, **kwargs):
    """Retry a GitLab API call on transient network/API errors with backoff."""
    last_exc = None
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_exc


def get_group(gl: gitlab.Gitlab, group_path: str = DEFAULT_GROUP):
    """Fetch the group object once, shared by project listing and group-level runners."""
    return _call_with_retries(gl.groups.get, group_path)


def list_group_projects(gl: gitlab.Gitlab, group, archived: bool = False):
    """Return all active (non-archived) projects under a group, including subgroups."""
    print(f"Listing projects under group '{group.full_path}' (archived={archived})...")
    return _call_with_retries(
        group.projects.list, all=True, include_subgroups=True, archived=archived
    )
