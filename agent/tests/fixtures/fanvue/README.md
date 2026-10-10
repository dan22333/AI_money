# Fanvue payload fixtures

These JSON files are the **ASSUMED** shapes of Fanvue webhook events and API
responses that `router.py` / `fanvue_client.py` / `reconcile.py` parse. They are
derived from the Fanvue docs, **not yet verified against the live API.**

`test_fanvue_contracts.py` runs the real parsing code over these fixtures so the
contract is pinned in one place and any field-path change breaks a test.

## How to turn "assumed" into "verified"

Once OAuth tokens exist (run `scripts/fanvue_auth.py`), capture real shapes:

```
python3 scripts/fanvue_probe.py          # dumps get_me / list_chats JSON
```

For webhooks, log a real delivery (or use Fanvue's test-send) and paste the body
here, replacing the matching fixture. If a real payload differs, the test will
fail — that failure IS the signal to fix the parser in router.py.
