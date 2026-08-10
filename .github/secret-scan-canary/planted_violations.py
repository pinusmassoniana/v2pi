# CI self-test fixture — not application code, not a real test, never imported or run.
#
# This file exists only to prove that the gitleaks rules in .gitleaks.toml actually fire. The
# secret-scan job copies it into a throwaway backend/tests/-shaped directory and scans that copy
# in isolation; a global allowlist entry in .gitleaks.toml keeps this file itself out of the main
# scan of the real tree, so its presence here can never fail that scan. See the "Prove the canary
# rules actually fire" step in .github/workflows/ci-release.yml.
#
# Every value below is planted on purpose and is not, and has never been, real key or network
# material. Do not copy either value into a real fixture — that is exactly the mistake this file
# exists to catch.

# Trips raw-x25519-public-key: 43 base64url characters next to a public-key identifier, the same
# shape as a pasted REALITY public key, but not built from a byte ramp and not on any allowlist.
planted_public_key = "NOTAREALKEYCANARYVALUEDONOTCOPYTHISINTOAFI"

# Trips test-fixture-routable-ipv6: a globally routable IPv6 literal in fixture-shaped context,
# outside 2001:db8::/32, fe80::/10, fc00::/7, ff00::/8, ::1, ::, and every shared-service constant
# this repo already allows.
planted_node = {"address": "dead:beef::1", "port": 443}
