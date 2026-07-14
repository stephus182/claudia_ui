# Test Coverage Sprint — Bugs Found & Fixed

**Date:** 2026-06-15  
**Scope:** claudia_ui — `tradingview.py`, `order_flow.py`, `agent.py`, `gdrive_sync.py`;  
ibkr_core_mcp — `client.py`  
**Method:** Test-driven coverage (~50 new unit tests); bugs surfaced by tests that should have passed but didn't.  
**Related:** 2026-06-12 security audit — all 8 fixes now have regression tests (`tests/test_security_regressions.py`).

---

## Bugs Found and Fixed

### BUG-1 — `TRADINGVIEW_MCP_PATH` validation let non-`.js` files through if they existed

**File:** `claudia/tradingview.py:62`  
**Commit:** in `feature/test-coverage` merge (3c75f3e → main)

**Root cause:**

```python
# Before (broken)
elif not path.endswith(".js") and not p.is_file():
    log.warning(...)
```

The compound condition `not .js AND not exists` meant a file ending in `.sh` (or any other extension) that *existed* on disk evaluated to `True AND False = False` — the warning was skipped and the invalid path was returned as the selected binary. The `.js` guard was silently defeated by file existence.

**Fix:**

```python
# After
elif not path.endswith(".js"):
    log.warning(...)
```

Any path not ending in `.js` is rejected regardless of whether the file exists. This completes the intent of security audit finding L-1 (which added the existence check) and the extension check independently.

**Caught by:** `test_find_bin_env_var_not_js_falls_through` — which passed in the worktree (no vendor fallback present) but would have caught the regression in CI where the full binary list is exercised.

---

### BUG-2 — `execute_staged_order` left the "Stage this order" button visible on error paths

**File:** `claudia/order_flow.py:97, 125, 173`  
**Commit:** b72502d

**Root cause:**

`await action.remove()` was a standalone call at the bottom of the function, after the `try/except`. Two early-return paths exited before reaching it:

1. **Invalid JSON payload** (line 97): parses `action.payload["order"]`, catches `json.JSONDecodeError` / `KeyError`, sends error message, and `return`s — `action.remove()` never called, button stays in the UI.
2. **Contract not found** (line 125): `search_contract()` returns `[]`, sends "Could not find contract" message, and `return`s from inside the `try` block — same result.

The button persisted in the Chainlit UI after both of these error cases, giving the user no way to dismiss it without refreshing the page.

**Fix:**

```python
# Invalid-payload handler: add remove() before return
except (json.JSONDecodeError, TypeError, KeyError):
    await cl.Message(content="Invalid order proposal data.", author="System").send()
    await action.remove()   # ← added
    return

# Main IBKR block: bare except → except/finally
    except Exception as exc:
        ...
        await cl.Message(content=...).send()
    finally:
        await action.remove()   # ← was standalone after try/except; now in finally
```

The `finally` block also guards any future early returns added inside the main try block.

**Caught by:** `test_execute_staged_order_invalid_payload_sends_error` and `test_execute_staged_order_contract_not_found` — both now assert `action.remove.assert_called_once()`.

---

## Test-Environment Dependency Fixed

Two tests in `test_tradingview.py` relied on the worktree directory having no `vendor/tradingview-mcp/` subtree. When the branch was merged to main (which has a populated vendor archive with `node_modules/`), `_find_tv_mcp_bin()` found the vendor fallback instead of returning `None`.

**Fix:** Added `monkeypatch.setattr(tv_module, "__file__", str(tmp_path / "claudia" / "tradingview.py"))` to both fall-through tests, consistent with the pattern already used in the vendor-path-specific tests. This redirects `Path(__file__).parent.parent` to the tmp directory for the duration of the test.

---

## Test Coverage Added

| File | New tests | What's covered |
|---|---|---|
| `tests/test_tradingview.py` | 17 | All 6 binary discovery candidates, CDP check, tool filtering, env allowlist |
| `tests/test_security_regressions.py` | 9 | All 8 findings from the 2026-06-12 audit |
| `tests/test_agent.py` | +16 | `_history_to_messages`, `_build_version_note`, `_handle_local_tool` (5 cases), `_extract_decisions`, `set_tv_bridge`, `_all_tools` |
| `tests/test_order_flow.py` | +10 | `execute_staged_order`: success, 3 error branches, `action.remove()`, limit price, contract not found |
| `ibkr_core_mcp/tests/test_client.py` | +4 | `ping()` retry on `authenticated: false`, 401 short-circuit, tickle call between attempts |

**Suite total (claudia_ui):** 133 tests, 0 failures.
