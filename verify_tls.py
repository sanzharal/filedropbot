"""Fail a build if either of the bot's TLS stores trusts the affected roots."""
import ssl

import httpx

AFFECTED = {"emSign ECC Root CA - G3", "AAA Certificate Services"}


def verify_store(label, context):
    roots = context.get_ca_certs()
    if len(roots) < 50:
        raise SystemExit(f"{label}: unexpectedly empty or incomplete trust store")
    names = {value for cert in roots for group in cert["subject"]
             for key, value in group if key == "commonName"}
    affected = AFFECTED & names
    if affected:
        raise SystemExit(f"{label}: still trusts {sorted(affected)}")
    print(f"{label}: {len(roots)} roots, affected eMudhra trust paths removed")


if __name__ == "__main__":
    verify_store("Python system defaults", ssl.create_default_context())
    verify_store("HTTPX defaults used by Telegram", httpx.create_ssl_context())
