"""Session and storage-state profiles with expiry and credential management.

Allows saving, loading, and applying named authentication state profiles
(cookies, local/session storage) across HTTP requests and Playwright browser
contexts.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionProfile:
    """A named session/storage profile containing auth cookies and state."""

    name: str
    cookies: list[dict[str, Any]] = field(default_factory=list)
    origins: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: float | None = None) -> bool:
        """Check if the profile has expired."""
        if self.expires_at is None:
            return False
        current_time = now if now is not None else time.time()
        return current_time >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Convert profile to serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionProfile:
        """Create a profile from a dictionary representation."""
        return cls(
            name=data["name"],
            cookies=data.get("cookies", []),
            origins=data.get("origins", []),
            created_at=data.get("created_at", time.time()),
            expires_at=data.get("expires_at"),
            description=data.get("description", ""),
            metadata=data.get("metadata", {}),
        )


class ProfileManager:
    """Manages profile storage, retrieval, export, and expiry handling."""

    def __init__(self, storage_dir: str | Path | None = None) -> None:
        if storage_dir is None:
            base_dir = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
            storage_dir = Path(base_dir) / "freegem" / "profiles"
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _profile_path(self, name: str) -> Path:
        # Sanitize profile name
        safe_name = "".join(c for c in name if c.isalnum() or c in ("-", "_")).lower()
        if not safe_name:
            safe_name = "default"
        return self.storage_dir / f"{safe_name}.json"

    def save_profile(self, profile: SessionProfile) -> Path:
        """Save profile to storage directory."""
        path = self._profile_path(profile.name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile.to_dict(), f, indent=2)
        return path

    def get_profile(self, name: str, *, include_expired: bool = False) -> SessionProfile | None:
        """Load profile by name. Returns None if not found or if expired (unless include_expired=True)."""
        path = self._profile_path(name)
        if not path.exists():
            return None

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            profile = SessionProfile.from_dict(data)
            if not include_expired and profile.is_expired():
                return None
            return profile
        except Exception:
            return None

    def list_profiles(self) -> list[dict[str, Any]]:
        """List summary info for all saved profiles."""
        results: list[dict[str, Any]] = []
        now = time.time()
        for path in self.storage_dir.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                profile = SessionProfile.from_dict(data)
                results.append({
                    "name": profile.name,
                    "created_at": profile.created_at,
                    "expires_at": profile.expires_at,
                    "is_expired": profile.is_expired(now),
                    "description": profile.description,
                    "cookie_count": len(profile.cookies),
                })
            except Exception:
                continue
        return results

    def delete_profile(self, name: str) -> bool:
        """Delete a profile by name. Returns True if deleted."""
        path = self._profile_path(name)
        if path.exists():
            path.unlink()
            return True
        return False

    def export_playwright_storage_state(self, name: str) -> dict[str, Any] | None:
        """Export profile as a Playwright-compatible storage_state dictionary."""
        profile = self.get_profile(name)
        if not profile:
            return None
        return {
            "cookies": profile.cookies,
            "origins": profile.origins,
        }

    def export_cookie_jar(self, name: str) -> http.cookiejar.CookieJar | None:
        """Export profile cookies to Python's http.cookiejar.CookieJar."""
        profile = self.get_profile(name)
        if not profile:
            return None

        jar = http.cookiejar.CookieJar()
        for c in profile.cookies:
            try:
                cookie = http.cookiejar.Cookie(
                    version=0,
                    name=c.get("name", ""),
                    value=c.get("value", ""),
                    port=None,
                    port_specified=False,
                    domain=c.get("domain", ""),
                    domain_specified=bool(c.get("domain")),
                    domain_initial_dot=c.get("domain", "").startswith("."),
                    path=c.get("path", "/"),
                    path_specified=bool(c.get("path")),
                    secure=bool(c.get("secure", False)),
                    expires=c.get("expires"),
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                )
                jar.set_cookie(cookie)
            except Exception:
                continue
        return jar
