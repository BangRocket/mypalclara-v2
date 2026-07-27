import unittest
from unittest.mock import patch

from backend import auth


class DiscordIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        auth._discord_user_id_cache.clear()

    def test_verified_clerk_discord_account_defines_user_id(self) -> None:
        clerk_user = {
            "external_accounts": [
                {
                    "provider": "oauth_discord",
                    "provider_user_id": "123456789",
                    "verification": {"status": "verified"},
                }
            ]
        }
        with (
            patch.object(auth, "current_user_id", return_value="user_1"),
            patch.object(auth, "_get_clerk_user", return_value=clerk_user),
        ):
            self.assertEqual(auth.get_discord_user_id(), 123456789)

    def test_unverified_discord_account_is_rejected(self) -> None:
        clerk_user = {
            "external_accounts": [
                {
                    "provider": "oauth_discord",
                    "provider_user_id": "123456789",
                    "verification": {"status": "unverified"},
                }
            ]
        }
        with (
            patch.object(auth, "current_user_id", return_value="user_1"),
            patch.object(auth, "_get_clerk_user", return_value=clerk_user),
        ):
            self.assertIsNone(auth.get_discord_user_id())

    def test_non_discord_social_account_is_ignored(self) -> None:
        clerk_user = {
            "external_accounts": [
                {
                    "provider": "oauth_github",
                    "provider_user_id": "123456789",
                    "verification": {"status": "verified"},
                }
            ]
        }
        with (
            patch.object(auth, "current_user_id", return_value="user_1"),
            patch.object(auth, "_get_clerk_user", return_value=clerk_user),
        ):
            self.assertIsNone(auth.get_discord_user_id())

    def test_positive_result_is_cached_for_process(self) -> None:
        clerk_user = {
            "external_accounts": [
                {
                    "provider": "discord",
                    "provider_user_id": "123456789",
                    "verification": {"status": "verified"},
                }
            ]
        }
        with (
            patch.object(auth, "current_user_id", return_value="user_1"),
            patch.object(auth, "_get_clerk_user", return_value=clerk_user) as get_user,
        ):
            self.assertEqual(auth.get_discord_user_id(), 123456789)
            self.assertEqual(auth.get_discord_user_id(), 123456789)
        get_user.assert_called_once_with("user_1")


class AccountPortalTests(unittest.TestCase):
    def test_development_issuer_maps_to_account_portal(self) -> None:
        with (
            patch.dict("os.environ", {"CLERK_ACCOUNT_PORTAL_URL": ""}),
            patch.object(
                auth,
                "_config",
                return_value=(
                    "https://example.clerk.accounts.dev",
                    "client_id",
                ),
            ),
        ):
            self.assertEqual(
                auth.account_portal_user_url(),
                "https://example.accounts.dev/user",
            )
