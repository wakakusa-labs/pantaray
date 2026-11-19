"""The user's own browser, which the ``browser`` tool may drive besides the app's
hidden page: the ids the wire and the setting carry, and each browser's name and
macOS user-data-dir, which Electron's ``browser/knownBrowsers.ts`` mirrors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

# Which page a ``browser`` call drives: the run's hidden page in the app, or one
# tab of the run's own in the user's browser.
BrowserChoice = Literal["pantaray", "user"]

UserBrowserId = Literal[
    "chrome", "chrome_beta", "chrome_dev", "chrome_canary", "edge", "brave"
]


@dataclass(frozen=True, slots=True)
class KnownUserBrowser:
    # What the user and the approval reviewer read.
    display_name: str
    # Under ``~/Library/Application Support``.
    user_data_dir: str


KNOWN_USER_BROWSERS: Final[dict[UserBrowserId, KnownUserBrowser]] = {
    "chrome": KnownUserBrowser("Google Chrome", "Google/Chrome"),
    "chrome_beta": KnownUserBrowser("Google Chrome Beta", "Google/Chrome Beta"),
    "chrome_dev": KnownUserBrowser("Google Chrome Dev", "Google/Chrome Dev"),
    "chrome_canary": KnownUserBrowser("Google Chrome Canary", "Google/Chrome Canary"),
    "edge": KnownUserBrowser("Microsoft Edge", "Microsoft Edge"),
    "brave": KnownUserBrowser("Brave", "BraveSoftware/Brave-Browser"),
}
