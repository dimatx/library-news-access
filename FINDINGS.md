# Findings: which MHL newspapers can actually be automated

Measured 2026-09-01 by replaying each flow with plain HTTP.

| Newspaper | Entry point | What the library grants | Automatable? |
|---|---|---|---|
| **Boston Globe** | `mhl.org/connect/37577` | **Rolling 72-hour** access attached to your Globe account. The page itself says: *"At the end of your free temporary access period, simply complete this form again."* | **Yes — and it's the only one that needs it.** |
| New York Times | `mhl.org/connect/20528` | Static gift code (`gift_code` did not change across requests) redeemed once against your NYT login. | No treadmill. Link only. |
| Wall Street Journal | `mhl.org/connect/3969` | Static partner redemption link. | No treadmill. Link only. |
| Washington Post | `mhl.org/connect/3970` | Static special-offer sign-in link. | No treadmill. Link only. |
| Eagle Tribune | `infoweb.newsbank.com/signin/MemorialHallLibrary/ETLL` | **Nothing persistent.** Posting the card mints a throwaway browsing session (`?nb_id=...`). There is no account and no entitlement, so there is nothing to keep alive. | **No — deliberately excluded.** |

## Why only the Globe is automated

The Globe is the one provider where the library grants a **short, expiring
entitlement to an account you own**. Everything else is either a one-shot code
(NYT/WSJ/WaPo) or a stateless session (NewsBank), and re-running those on a
timer accomplishes nothing.

So this service does one thing well: keep the Globe's 72-hour window from
lapsing. The provider engine stays generic (`ez_register` / `link_only`) so a
future newspaper that uses the same expiring-form pattern is a `providers.json`
entry rather than a code change.

## Measured mechanics of the Globe flow

1. `POST https://mhl.org/connect/37577` with `mhl-connect=`, `db_id=37577`,
   `card_number=<card>`.
   - Valid card -> `302` to `https://manage.bostonglobe.com/cs/reg/ez/mhl.aspx`
   - Invalid card -> `200` with `.validation-message`:
     *"Not a valid library card number. Please try again."*
2. That page is ASP.NET WebForms. Echo back `__VIEWSTATE`,
   `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION` plus `txtFirst`, `txtLast`,
   `txtEmail`, `txtPassword`, `txtVerifyPassword`, and POST to `./mhl.aspx`.
   The submit `<button id="cmdSubmit">` has **no `name`**, so nothing extra is
   sent for it.
3. Success -> `302` off `manage.bostonglobe.com` to `bostonglobe.com`.
   Failure -> stays on `mhl.aspx` and renders an error.

Submitting "Create Account" for an account that already exists is the intended
path — it re-ups the existing account rather than erroring. No browser, no
JavaScript, and no CAPTCHA is involved anywhere in this chain.
